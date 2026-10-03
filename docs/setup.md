# Device setup

## BotFather and pairing

Create a dedicated bot through [@BotFather](https://t.me/BotFather) with `/newbot`. Start its private chat before pairing. The bot must not share a polling connection with another application. `doctor --online` checks for an active webhook; remove a previously configured webhook through the Telegram Bot API before using long polling.

Set the token in the environment variable named by `telegram.token_env`. Never paste it into a URL in a bug report, terminal log, or screenshot. Run `pair` with the runtime stopped, send the printed command privately, and configure both IDs. Pairing only accepts the exact random code and expires after five minutes. Use a dedicated new bot for initial pairing: pairing polls may acknowledge earlier unrelated updates while looking for the code.

## Audio checks and calibration

1. Run `telie-talkie devices` and select the intended input and output. Device names usually survive reboots better than numeric indices. Where several devices share a name, use a more specific PortAudio name.
2. Run `telie-talkie doctor --audio-check`. Hear the ready tone, speak for three seconds, and confirm the replay is clear. Input must support mono 16 kHz; output rate defaults to 48 kHz.
3. Run `telie-talkie run`. Say the wake phrase clearly, wait for the beep and brief settling interval, then speak. Test at the normal distance and background noise level.
4. Increase `keywords_threshold` if ordinary speech causes false triggers. Decrease it cautiously if the phrase rarely triggers. `keywords_score` controls keyword boosting; higher scores make the phrase easier to detect.
5. Tune `vad_threshold` if noise counts as speech or quiet speech is missed. Keep pre-roll at least as long as the VAD minimum speech confirmation period.
6. Send a voice note containing the wake phrase back to the device. Playback should not trigger a recording. Increase `audio.settle_seconds` if room echoes last beyond playback.

If a USB microphone cannot run at 16 kHz directly, configure a Linux audio layer that provides that rate or use a compatible input. The app does not add microphone sample-rate conversion in V1. The service below uses an ALSA/PortAudio device available to the `audio` group; a desktop-only PulseAudio/PipeWire session may require a user service and its session environment instead.

## Run at startup

The supplied service uses generic installation paths and a dedicated `telie` service account. From the repository checkout, install the code and models under `/opt/telie-talkie`, local configuration under `/etc/telie-talkie`, and state under `/var/lib/telie-talkie`.

```bash
sudo useradd --system --user-group --no-create-home --groups audio telie
sudo install -d -m 0755 /opt/telie-talkie
sudo cp -r pyproject.toml README.md LICENSE src /opt/telie-talkie/
sudo python3 -m venv /opt/telie-talkie/.venv
sudo /opt/telie-talkie/.venv/bin/python -m pip install /opt/telie-talkie

sudo install -d -m 0750 -o root -g telie /etc/telie-talkie
sudo install -d -m 0700 -o telie -g telie /var/lib/telie-talkie
sudo install -m 0640 -o root -g telie config.example.toml /etc/telie-talkie/config.toml
sudo install -m 0600 deploy/bot.env.example /etc/telie-talkie/bot.env
sudoedit /etc/telie-talkie/config.toml /etc/telie-talkie/bot.env
```

Set the paired IDs and audio devices in the installed TOML file. Set `storage.directory = "/var/lib/telie-talkie"` and `models.directory = "/opt/telie-talkie/models"`. Replace the environment file's token placeholder locally.

Download models before starting the protected service:

```bash
sudo /opt/telie-talkie/.venv/bin/telie-talkie --config /etc/telie-talkie/config.toml setup-models
# setup-models writes the runtime keyword file as the invoking user.
sudo chown -R telie:telie /var/lib/telie-talkie
sudo install -m 0644 deploy/telie-talkie.service /etc/systemd/system/telie-talkie.service
sudo systemctl daemon-reload
sudo systemctl enable --now telie-talkie
sudo systemctl status telie-talkie
sudo journalctl -u telie-talkie -f
```

The service restarts after failures, retains queues, and permits writes only to its state directory. Keep the configuration and state together with the original bot/user pairing. Changing the bot ID or authorized user requires a separate state directory to avoid sending old recordings to a different account. Rotating the token for the same bot retains the queue binding.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| PortAudio library missing | Install `libportaudio2` on Debian/Raspberry Pi OS |
| Invalid input or sample rate | Verify device selection and 16 kHz mono support with `doctor` |
| Speaker silent | Check output device, mixer mute, amplifier power, and `audio.volume` |
| Device available interactively but not in service | Check `audio` group access and ALSA versus session audio routing |
| Missing model | Run `setup-models` using the same configuration as `run` |
| Polling status 409 | Stop other pollers and remove any webhook |
| Telegram status 401/403 | Verify the token, start the private chat, and check whether the bot was blocked |
| Telegram status 429 | Let the queue honor the server's retry delay |
| Microphone overflow | Check CPU load and device buffering; inference needs to keep up with real-time audio |
| Storage full | Deliver existing queued messages or increase the audio budget and available disk space |
| Binding mismatch | Restore the original bot/user settings, or use a new state directory |

Error notices persist while offline. Rejected new audio never evicts existing queued recordings. Incoming duration is checked from decoded data as well as Telegram metadata. The audio budget includes compressed queued files and partial downloads; model files, SQLite metadata, and bounded in-memory decoded audio are separate from that budget.

## Hardware acceptance

Before unattended use, complete these checks on the target device:

- Wake detection works repeatedly at the intended speaking distance.
- Telegram receives understandable recordings without the wake phrase or beep.
- Voice notes and audio files sent by the authorized user play in order.
- A reply arriving during recording waits for recording to finish.
- Speaker playback of the wake phrase does not start a new recording.
- After disconnecting the network, several outgoing recordings remain queued and arrive after reconnection.
- Restarting during an outage preserves queued work; interrupting playback replays it from the beginning.
- Oversized/corrupt media is rejected and a later valid message still plays.

The automated test suite covers software behavior. Live Telegram delivery and acoustic performance still require this device-level check.
