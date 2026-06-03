from __future__ import annotations

import logging
import queue
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Callable

from diffbot_audio.config import (
    AudioConfig,
    FasterWhisperVttBackendConfig,
    RivaVttBackendConfig,
    VttBackendConfig,
)

LOGGER = logging.getLogger("diffbot_audio.vtt")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280
SILENCE_RMS = 500.0
SILENCE_SECONDS = 1.2
START_TIMEOUT_SECONDS = 5.0
MAX_RECORDING_SECONDS = 12.0
MIN_VOICE_FRAMES = 2
TRANSCRIPTION_QUEUE_SIZE = 4


class VoiceCommandWorker:
    def __init__(
        self,
        config: AudioConfig,
        is_speaking: Callable[[], bool],
        play_sound: Callable[[Path], None],
        emit_text: Callable[[str], None],
        emit_error: Callable[[str], None],
    ) -> None:
        self._config = config
        self._is_speaking = is_speaking
        self._play_sound = play_sound
        self._emit_text = emit_text
        self._emit_error = emit_error
        self._stop_event = threading.Event()
        self._asr_busy_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._asr_thread: threading.Thread | None = None
        self._transcription_queue: queue.Queue[list[object]] = queue.Queue(maxsize=TRANSCRIPTION_QUEUE_SIZE)
        self._wake_model: object | None = None
        self._asr_backend: AsrBackend | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._config.vtt.trigger == "wake_word":
            self._wake_model = self._load_wake_model()
        if self._config.vtt.backend is None:
            raise RuntimeError("VTT is enabled, but no VTT backend profile is selected.")
        self._asr_backend = _load_asr_backend(self._config.vtt.backend)
        self._thread = threading.Thread(target=self._run, name="diffbot-vtt", daemon=True)
        self._asr_thread = threading.Thread(target=self._run_transcription, name="diffbot-vtt-asr", daemon=True)
        self._asr_thread.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._asr_thread is not None:
            self._asr_thread.join(timeout=2)

    def _load_wake_model(self) -> object:
        try:
            import openwakeword
            from openwakeword.model import Model
            from openwakeword.utils import download_models
        except ImportError as exc:
            raise RuntimeError("VTT requires openwakeword. Run `uv sync` to install dependencies.") from exc

        _quiet_onnxruntime()
        model_name = self._config.wake_word.model
        model_path = self._config.wake_word.model_path
        if model_path is None and model_name not in openwakeword.MODELS:
            available = ", ".join(sorted(openwakeword.MODELS))
            raise RuntimeError(f"openWakeWord model {model_name!r} is unavailable. Available models: {available}.")

        try:
            if model_path is None:
                download_models(model_names=[model_name])
                return Model(wakeword_models=[model_name])

            download_models(model_names=["__diffbot_custom__"])
            inference_framework = model_path.suffix.removeprefix(".").lower()
            return Model(wakeword_models=[str(model_path)], inference_framework=inference_framework)
        except Exception as exc:
            raise RuntimeError(f"Failed to load openWakeWord model {model_name!r}: {exc}") from exc

    def _run(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            self._emit_error("VTT requires sounddevice. Run `uv sync` to install dependencies.")
            return

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                device=self._config.microphone.device,
            ) as stream:
                LOGGER.info("VTT microphone loop started")
                preroll_frames: deque[object] = deque(maxlen=self._voice_activity_preroll_blocks)
                while not self._stop_event.is_set():
                    frame = self._read_frame(stream)
                    if frame is None:
                        continue
                    if self._voice_activity_backpressure_active():
                        continue
                    if self._is_speaking():
                        self._reset_wake_model()
                        preroll_frames.clear()
                        continue
                    if self._config.vtt.trigger == "wake_word" and self._wake_word_triggered(frame):
                        self._handle_wake_word(stream)
                    elif self._config.vtt.trigger == "voice_activity" and self._voice_activity_triggered(frame):
                        self._handle_voice_activity(stream, list(preroll_frames), frame)
                        preroll_frames.clear()
                    elif self._config.vtt.trigger == "voice_activity":
                        preroll_frames.append(frame)
        except Exception as exc:
            LOGGER.exception("VTT microphone loop failed")
            self._emit_error(f"VTT microphone loop failed: {exc}")

    def _read_frame(self, stream: object) -> object | None:
        import numpy as np

        data, overflowed = stream.read(FRAME_SAMPLES)
        if overflowed:
            LOGGER.warning("Microphone input overflowed")
        if self._stop_event.is_set():
            return None
        return np.asarray(data, dtype=np.int16).reshape(-1).copy()

    def _wake_word_triggered(self, frame: object) -> bool:
        assert self._wake_model is not None
        predictions = self._wake_model.predict(frame)
        score = _prediction_score(predictions, self._config.wake_word.model)
        if score >= self._config.wake_word.threshold:
            LOGGER.info("Wake word triggered with score %.3f", score)
            self._reset_wake_model()
            return True
        return False

    def _voice_activity_triggered(self, frame: object) -> bool:
        rms = _rms(frame)
        if rms >= self._silence_rms:
            LOGGER.info("Voice activity triggered with RMS %.1f", rms)
            return True
        return False

    def _handle_wake_word(self, stream: object) -> None:
        self._play_sound_async(self._config.sounds.wake_triggered, "Wake notification sound failed")

        frames = self._record_utterance(stream)
        if not frames:
            LOGGER.info("Wake word timed out before speech")
            return

        self._play_sound_async(self._config.sounds.recording_sent, "Processing notification sound failed")
        self._queue_transcription(frames)

    def _handle_voice_activity(self, stream: object, preroll_frames: list[object], initial_frame: object) -> None:
        frames = self._record_utterance(
            stream,
            initial_frames=[*preroll_frames, initial_frame],
            trim_front=False,
        )
        if not frames:
            LOGGER.info("Voice activity did not produce enough speech")
            self._cool_down_voice_activity()
            return

        self._queue_transcription(frames)
        self._cool_down_voice_activity()

    def _play_sound_async(self, sound_path: Path, error_prefix: str) -> None:
        thread = threading.Thread(
            target=self._play_sound_safely,
            args=(sound_path, error_prefix),
            name="diffbot-vtt-sound",
            daemon=True,
        )
        thread.start()

    def _play_sound_safely(self, sound_path: Path, error_prefix: str) -> None:
        try:
            self._play_sound(sound_path)
        except Exception as exc:
            LOGGER.exception(error_prefix)
            self._emit_error(f"{error_prefix}: {exc}")

    def _queue_transcription(self, frames: list[object]) -> None:
        try:
            self._transcription_queue.put_nowait(frames)
        except queue.Full:
            LOGGER.warning("Dropping voice command because transcription queue is full")
            self._emit_error("Transcription queue is full.")

    def _run_transcription(self) -> None:
        while not self._stop_event.is_set():
            try:
                frames = self._transcription_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._asr_busy_event.set()
                text = self._transcribe(frames)
            except Exception as exc:
                LOGGER.exception("Transcription failed")
                self._emit_error(f"Transcription failed: {exc}")
                continue
            finally:
                self._asr_busy_event.clear()

            if not text:
                self._emit_error("Transcription produced no text.")
                continue

            LOGGER.info("Raw transcript: %s", text)
            command_text = self._command_text(text)
            if command_text:
                LOGGER.info("Voice command transcribed: %s", command_text)
                self._emit_text(command_text)
            elif self._config.vtt.trigger == "voice_activity":
                LOGGER.info("Ignoring transcript without command prefix: %s", text)

    def _record_utterance(
        self,
        stream: object,
        initial_frames: list[object] | None = None,
        trim_front: bool = True,
    ) -> list[object]:
        frames = []
        voice_frames = 0
        silence_frames = 0
        heard_voice = False
        started_at = time.monotonic()
        silence_limit = max(1, int(SILENCE_SECONDS / (FRAME_SAMPLES / SAMPLE_RATE)))

        def add_frame(frame: object) -> None:
            nonlocal heard_voice, silence_frames, voice_frames

            rms = _rms(frame)
            if rms >= self._silence_rms:
                heard_voice = True
                voice_frames += 1
                silence_frames = 0
            elif heard_voice:
                silence_frames += 1
            frames.append(frame)

        if initial_frames is not None:
            for initial_frame in initial_frames:
                add_frame(initial_frame)

        while not self._stop_event.is_set():
            if self._is_speaking():
                return []

            frame = self._read_frame(stream)
            if frame is None:
                continue

            add_frame(frame)
            elapsed = time.monotonic() - started_at
            if not heard_voice and elapsed >= START_TIMEOUT_SECONDS:
                return []
            if heard_voice and silence_frames >= silence_limit:
                break
            if elapsed >= MAX_RECORDING_SECONDS:
                break

        if voice_frames < MIN_VOICE_FRAMES:
            return []
        return _trim_silence(frames, self._silence_rms, trim_front=trim_front)

    def _transcribe(self, frames: list[object]) -> str:
        assert self._asr_backend is not None
        return self._asr_backend.transcribe(frames)

    def _command_text(self, text: str) -> str:
        text = text.strip()
        if self._config.vtt.trigger != "voice_activity":
            return text
        command_text = _strip_command_prefix(text, self._config.vtt.command_prefixes)
        if command_text:
            LOGGER.info("Command prefix matched")
        else:
            LOGGER.info("Command prefix did not match")
        return command_text

    @property
    def _silence_rms(self) -> float:
        if self._config.vtt.trigger == "voice_activity":
            return self._config.vtt.voice_activity_rms_threshold
        return SILENCE_RMS

    def _voice_activity_backpressure_active(self) -> bool:
        if self._config.vtt.trigger != "voice_activity":
            return False
        return self._asr_busy_event.is_set() or not self._transcription_queue.empty()

    def _cool_down_voice_activity(self) -> None:
        cooldown = self._config.vtt.voice_activity_cooldown_seconds
        if cooldown > 0:
            time.sleep(cooldown)

    @property
    def _voice_activity_preroll_blocks(self) -> int:
        seconds_per_block = FRAME_SAMPLES / SAMPLE_RATE
        return max(0, int(self._config.vtt.voice_activity_preroll_seconds / seconds_per_block))

    def _drain_stream(self, stream: object, blocks: int) -> None:
        for _ in range(blocks):
            if self._stop_event.is_set():
                return
            try:
                stream.read(FRAME_SAMPLES)
            except Exception:
                LOGGER.debug("Failed to drain microphone stream", exc_info=True)
                return

    def _reset_wake_model(self) -> None:
        if self._wake_model is not None:
            self._wake_model.reset()


