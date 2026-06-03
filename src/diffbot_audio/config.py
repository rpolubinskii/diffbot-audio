from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioConfig:
    host: str
    port: int
    piper_binary: str
    piper_model: str
    piper_extra_args: tuple[str, ...]
    playback_command: str
    speaker_device: str | None
    vtt: "VttConfig"
    wake_word: "WakeWordConfig"
    microphone: "MicrophoneConfig"
    sounds: "SoundsConfig"


@dataclass(frozen=True)
class VttConfig:
    enabled: bool
    selected_backend: str
    backend: "VttBackendConfig | None"
    trigger: str
    command_prefixes: tuple[str, ...]
    voice_activity_rms_threshold: float
    voice_activity_cooldown_seconds: float
    voice_activity_preroll_seconds: float


@dataclass(frozen=True)
class FasterWhisperVttBackendConfig:
    type: str
    model: str
    language: str | None
    compute_type: str
    beam_size: int
    vad_filter: bool


@dataclass(frozen=True)
class RivaVttBackendConfig:
    type: str
    uri: str
    language_code: str
    model: str
    automatic_punctuation: bool
    use_ssl: bool


VttBackendConfig = FasterWhisperVttBackendConfig | RivaVttBackendConfig


@dataclass(frozen=True)
class WakeWordConfig:
    backend: str
    model: str
    model_path: Path | None
    threshold: float


@dataclass(frozen=True)
class MicrophoneConfig:
    device: str | None


@dataclass(frozen=True)
class SoundsConfig:
    wake_triggered: Path
    recording_sent: Path
    playback_command: str | None


SUPPORTED_WAKE_WORD_MODELS = {
    "alexa",
    "hey_mycroft",
    "hey_jarvis",
    "hey_rhasspy",
    "weather",
    "timer",
}


DEFAULT_CONFIG_PATH = Path("config.toml")


class ConfigError(ValueError):
    pass


def config_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to diffbot-audio TOML config.")
    return parser


def load_config(argv: list[str] | None = None) -> AudioConfig:
    parser = config_arg_parser("Run the DiffBot audio gRPC service.")
    args = parser.parse_args(argv)
    return load_config_file(args.config)


