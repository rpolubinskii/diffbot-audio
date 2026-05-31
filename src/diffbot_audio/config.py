from __future__ import annotations

import argparse
import os
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioConfig:
    host: str
    port: int
    piper_binary: str
    piper_model: str | None
    piper_extra_args: tuple[str, ...]
    playback_command: str
    speaker_device: str | None


def load_config(argv: list[str] | None = None) -> AudioConfig:
    parser = argparse.ArgumentParser(description="Run the DiffBot audio gRPC service.")
    parser.add_argument("--host", default=os.getenv("DIFFBOT_AUDIO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DIFFBOT_AUDIO_PORT", "50052")))
    parser.add_argument("--piper-binary", default=os.getenv("DIFFBOT_AUDIO_PIPER_BINARY", "piper"))
    parser.add_argument("--piper-model", default=os.getenv("DIFFBOT_AUDIO_PIPER_MODEL"))
    parser.add_argument(
        "--piper-extra-args",
        default=os.getenv("DIFFBOT_AUDIO_PIPER_EXTRA_ARGS", ""),
        help="Extra Piper CLI arguments, parsed with shell-style quoting.",
    )
    parser.add_argument("--playback-command", default=os.getenv("DIFFBOT_AUDIO_PLAYBACK_COMMAND", "aplay"))
    parser.add_argument("--speaker-device", default=os.getenv("DIFFBOT_AUDIO_SPEAKER_DEVICE"))
    args = parser.parse_args(argv)

    return AudioConfig(
        host=args.host,
        port=args.port,
        piper_binary=args.piper_binary,
        piper_model=args.piper_model,
        piper_extra_args=tuple(shlex.split(args.piper_extra_args)),
        playback_command=args.playback_command,
        speaker_device=args.speaker_device,
    )
