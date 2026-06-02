# diffbot-audio

Local robot audio service for DiffBot. V1 exposes streaming gRPC `Speak` and `StreamVoiceCommands` RPCs. TTS uses Piper plus a local playback command. VTT uses openWakeWord and faster-whisper.

## Configure

Create a local config from the example:

```bash
cp config.toml config.toml
```

Edit `config.toml` for the robot:

```toml
[grpc]
host = "0.0.0.0"
port = 50052

[piper]
binary = "piper"
model = "/path/to/voice.onnx"
extra_args = []

[playback]
command = "aplay"
speaker_device = "default"

[vtt]
enabled = true
backend = "faster-whisper"
model = "small"
language = "en"
compute_type = "float32"
beam_size = 5
vad_filter = false

[wake_word]
enabled = true
backend = "openwakeword"
model = "alexa"
threshold = 0.5

[microphone]
device = "default"

[sounds]
wake_triggered = "sounds/switch_005.ogg"
recording_sent = "sounds/switch_007.ogg"
```

`playback.command` can be a simple command such as `aplay`, or a template containing `{file}` and optionally `{device}`.
Notification sounds reuse `playback.command` when possible. If `playback.command` is `aplay` and the sound is OGG, the service defaults to `paplay`; set `sounds.command` to override that.

`wake_word.model` can be a built-in openWakeWord model name (`alexa`, `hey_mycroft`, `hey_jarvis`, `hey_rhasspy`, `weather`, `timer`) or a path to a custom `.onnx`/`.tflite` model. Relative paths are resolved from the config file location:

```toml
[wake_word]
model = "models/Robot_20260330_000935.onnx"
```

## Run

```bash
uv python install 3.11
uv sync --python 3.11
uv run --python 3.11 diffbot-audio
```

Use an explicit config path when needed:

```bash
uv run --python 3.11 diffbot-audio --config /path/to/config.toml
```

## Smoke Test

```bash
uv run --python 3.11 diffbot-audio-say "test"
```

Or with an explicit config:

```bash
uv run --python 3.11 diffbot-audio-say --config /path/to/config.toml "test"
```

The client prints stream states and exits with a non-zero status if the service returns `FAILED`.

Stream finalized voice commands:

```bash
uv run --python 3.11 diffbot-audio-listen
```

Say the configured wake word, then a short command. The client prints finalized command text or `ERROR: ...` events.