class AsrBackend(ABC):
    @abstractmethod
    def transcribe(self, frames: list[object]) -> str:
        pass


class FasterWhisperAsrBackend(AsrBackend):
    def __init__(self, config: FasterWhisperVttBackendConfig) -> None:
        self._config = config
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("VTT requires faster-whisper. Run `uv sync` to install dependencies.") from exc

        try:
            self._model = WhisperModel(config.model, device="auto", compute_type=config.compute_type)
        except Exception as exc:
            raise RuntimeError(f"Failed to load faster-whisper model {config.model!r}: {exc}") from exc

    def transcribe(self, frames: list[object]) -> str:
        import numpy as np

        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language=self._config.language,
            beam_size=self._config.beam_size,
            vad_filter=self._config.vad_filter,
            vad_parameters={"min_silence_duration_ms": 500} if self._config.vad_filter else None,
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()


class RivaAsrBackend(AsrBackend):
    def __init__(self, config: RivaVttBackendConfig) -> None:
        self._config = config
        try:
            import riva.client
            import riva.client.proto.riva_asr_pb2 as rasr
        except ImportError as exc:
            raise RuntimeError("VTT backend 'riva' requires nvidia-riva-client. Run `uv sync` to install dependencies.") from exc

        self._riva_client = riva.client
        self._rasr = rasr

        try:
            auth = riva.client.Auth(uri=config.uri, use_ssl=config.use_ssl)
            self._service = riva.client.ASRService(auth)
            self._auth_metadata = auth.get_auth_metadata()
            self._validate_server_model()
        except Exception as exc:
            raise RuntimeError(f"Failed to connect to Riva ASR at {config.uri!r}: {exc}") from exc

    def transcribe(self, frames: list[object]) -> str:
        import numpy as np

        audio = np.concatenate(frames).astype(np.int16).tobytes()
        transcripts = []
        responses = self._service.streaming_response_generator(
            _chunk_bytes(audio, FRAME_SAMPLES * 2),
            self._streaming_recognition_config(),
        )
        for response in responses:
            for result in response.results:
                if result.is_final and result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        transcripts.append(transcript)
        return " ".join(transcripts).strip()

    def _validate_server_model(self) -> None:
        request = self._rasr.RivaSpeechRecognitionConfigRequest(model_name=self._config.model)
        response = self._service.stub.GetRivaSpeechRecognitionConfig(request, metadata=self._auth_metadata)
        model_names = [model.model_name for model in response.model_config]
        if not model_names:
            raise RuntimeError(f"Riva ASR model {self._config.model!r} is unavailable.")
        LOGGER.info("Riva ASR model available: %s", ", ".join(model_names))

    def _recognition_config(self) -> object:
        config = self._riva_client.RecognitionConfig()
        config.encoding = self._riva_client.AudioEncoding.LINEAR_PCM
        config.sample_rate_hertz = SAMPLE_RATE
        config.language_code = self._config.language_code
        config.max_alternatives = 1
        config.audio_channel_count = 1
        config.enable_automatic_punctuation = self._config.automatic_punctuation
        config.model = self._config.model
        return config

    def _streaming_recognition_config(self) -> object:
        config = self._riva_client.StreamingRecognitionConfig()
        config.config.CopyFrom(self._recognition_config())
        config.interim_results = False
        return config


