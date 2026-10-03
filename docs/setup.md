# Device setup

## BotFather and pairing

Create a dedicated bot through [@BotFather](https://t.me/BotFather) with `/newbot`. Start its private chat before pairing. The bot must not share a polling connection with another application. `doctor --online` checks for an active webhook; remove a previously configured webhook through the Telegram Bot API before using long polling.

For interactive use, set the token in the environment variable named by `telegram.token_env`. A token file supplied through `telegram.token_file_env` is also supported. For unattended use, follow the [systemd credential setup](systemd.md#token-design). Never paste it into a URL in a bug report, terminal log, or screenshot.

If you know your numeric Telegram user ID, set `telegram.user_id` in TOML and omit `chat_id` or leave it at `0`. The app derives the private chat ID from the user ID, while incoming messages must still match that chat and sender. `pair --user-id ID` prints these settings locally without a token, network access, or a code exchange. It does not write your configuration. Usernames and phone numbers cannot replace the numeric ID.

If you do not know the ID, run `pair` with the runtime stopped, send the printed command privately to your bot, and configure the returned `user_id`. Discovery only accepts the exact random code; its lifetime defaults to five minutes and is configurable. Use a dedicated new bot for initial discovery: pairing polls may acknowledge earlier unrelated updates while looking for the code. Start the bot's private chat before running the device.

## Audio checks and calibration

Set `detection.wake_phrase` to your preferred English phrases in TOML, such as `["HELLO KITTY", "HEY BUDDY"]`, and restart the app/service. Any listed phrase triggers recording; a single string also works. The app creates the keyword configuration automatically; the [wake phrase settings](configuration.md#wake-phrase) explain the supported format.

1. Run `telie-talkie devices` and select the intended input and output. Device names usually survive reboots better than numeric indices. Where several devices share a name, use a more specific PortAudio name.
2. Run `telie-talkie doctor --audio-check`. Hear the ready tone, speak for the configured check duration (three seconds by default), and confirm the replay is clear. Set the input rate and channels supported by the device; capture is normalized locally to 16 kHz mono. Output rate defaults to 48 kHz.
3. Run `telie-talkie run`. Say each configured wake phrase clearly, wait for the beep and brief settling interval, then speak. Test at the normal distance and background noise level.
4. Increase `keywords_threshold` if ordinary speech causes false triggers. Decrease it cautiously if the phrase rarely triggers. `keywords_score` controls keyword boosting; higher scores make the phrase easier to detect.
5. Tune `vad_threshold` if noise counts as speech or quiet speech is missed. Keep pre-roll at least as long as the VAD minimum speech confirmation period.
6. Send voice notes containing the configured wake phrases back to the device. Playback should not trigger a recording. Increase `audio.settle_seconds` if room echoes last beyond playback.

If a USB microphone exposes 44.1 or 48 kHz, set `audio.input_sample_rate` to that native rate; the app handles streaming resampling. The [configuration guide](configuration.md) includes stereo input, buffering, latency, slower CPU, and network examples. The service below uses an ALSA/PortAudio device available to the `audio` group; a desktop-only PulseAudio/PipeWire session may require a user service and its session environment instead.

## Run at startup

Follow the [systemd installation guide](systemd.md) to install the app with a dedicated service account, startup at boot, automatic recovery, and graceful shutdown. The supplied unit uses systemd credentials to read a root-owned token file. Configuration and models remain read-only, and queued audio lives in a private managed state directory.

The guide covers installation, pairing and audio checks as the service account, token rotation, custom paths, and migration from the previous environment-file service.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| PortAudio library missing | Install `libportaudio2` on Debian/Raspberry Pi OS |
| Invalid input or sample rate | Verify the configured hardware rate, channel count, and device with `doctor` |
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
