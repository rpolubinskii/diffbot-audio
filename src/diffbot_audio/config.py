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

    piper_model = _optional_string(piper_config, "model")
    if not piper_model:
        raise ConfigError(f"{path}: piper.model is required.")

    return AudioConfig(
        host=_string(grpc_config, "host", "0.0.0.0"),
        port=_integer(grpc_config, "port", 50052),
        piper_binary=_string(piper_config, "binary", "piper"),
        piper_model=piper_model,
        piper_extra_args=_string_list(piper_config, "extra_args"),
        playback_command=_string(playback_config, "command", "aplay"),
        speaker_device=_optional_string(playback_config, "speaker_device"),
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
    if not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer.")
    return value


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings.")
    return tuple(value)
