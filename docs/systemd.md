# Run with systemd

The supplied [service unit](../deploy/telie-talkie.service) starts Telie Talkie at boot, restarts it after failures, and runs it as a dedicated `telie` account with audio-device access. It requires **systemd 247 or later**, Python 3.11+, FFmpeg, and PortAudio. Use ALSA devices accessible to the `audio` group; desktop session audio may require a user service instead.

## Token design

Store the BotFather token as the only value in `/etc/telie-talkie/bot.token`, owned by **root:root with mode 0600**. The system manager reads that file when starting the service. `LoadCredential=` supplies a private runtime copy, which the app reads through systemd's `CREDENTIALS_DIRECTORY`. The token value stays out of the unit file, TOML, process arguments, and service environment. Child processes inherit the credential-directory path rather than the token value.

This follows systemd's [service credential interface](https://systemd.io/CREDENTIALS/). The source file is plaintext on disk; protect it and any backups. The service account needs access to the runtime credential, not to the root-owned source file. The app reads the token once at startup, redacts it from logs, and never reports token-file paths or contents in read errors.

For interactive use, `TELEGRAM_BOT_TOKEN` remains supported. A nonempty `TELEGRAM_BOT_TOKEN_FILE` selects an explicit token file; otherwise a systemd credential takes precedence over the environment token. An unreadable, empty, malformed, or oversized token file fails rather than falling back to an environment token. Files contain a single UTF-8 token, with an optional final newline, and must be at most 4096 bytes. Variable names can be changed through `telegram.token_env` and `telegram.token_file_env`; update `UnsetEnvironment=` to match. Change `telegram.token_credential` and the identifier in `LoadCredential=` together to rename the credential. The supplied unit clears both token environment variables so it always uses the credential.

## Install

From a repository checkout, install the dependencies and create the account once:

```bash
sudo apt update
sudo apt install python3-venv python3-dev libportaudio2 ffmpeg
sudo useradd --system --user-group --no-create-home --groups audio telie
sudo install -d -m 0755 /opt/telie-talkie
sudo cp -r pyproject.toml README.md LICENSE src /opt/telie-talkie/
sudo python3 -m venv /opt/telie-talkie/.venv
sudo /opt/telie-talkie/.venv/bin/python -m pip install /opt/telie-talkie

sudo install -d -m 0750 -o root -g telie /etc/telie-talkie
sudo install -d -m 0700 -o telie -g telie /var/lib/telie-talkie
sudo install -m 0640 -o root -g telie config.example.toml /etc/telie-talkie/config.toml
sudoedit /etc/telie-talkie/config.toml
```

In the installed TOML, set these directories and select the audio devices/rates for your hardware. Leave `token_env`, `token_file_env`, and `token_credential` at their defaults for the supplied unit. The token value belongs in its separate file.

```toml
[storage]
directory = "/var/lib/telie-talkie"
max_audio_bytes = 536870912

[models]
directory = "/opt/telie-talkie/models"
```

Edit the existing tables rather than adding duplicate TOML tables. Download and verify the models before starting the protected service:

```bash
sudo /opt/telie-talkie/.venv/bin/telie-talkie --config /etc/telie-talkie/config.toml setup-models
sudo chown -R telie:telie /var/lib/telie-talkie
```

Create the token file and paste **only the token value** in the editor. These commands preserve an existing file's contents. The token is never included in a shell command or shell history.

```bash
sudo touch /etc/telie-talkie/bot.token
sudo chown root:root /etc/telie-talkie/bot.token
sudo chmod 0600 /etc/telie-talkie/bot.token
sudoedit /etc/telie-talkie/bot.token
```

## Pair and check as the service account

Start the bot's private chat in Telegram. Keep the main service stopped while pairing. Use a temporary systemd unit to pass the same credential and run as the same account:

```bash
sudo systemd-run --unit=telie-talkie-pair --collect --wait --pty \
  --property=User=telie --property=Group=telie \
  --property=SupplementaryGroups=audio \
  --property=LoadCredential=telegram_bot_token:/etc/telie-talkie/bot.token \
  --property='UnsetEnvironment=TELEGRAM_BOT_TOKEN TELEGRAM_BOT_TOKEN_FILE' \
  /opt/telie-talkie/.venv/bin/telie-talkie \
  --config /etc/telie-talkie/config.toml pair
```

