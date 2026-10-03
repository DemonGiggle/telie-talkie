"""Short SQLite transactions and atomic audio files, owned by the asyncio thread."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path


class StorageFull(Exception):
    pass


class InstanceLock:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (directory / "instance.lock").open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another run/pair process is using this storage directory") from None

    def close(self) -> None:
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Store:
    def __init__(self, directory: Path, max_audio_bytes: int):
        self.directory = directory
        self.audio_dir = directory / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_audio_bytes = max_audio_bytes
        self.db = sqlite3.connect(directory / "queues.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (update_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, file_id TEXT NOT NULL,
                size INTEGER, duration REAL, state TEXT NOT NULL DEFAULT 'pending',
                path TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, path TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0
            );
        """)

    def close(self) -> None:
        self.db.close()

    def bind(self, token: str, chat_id: int, user_id: int) -> None:
        # Prevent queued audio leaking to a different bot or newly paired account.
        bot_id = token.partition(":")[0]
        identity = hashlib.sha256(f"{bot_id}:{chat_id}:{user_id}".encode()).hexdigest()
        row = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if row and row[0] != identity:
            raise ValueError(
                "Storage belongs to another bot/user. Restore its configuration or use "
                "a new storage directory; existing queues have been preserved"
            )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('identity', ?)", (identity,))

    @property
    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def recover(self) -> None:
        with self.db:
            self.db.execute("UPDATE inbox SET state='pending' WHERE state='downloading'")
            self.db.execute("UPDATE inbox SET state='ready' WHERE state='playing'")
            self.db.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
            # Redownload missing incoming files; an outbox file should never go missing.
            for row in self.db.execute("SELECT id, path FROM inbox WHERE state='ready'"):
                if not self.path(row["path"]).is_file():
                    self.db.execute(
                        "UPDATE inbox SET state='pending', path=NULL WHERE id=?", (row["id"],)
                    )
        referenced = {
            r[0]
            for r in self.db.execute(
                "SELECT path FROM inbox WHERE state IN ('ready', 'playing') "
                "UNION SELECT path FROM outbox WHERE state IN ('pending', 'sending')"
            )
        }
        for path in self.audio_dir.iterdir():
            if path.is_file() and path.name not in referenced:
                path.unlink()

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("Invalid managed audio filename")
        return self.audio_dir / name

    @property
    def used_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.audio_dir.iterdir() if p.is_file())

    def stage(self) -> Path:
        path = self.audio_dir / f"{uuid.uuid4().hex}.part"
        try:
            path.touch(mode=0o600, exist_ok=False)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise StorageFull("The audio filesystem is full") from None
            raise
        return path

    def append(self, path: Path, chunk: bytes) -> None:
        # All writers run on the same event loop, without an await between check and write.
        if self.used_bytes + len(chunk) > self.max_audio_bytes:
            raise StorageFull("Audio storage is full; existing queued messages are preserved")
        try:
            with path.open("ab") as file:
                file.write(chunk)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise StorageFull("The audio filesystem is full") from None
            raise

    def commit_file(self, path: Path, suffix: str) -> str:
        with path.open("rb") as file:
            os.fsync(file.fileno())
        final = path.with_suffix(suffix)
        path.rename(final)
        fd = os.open(self.audio_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return final.name

    def enqueue_voice(self, chat_id: int, data: bytes) -> int:
        path = self.stage()
        try:
            self.append(path, data)
            name = self.commit_file(path, ".ogg")
        finally:
            path.unlink(missing_ok=True)
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO outbox(chat_id, path) VALUES (?, ?)", (chat_id, name)
            )
        return cursor.lastrowid

    def notice(self, chat_id: int, text: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO notices(chat_id, text) VALUES (?, ?)", (chat_id, text))

    def ingest(
        self, updates: list[dict], chat_id: int, user_id: int, max_size: int, max_seconds: float
    ) -> None:
        # Metadata and polling progress share one transaction. A subsequent poll acknowledges
        # updates only after the pending download (or deliberate ignore/rejection) is durable.
        with self.db:
            offset = self.offset
            for update in sorted(updates, key=lambda x: x["update_id"]):
                uid = update["update_id"]
                offset = max(offset, uid + 1)
                if self.db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (uid,)).rowcount == 0:
                    continue
                message = update.get("message", {})
                if (
                    message.get("chat", {}).get("type") != "private"
                    or message.get("chat", {}).get("id") != chat_id
                    or message.get("from", {}).get("id") != user_id
                ):
                    continue
                media = message.get("voice") or message.get("audio")
                if not media or not media.get("file_id"):
                    continue
                size, duration = media.get("file_size"), media.get("duration")
                oversized = (size is not None and size > max_size) or (
                    duration is not None and duration > max_seconds
                )
                self.db.execute(
                    """INSERT INTO inbox
                    (update_id, chat_id, user_id, file_id, size, duration, state, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uid,
                        chat_id,
                        user_id,
                        media["file_id"],
                        size,
                        duration,
                        "failed" if oversized else "pending",
                        "oversized" if oversized else None,
                    ),
                )
                if oversized:
                    self.db.execute(
                        "INSERT INTO notices(chat_id, text) VALUES (?, ?)",
                        (
                            chat_id,
                            "Audio rejected: file size or duration exceeds the "
                            "device's configured limit.",
                        ),
                    )
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset', ?)", (str(offset),))

    def head(self, table: str, states: tuple[str, ...]):
        self._table(table)
        marks = ",".join("?" for _ in states)
        return self.db.execute(
            f"SELECT * FROM {table} WHERE state IN ({marks}) ORDER BY id LIMIT 1", states
        ).fetchone()

    def head_notice(self):
        return self.db.execute("SELECT * FROM notices ORDER BY id LIMIT 1").fetchone()

    @staticmethod
    def _table(table: str) -> None:
        if table not in ("inbox", "outbox", "notices"):
            raise ValueError("Invalid queue")

    def state(self, table: str, row_id: int, state: str) -> None:
        self._table(table)
        with self.db:
            self.db.execute(f"UPDATE {table} SET state=? WHERE id=?", (state, row_id))

    def ready(self, row_id: int, name: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET state='ready', path=?, attempts=0, next_attempt=0 WHERE id=?",
                (name, row_id),
            )

    def retry(self, table: str, row, delay: float, error: str) -> None:
        self._table(table)
        extra = ", state='pending', error=?" if table != "notices" else ""
        args = [time.time() + delay]
        if extra:
            args.append(error)
        args.append(row["id"])
        with self.db:
            self.db.execute(
                f"UPDATE {table} SET attempts=attempts+1, next_attempt=?{extra} WHERE id=?", args
            )

    def finish(self, table: str, row, error: str | None = None) -> None:
        self._table(table)
        with self.db:
            if table == "notices":
                self.db.execute("DELETE FROM notices WHERE id=?", (row["id"],))
            else:
                self.db.execute(
                    f"UPDATE {table} SET state=?, error=? WHERE id=?",
                    ("failed" if error else "done", error, row["id"]),
                )
                if error and table == "inbox":
                    self.db.execute(
                        "INSERT INTO notices(chat_id, text) VALUES (?, ?)",
                        (row["chat_id"], f"Audio rejected: {error}."),
                    )
        if table != "notices" and row["path"]:
            self.path(row["path"]).unlink(missing_ok=True)
