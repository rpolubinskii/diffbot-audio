from __future__ import annotations

import logging
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent import futures
from pathlib import Path
from typing import Iterable

import grpc

from diffbot_audio.config import AudioConfig, ConfigError, load_config
from diffbot_audio.proto import audio_pb2
from diffbot_audio.vtt import VoiceCommandWorker

LOGGER = logging.getLogger("diffbot_audio")


class PiperAudioService:
    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._speaking_lock = threading.Lock()
        self._is_speaking = False
        self._speaking_count = 0
        self._voice_commands = _VoiceCommandBroadcaster()
        self._vtt_worker: VoiceCommandWorker | None = None
        if self._config.vtt.enabled:
            self._vtt_worker = VoiceCommandWorker(
                config=self._config,
                is_speaking=lambda: self.is_speaking,
                play_sound=lambda path: self.play_sound(path, mark_speaking=False),
                emit_text=self._voice_commands.emit_text,
                emit_error=self._voice_commands.emit_error,
            )
            self._vtt_worker.start()

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

    def stream_voice_commands(
        self,
        _request: audio_pb2.StreamVoiceCommandsRequest,
        context: grpc.ServicerContext,
    ) -> Iterable[audio_pb2.VoiceCommandEvent]:
        if not self._config.vtt.enabled:
            yield audio_pb2.VoiceCommandEvent(error="VTT is disabled.")
            return

        subscriber = self._voice_commands.subscribe()
        try:
            while context.is_active():
                try:
                    yield subscriber.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            self._voice_commands.unsubscribe(subscriber)

    def stop(self) -> None:
        if self._vtt_worker is not None:
            self._vtt_worker.stop()

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

    def play_sound(self, sound_path: Path, mark_speaking: bool = True) -> None:
        if not sound_path.exists():
            raise RuntimeError(f"Sound file does not exist: {sound_path}")
        if mark_speaking:
            with self._speaking():
                self._play_sound_process(sound_path)
            return
        self._play_sound_process(sound_path)

    def _play_sound_process(self, sound_path: Path) -> None:
        process = self._start_sound_playback(sound_path)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise RuntimeError(_format_process_error("sound playback", process.returncode, stdout, stderr))

    def _start_sound_playback(self, sound_path: Path) -> subprocess.Popen[str]:
        command = _playback_command(
            _sound_playback_command(self._config, sound_path),
            sound_path,
            self._config.speaker_device,
        )
        LOGGER.info("Starting notification sound playback")
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
            self._service._speaking_count += 1
            self._service._is_speaking = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        with self._service._speaking_lock:
            self._service._speaking_count = max(0, self._service._speaking_count - 1)
            self._service._is_speaking = self._service._speaking_count > 0


def create_server(config: AudioConfig) -> grpc.Server:
    service = PiperAudioService(config)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    method_handlers = {
        "Speak": grpc.unary_stream_rpc_method_handler(
            service.speak,
            request_deserializer=audio_pb2.SpeakRequest.FromString,
            response_serializer=audio_pb2.SpeakEvent.SerializeToString,
        ),
        "StreamVoiceCommands": grpc.unary_stream_rpc_method_handler(
            service.stream_voice_commands,
            request_deserializer=audio_pb2.StreamVoiceCommandsRequest.FromString,
            response_serializer=audio_pb2.VoiceCommandEvent.SerializeToString,
        )
    }
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("diffbot.audio.v1.AudioService", method_handlers),)
    )
    listen_addr = f"{config.host}:{config.port}"
    bound_port = server.add_insecure_port(listen_addr)
    if bound_port == 0:
        service.stop()
        raise RuntimeError(f"Failed to bind gRPC server on {listen_addr}.")
    setattr(server, "_diffbot_audio_service", service)
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
        service = getattr(server, "_diffbot_audio_service", None)
        if service is not None:
            service.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    try:
        config = load_config()
    except ConfigError as exc:
        LOGGER.error("Invalid config: %s", exc)
        sys.exit(2)
    try:
        serve(config)
    except RuntimeError as exc:
        LOGGER.error("Startup failed: %s", exc)
        sys.exit(1)


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


def _sound_playback_command(config: AudioConfig, sound_path: Path) -> str:
    if config.sounds.playback_command:
        return config.sounds.playback_command
    if sound_path.suffix.lower() == ".ogg" and _playback_executable(config.playback_command) == "aplay":
        return "paplay"
    return config.playback_command


def _playback_executable(playback_command: str) -> str:
    parts = shlex.split(playback_command)
    if not parts:
        return ""
    return Path(parts[0]).name


class _VoiceCommandBroadcaster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[audio_pb2.VoiceCommandEvent]] = set()

    def subscribe(self) -> queue.Queue[audio_pb2.VoiceCommandEvent]:
        subscriber: queue.Queue[audio_pb2.VoiceCommandEvent] = queue.Queue(maxsize=16)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[audio_pb2.VoiceCommandEvent]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def emit_text(self, text: str) -> None:
        self._emit(audio_pb2.VoiceCommandEvent(text=text))

    def emit_error(self, error: str) -> None:
        self._emit(audio_pb2.VoiceCommandEvent(error=error))

    def _emit(self, event: audio_pb2.VoiceCommandEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                LOGGER.warning("Dropping voice command event for a slow subscriber")


def _format_process_error(label: str, returncode: int, stdout: str | None, stderr: str | None) -> str:
    output = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if output:
        output = output[-1000:]
        return f"{label} exited with code {returncode}: {output}"
    return f"{label} exited with code {returncode}."


if __name__ == "__main__":
    main()
