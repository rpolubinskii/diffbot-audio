# diffbot-audio

Local robot audio service for DiffBot. V1 exposes a single streaming gRPC `Speak` RPC and uses Piper plus a local playback command.

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
```

`playback.command` can be a simple command such as `aplay`, or a template containing `{file}` and optionally `{device}`.

## Run

```bash
uv sync
uv run diffbot-audio
```

Use an explicit config path when needed:

```bash
uv run diffbot-audio --config /path/to/config.toml
```

## Smoke Test

```bash
uv run diffbot-audio-say "test"
```

Or with an explicit config:

```bash
uv run diffbot-audio-say --config /path/to/config.toml "test"
```

The client prints stream states and exits with a non-zero status if the service returns `FAILED`.