Send the printed pairing command privately to the bot. Paste the returned chat/user IDs into `/etc/telie-talkie/config.toml` with `sudoedit`. Then check Telegram and the physical microphone/speaker using a temporary unit:

```bash
sudo systemd-run --unit=telie-talkie-doctor --collect --wait --pty \
  --property=User=telie --property=Group=telie \
  --property=SupplementaryGroups=audio \
  --property=LoadCredential=telegram_bot_token:/etc/telie-talkie/bot.token \
  --property='UnsetEnvironment=TELEGRAM_BOT_TOKEN TELEGRAM_BOT_TOKEN_FILE' \
  /opt/telie-talkie/.venv/bin/telie-talkie \
  --config /etc/telie-talkie/config.toml doctor --online --audio-check
```

## Enable and operate

```bash
sudo install -m 0644 deploy/telie-talkie.service /etc/systemd/system/telie-talkie.service
sudo systemd-analyze verify /etc/systemd/system/telie-talkie.service
sudo systemctl daemon-reload
sudo systemctl enable --now telie-talkie.service
sudo systemctl status telie-talkie.service
sudo journalctl -u telie-talkie.service -f
```

The unit uses `StateDirectory=` to maintain the private state directory. Configuration, code, and models are read-only inside the service; persistent writes go to `/var/lib/telie-talkie`. `ProtectHome=` hides home directories, so install code/models under the supplied paths or change the sandbox settings for your deployment. Adjust service settings with `sudo systemctl edit telie-talkie.service`; a custom state directory also needs a matching `StateDirectory=` or `ReadWritePaths=` override.

`systemctl stop` sends SIGTERM to Python. `KillMode=mixed` lets Python close its FFmpeg children during cleanup before systemd forces remaining processes down on timeout. The app cancels its workers, closes audio and HTTP connections, and closes SQLite before exiting successfully. Stored messages survive; an unfinished microphone recording has not yet been queued. Playback interrupted by a stop is replayed from the beginning on the next start. The unit allows 30 seconds for shutdown and restarts failed processes after five seconds.

To rotate the token for the **same bot**, edit the source file and restart:

```bash
sudoedit /etc/telie-talkie/bot.token
sudo systemctl restart telie-talkie.service
```

Credential contents are fixed for each service invocation, so a restart is required. Token rotation preserves the queue binding. Switching to a different bot or user requires a new state directory to avoid delivering queued recordings to a different account. To upgrade the app, stop the service, update the installed source/package, and start the service again; keep the existing configuration, token, models, and state.

## Migrate an existing environment-file service

Stop the service, create the token file as above, and replace the installed unit with the new one. Then run `daemon-reload` and start the service. The previous `/etc/telie-talkie/bot.env` is no longer read by the supplied unit. Remove the obsolete secret file after verifying the new setup. Keep existing paired IDs and state.

If your systemd is older than 247, upgrade it or override the credential settings to use the legacy [environment-file example](../deploy/bot.env.example):

```ini
[Service]
LoadCredential=
UnsetEnvironment=
EnvironmentFile=/etc/telie-talkie/bot.env
```

Install that file as root:root mode 0600 and edit the placeholder locally. This fallback passes the token in the service environment, including to child processes. The credentials-based unit is the recommended installation.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Service fails before Python starts | Check `journalctl` for a missing credential source, wrong installation path, or unit error |
| Cannot read bot token file | Check `LoadCredential=`, `telegram.token_credential`, and the source file; avoid printing its contents |
| Token changed but authentication still fails | Restart the service to reload the credential; verify that the file contains only the token |
| Microphone/speaker works only in the desktop session | Select ALSA devices accessible to `telie`, or adapt the unit for a user audio session |
| Permission denied for state | Use the managed state path and account ownership; match sandbox overrides to custom paths |
| Models inside a home directory cannot load | Use `/opt/telie-talkie/models` or deliberately adjust `ProtectHome=` |

Complete the [hardware acceptance checks](setup.md#hardware-acceptance) before unattended use.