def load_config_file(path: Path) -> AudioConfig:
    data = _read_toml(path)
    grpc_config = _table(data, "grpc")
    piper_config = _table(data, "piper")
    playback_config = _table(data, "playback")
    vtt_config = _table(data, "vtt")
    wake_word_config = _table(data, "wake_word")
    microphone_config = _table(data, "microphone")
    sounds_config = _table(data, "sounds")

    piper_model = _optional_string(piper_config, "model")
    if not piper_model:
        raise ConfigError(f"{path}: piper.model is required.")

    vtt = _vtt_config(path, vtt_config)
    wake_word_active = vtt.enabled and vtt.trigger == "wake_word"

    wake_word_model = _string(wake_word_config, "model", "hey_jarvis")
    wake_word_model_path = _wake_word_model_path(path, wake_word_model)
    wake_word = WakeWordConfig(
        backend=_string(wake_word_config, "backend", "openwakeword"),
        model=wake_word_model,
        model_path=wake_word_model_path,
        threshold=_float(wake_word_config, "threshold", 0.5),
    )
    if wake_word_active and wake_word.backend != "openwakeword":
        raise ConfigError(f"{path}: wake_word.backend must be \"openwakeword\" when wake word is enabled.")
    if wake_word_active and wake_word.model_path is None and wake_word.model not in SUPPORTED_WAKE_WORD_MODELS:
        supported = ", ".join(sorted(SUPPORTED_WAKE_WORD_MODELS))
        raise ConfigError(f"{path}: wake_word.model must be one of: {supported}, or a path to a .onnx/.tflite model.")
    if wake_word_active and wake_word.model_path is not None and not wake_word.model_path.exists():
        raise ConfigError(f"{path}: wake_word.model path does not exist: {wake_word.model_path}.")
    if wake_word_active and wake_word.model_path is not None and wake_word.model_path.suffix not in {".onnx", ".tflite"}:
        raise ConfigError(f"{path}: wake_word.model path must end in .onnx or .tflite.")
    if not 0 <= wake_word.threshold <= 1:
        raise ConfigError(f"{path}: wake_word.threshold must be between 0 and 1.")

    return AudioConfig(
        host=_string(grpc_config, "host", "0.0.0.0"),
        port=_integer(grpc_config, "port", 50052),
        piper_binary=_string(piper_config, "binary", "piper"),
        piper_model=piper_model,
        piper_extra_args=_string_list(piper_config, "extra_args"),
        playback_command=_string(playback_config, "command", "aplay"),
        speaker_device=_optional_string(playback_config, "speaker_device"),
        vtt=vtt,
        wake_word=wake_word,
        microphone=MicrophoneConfig(
            device=_none_if_default(_string(microphone_config, "device", "default")),
        ),
        sounds=SoundsConfig(
            wake_triggered=_path(path, _string(sounds_config, "wake_triggered", "sounds/switch_005.ogg")),
            recording_sent=_path(path, _string(sounds_config, "recording_sent", "sounds/switch_007.ogg")),
            playback_command=_optional_string(sounds_config, "command"),
        ),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            parsed = tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: config root must be a TOML table.")
    return parsed


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a TOML table.")
    return value


def _vtt_config(path: Path, data: dict[str, Any]) -> VttConfig:
    _reject_legacy_vtt_keys(path, data)
    enabled = _boolean(data, "enabled", True)
    trigger = _string(data, "trigger", "wake_word")
    if trigger not in {"wake_word", "voice_activity"}:
        raise ConfigError(f"{path}: vtt.trigger must be \"wake_word\" or \"voice_activity\".")
    command_prefixes = _string_list(data, "command_prefixes") if "command_prefixes" in data else ("robot",)
    if any(not prefix.strip() for prefix in command_prefixes):
        raise ConfigError(f"{path}: vtt.command_prefixes must not contain blank strings.")
    command_prefixes = tuple(prefix.strip() for prefix in command_prefixes)
    if enabled and trigger == "voice_activity" and not command_prefixes:
        raise ConfigError(f"{path}: vtt.command_prefixes must define at least one prefix for voice_activity.")
    voice_activity_rms_threshold = _float(data, "voice_activity_rms_threshold", 250.0)
    if voice_activity_rms_threshold <= 0:
        raise ConfigError(f"{path}: vtt.voice_activity_rms_threshold must be greater than 0.")
    voice_activity_cooldown_seconds = _float(data, "voice_activity_cooldown_seconds", 0.8)
    if voice_activity_cooldown_seconds < 0:
        raise ConfigError(f"{path}: vtt.voice_activity_cooldown_seconds must be at least 0.")
    voice_activity_preroll_seconds = _float(data, "voice_activity_preroll_seconds", 0.8)
    if voice_activity_preroll_seconds < 0:
        raise ConfigError(f"{path}: vtt.voice_activity_preroll_seconds must be at least 0.")
    selected_backend = _string(data, "selected_backend", "") if enabled else _optional_string(data, "selected_backend") or ""
    backends = _table(data, "backends")

    if not enabled:
        return VttConfig(
            enabled=enabled,
            selected_backend=selected_backend,
            backend=None,
            trigger=trigger,
            command_prefixes=command_prefixes,
            voice_activity_rms_threshold=voice_activity_rms_threshold,
            voice_activity_cooldown_seconds=voice_activity_cooldown_seconds,
            voice_activity_preroll_seconds=voice_activity_preroll_seconds,
        )
    if not backends:
        raise ConfigError(f"{path}: vtt.backends must define at least one backend when VTT is enabled.")
    if selected_backend not in backends:
        available = ", ".join(sorted(backends))
        raise ConfigError(f"{path}: vtt.selected_backend must name one of vtt.backends: {available}.")

    backend_config = backends[selected_backend]
    if not isinstance(backend_config, dict):
        raise ConfigError(f"{path}: vtt.backends.{selected_backend} must be a TOML table.")

    backend = _vtt_backend_config(path, selected_backend, backend_config)
    return VttConfig(
        enabled=enabled,
        selected_backend=selected_backend,
        backend=backend,
        trigger=trigger,
        command_prefixes=command_prefixes,
        voice_activity_rms_threshold=voice_activity_rms_threshold,
        voice_activity_cooldown_seconds=voice_activity_cooldown_seconds,
        voice_activity_preroll_seconds=voice_activity_preroll_seconds,
    )


def _reject_legacy_vtt_keys(path: Path, data: dict[str, Any]) -> None:
    legacy_keys = {
        "backend",
        "model",
        "language",
        "compute_type",
        "beam_size",
        "vad_filter",
        "riva_uri",
        "riva_language_code",
        "riva_model",
        "riva_automatic_punctuation",
        "riva_use_ssl",
    }
    used_keys = sorted(legacy_keys.intersection(data))
    if used_keys:
        keys = ", ".join(used_keys)
        raise ConfigError(f"{path}: move legacy vtt keys to named backend profiles under [vtt.backends.*]: {keys}.")


def _vtt_backend_config(path: Path, name: str, data: dict[str, Any]) -> VttBackendConfig:
    backend_type = _string(data, "type", "")
    if backend_type == "faster-whisper":
        beam_size = _integer(data, "beam_size", 5)
        if beam_size < 1:
            raise ConfigError(f"{path}: vtt.backends.{name}.beam_size must be at least 1.")
        return FasterWhisperVttBackendConfig(
            type=backend_type,
            model=_string(data, "model", "small"),
            language=_optional_string(data, "language") or "en",
            compute_type=_string(data, "compute_type", "float32"),
            beam_size=beam_size,
            vad_filter=_boolean(data, "vad_filter", False),
        )
    if backend_type == "riva":
        return RivaVttBackendConfig(
            type=backend_type,
            uri=_string(data, "uri", "localhost:50051"),
            language_code=_string(data, "language_code", "en-US"),
            model=_string(data, "model", "parakeet-1.1b-en-us-asr-streaming"),
            automatic_punctuation=_boolean(data, "automatic_punctuation", True),
            use_ssl=_boolean(data, "use_ssl", False),
        )
    raise ConfigError(f"{path}: vtt.backends.{name}.type must be \"faster-whisper\" or \"riva\".")


def _string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string.")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string when set.")
    return value


def _integer(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer.")
    return value


def _float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number.")
    return float(value)


def _boolean(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean.")
    return value


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings.")
    return tuple(value)


def _path(config_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return config_path.parent / path


def _none_if_default(value: str) -> str | None:
    if value == "default":
        return None
    return value


def _wake_word_model_path(config_path: Path, model: str) -> Path | None:
    if model in SUPPORTED_WAKE_WORD_MODELS:
        return None
    if model.endswith((".onnx", ".tflite")) or "/" in model:
        return _path(config_path, model)
    return None
