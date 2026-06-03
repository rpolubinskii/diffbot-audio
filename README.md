# diffbot-audio

Local robot audio service for DiffBot. V1 exposes streaming gRPC `Speak` and `StreamVoiceCommands` RPCs. TTS uses Piper plus a local playback command. VTT can be triggered by openWakeWord or voice activity, with either faster-whisper or NVIDIA Riva ASR.

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
selected_backend = "whisper_base"
trigger = "wake_word"
command_prefixes = ["robot"]
voice_activity_rms_threshold = 250.0
voice_activity_cooldown_seconds = 0.8

[vtt.backends.whisper_base]
type = "faster-whisper"
model = "small"
language = "en"
compute_type = "float32"
beam_size = 5
vad_filter = false

[vtt.backends.riva_1_1b]
type = "riva"
uri = "localhost:50051"
language_code = "en-US"
model = "parakeet-1.1b-en-us-asr-streaming"
automatic_punctuation = true
use_ssl = false

[vtt.backends.riva_0_6b]
type = "riva"
uri = "localhost:50051"
language_code = "en-US"
model = "parakeet-0.6b-en-us-asr-streaming"
automatic_punctuation = true
use_ssl = false

[wake_word]
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

`wake_word` settings are used only when `vtt.trigger = "wake_word"`. The wake word is enabled by selecting that trigger. `wake_word.model` can be a built-in openWakeWord model name (`alexa`, `hey_mycroft`, `hey_jarvis`, `hey_rhasspy`, `weather`, `timer`) or a path to a custom `.onnx`/`.tflite` model. Relative paths are resolved from the config file location:

```toml
[wake_word]
model = "models/Robot_20260330_000935.onnx"
```

To run VTT without openWakeWord, use voice-activity trigger mode. The service records speech based on volume/silence, transcribes it, and only emits commands that start with a configured prefix. Prefix matching is case-insensitive and the prefix is stripped before emitting:

```toml
[vtt]
enabled = true
selected_backend = "riva_1_1b"
trigger = "voice_activity"
command_prefixes = ["robot"]
voice_activity_rms_threshold = 250.0
voice_activity_cooldown_seconds = 0.8
```

With this setup, `robot say hello` emits `say hello`; unrelated speech is ignored.
If speech does not trigger reliably, lower `voice_activity_rms_threshold` in small steps, for example `200.0` then `150.0`. If background noise triggers too often, raise it. Voice-activity mode does not play notification sounds while recording, because the microphone can hear them and retrigger.

### NVIDIA Riva ASR on Jetson

For Jetson-local ASR, run the Riva server locally and select a Riva backend profile, for example `selected_backend = "riva_1_1b"`. The trigger flow stays local: this service records a 16 kHz mono utterance from wake-word or voice-activity detection, and Riva receives that PCM audio over local gRPC.

On JetPack 6 / Ubuntu 22.04, validate the base system:

```bash
cat /etc/nv_tegra_release
docker --version
docker info | grep -i nvidia
sudo nvpmodel -m 0
```

Install and authenticate the NVIDIA NGC CLI with an NGC API key, then download and configure Riva Embedded:

```bash
ngc registry resource download-version nvidia/riva/riva_quickstart_arm64:2.19.0
cd riva_quickstart_arm64_v2.19.0
```

Edit `config.sh`:

```bash
service_enabled_asr=true
service_enabled_tts=false
service_enabled_nmt=false
asr_language_code=("en-US")
asr_acoustic_model=("parakeet_1.1b")
use_asr_streaming_throughput_mode=false
```

Initialize, start, and test Riva:

```bash
bash riva_init.sh
bash riva_start.sh
riva_streaming_asr_client --list_models
riva_asr_client --audio_file=/opt/riva/wav/en-US_sample.wav
```

If Jetson memory is insufficient, change only `asr_acoustic_model` to `parakeet_0.6b` and rerun `bash riva_init.sh`.

Use this VTT config when Riva is running:

```toml
[vtt]
enabled = true
selected_backend = "riva_1_1b"
trigger = "voice_activity"
command_prefixes = ["robot"]
voice_activity_rms_threshold = 250.0
voice_activity_cooldown_seconds = 0.8

[vtt.backends.riva_1_1b]
type = "riva"
uri = "localhost:50051"
language_code = "en-US"
model = "parakeet-1.1b-en-us-asr-streaming"
automatic_punctuation = true
use_ssl = false
```

When the selected backend profile has `type = "riva"`, service startup fails fast if `nvidia-riva-client` cannot be imported or the configured Riva model cannot be listed from the server. The `model` value must match `riva_streaming_asr_client --list_models`; `parakeet-1.1b-en-us-asr-streaming` is the expected starting value for `asr_acoustic_model=("parakeet_1.1b")`.

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

Say the configured wake word, then a short command. In voice-activity mode, say a command starting with the configured prefix, such as `robot say hello`. The client prints finalized command text or `ERROR: ...` events.
