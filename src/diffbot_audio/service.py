from __future__ import annotations

import logging
import sys
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from concurrent import futures
from pathlib import Path
from typing import Iterable

import grpc

from diffbot_audio.config import AudioConfig, ConfigError, load_config
from diffbot_audio.proto import audio_pb2

LOGGER = logging.getLogger("diffbot_audio")


class PiperAudioService:
    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._speaking_lock = threading.Lock()
        self._is_speaking = False

    @property
    def is_speaking(self) -> bool:
        with self._speaking_lock:
            return self._is_speaking

    def speak(
        self,
        request: audio_pb2.SpeakRequest,
        context: grpc.ServicerContext,
    ) -> Iterable[audio_pb2.SpeakEvent]:
        text = request.text.strip()
        if not text:
            yield _failed("SpeakRequest.text must not be blank.")
            return

        try:
            with tempfile.TemporaryDirectory(prefix="diffbot-audio-") as tmp_dir:
                wav_path = Path(tmp_dir) / "speech.wav"
                self._synthesize(text, wav_path)
                with self._speaking():
                    process = self._start_playback(wav_path)
                    yield audio_pb2.SpeakEvent(state=audio_pb2.STARTED)
                    stdout, stderr = process.communicate()
                    if process.returncode != 0:
                        yield _failed(_format_process_error("playback", process.returncode, stdout, stderr))
                        return
                yield audio_pb2.SpeakEvent(state=audio_pb2.FINISHED)
        except Exception as exc:
            LOGGER.exception("Speak failed")
            yield _failed(str(exc))

    def _synthesize(self, text: str, wav_path: Path) -> None:
        command = [
            self._config.piper_binary,
            "--model",
            self._config.piper_model,
            "--output_file",
            str(wav_path),
            *self._config.piper_extra_args,
        ]
        LOGGER.info("Running Piper synthesis")
        result = subprocess.run(
            command,
            input=text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(_format_process_error("piper", result.returncode, result.stdout, result.stderr))
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            raise RuntimeError("Piper did not produce a non-empty WAV file.")

    def _start_playback(self, wav_path: Path) -> subprocess.Popen[str]:
        command = _playback_command(
            self._config.playback_command,
            wav_path,
            self._config.speaker_device,
        )
        LOGGER.info("Starting playback")
        return subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _speaking(self) -> "_SpeakingFlag":
        return _SpeakingFlag(self)


class _SpeakingFlag:
    def __init__(self, service: PiperAudioService) -> None:
        self._service = service

    def __enter__(self) -> None:
        with self._service._speaking_lock:
            self._service._is_speaking = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        with self._service._speaking_lock:
            self._service._is_speaking = False


def create_server(config: AudioConfig) -> grpc.Server:
    service = PiperAudioService(config)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    method_handlers = {
        "Speak": grpc.unary_stream_rpc_method_handler(
            service.speak,
            request_deserializer=audio_pb2.SpeakRequest.FromString,
            response_serializer=audio_pb2.SpeakEvent.SerializeToString,
        )
    }
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("diffbot.audio.v1.AudioService", method_handlers),)
    )
    listen_addr = f"{config.host}:{config.port}"
    bound_port = server.add_insecure_port(listen_addr)
    if bound_port == 0:
        raise RuntimeError(f"Failed to bind gRPC server on {listen_addr}.")
    return server


def serve(config: AudioConfig) -> None:
    server = create_server(config)
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s, stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    server.start()
    LOGGER.info("diffbot-audio listening on %s:%s", config.host, config.port)
    try:
        while not stop_event.wait(timeout=0.5):
            time.sleep(0)
    finally:
        server.stop(grace=5).wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        LOGGER.error("Invalid config: %s", exc)
        sys.exit(2)
    serve(config)


def _failed(error: str) -> audio_pb2.SpeakEvent:
    return audio_pb2.SpeakEvent(state=audio_pb2.FAILED, error=error)


def _playback_command(playback_command: str, wav_path: Path, speaker_device: str | None) -> list[str]:
    parts = shlex.split(playback_command)
    if not parts:
        raise RuntimeError("Playback command must not be blank.")

    file_value = str(wav_path)
    device_value = speaker_device or ""
    if any("{file}" in part or "{device}" in part for part in parts):
        return [part.format(file=file_value, device=device_value) for part in parts]

    executable = Path(parts[0]).name
    if executable == "aplay" and speaker_device:
        return [*parts, "-D", speaker_device, file_value]
    if executable == "paplay" and speaker_device:
        return [*parts, f"--device={speaker_device}", file_value]
    return [*parts, file_value]


def _format_process_error(label: str, returncode: int, stdout: str | None, stderr: str | None) -> str:
    output = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if output:
        output = output[-1000:]
        return f"{label} exited with code {returncode}: {output}"
    return f"{label} exited with code {returncode}."


if __name__ == "__main__":
    main()
