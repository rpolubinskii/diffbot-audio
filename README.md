# diffbot-audio

Local robot audio service for DiffBot. V1 exposes a single streaming gRPC `Speak` RPC and uses Piper plus a local playback command.

## Configure

```bash
export DIFFBOT_AUDIO_PIPER_BINARY=piper
export DIFFBOT_AUDIO_PIPER_MODEL=/path/to/voice.onnx
export DIFFBOT_AUDIO_PLAYBACK_COMMAND=aplay
# Optional:
export DIFFBOT_AUDIO_SPEAKER_DEVICE=default
export DIFFBOT_AUDIO_HOST=0.0.0.0
export DIFFBOT_AUDIO_PORT=50052
```

`DIFFBOT_AUDIO_PLAYBACK_COMMAND` can be a simple command such as `aplay`, or a template containing `{file}` and optionally `{device}`.

## Run

```bash
uv sync
uv run diffbot-audio
```

## Smoke Test

```bash
uv run diffbot-audio-say "test"
```

The client prints stream states and exits with a non-zero status if the service returns `FAILED`.