def _load_asr_backend(config: VttBackendConfig) -> AsrBackend:
    if config.type == "faster-whisper":
        return FasterWhisperAsrBackend(config)
    if config.type == "riva":
        return RivaAsrBackend(config)
    raise RuntimeError(f"Unsupported VTT backend: {config.type}.")


def _chunk_bytes(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[index : index + chunk_size] for index in range(0, len(data), chunk_size)]


def _strip_command_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    for prefix in sorted(prefixes, key=len, reverse=True):
        pattern = rf"^\s*{re.escape(prefix)}(?:\s*[,.:;-]\s*|\s+|$)(.*)$"
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _prediction_score(predictions: dict[str, float], model_name: str) -> float:
    if model_name in predictions:
        return float(predictions[model_name])
    prefix = f"{model_name}_"
    prefixed_scores = [float(score) for label, score in predictions.items() if label.startswith(prefix)]
    if prefixed_scores:
        return max(prefixed_scores)
    return max((float(score) for score in predictions.values()), default=0.0)


def _quiet_onnxruntime() -> None:
    try:
        import onnxruntime as ort
    except ImportError:
        return
    ort.set_default_logger_severity(3)


def _rms(frame: object) -> float:
    import numpy as np

    samples = np.asarray(frame, dtype=np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def _trim_silence(frames: list[object], threshold: float = SILENCE_RMS, trim_front: bool = True) -> list[object]:
    first = 0
    last = len(frames)
    if trim_front:
        while first < last and _rms(frames[first]) < threshold:
            first += 1
    while last > first and _rms(frames[last - 1]) < threshold:
        last -= 1
    return frames[first:last]
