"""Optional sherpa-onnx adapters for the Go2 voice service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import inspect
import tempfile
from typing import Any, Sequence


class VoiceEngineError(RuntimeError):
    """A required inference dependency or model resource is unavailable."""


_KEYWORD_BOOST = 3.0
_KEYWORD_THRESHOLD = 0.1
_KEYWORD_TRAILING_BLANKS = 0
_KEYWORD_ACTIVE_PATHS = 8


def keyword_line(tokens: Sequence[str], phrase: str, *, score: float, threshold: float) -> str:
    """Build one sherpa-onnx keyword line: tokens, boost score, threshold, label."""
    return " ".join(tokens) + f" :{float(score)} #{float(threshold)} @{phrase.replace(' ', '_')}"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise VoiceEngineError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    value = int(_env_float(name, float(default)))
    if value < 1:
        raise VoiceEngineError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class AsrResult:
    text: str
    is_endpoint: bool


@dataclass(frozen=True)
class SynthesizedAudio:
    samples: tuple[float, ...]
    sample_rate: int


def _model_dir(root: Path, name: str, required: Sequence[str]) -> tuple[Path, dict[str, Path]]:
    base = root / "models" / name
    if not base.is_dir():
        raise VoiceEngineError(f"Missing {name} model directory: {base}")
    files: dict[str, Path] = {}
    for filename in required:
        matches = sorted(base.rglob(filename))
        if not matches:
            raise VoiceEngineError(f"Missing {name} model file {filename} under {base}")
        if len(matches) > 1:
            raise VoiceEngineError(f"Ambiguous {name} model file {filename} under {base}")
        files[filename] = matches[0]
    return base, files


def _fallback_phone_pinyin(text: str, tokens_path: Path) -> list[str]:
    """Encode a Chinese wake phrase without sherpa's optional sentencepiece.

    sherpa's ``text2token`` imports sentencepiece unconditionally in some
    releases even for ``phone+ppinyin``.  The phone+partial-pinyin path only
    needs pypinyin and the model token table, so keep a small deterministic
    fallback for Alpine deployments where sentencepiece has no wheel.
    """
    try:
        from pypinyin import pinyin
        from pypinyin.contrib.tone_convert import to_finals_tone, to_initials
    except ImportError as exc:
        raise VoiceEngineError("pypinyin is required to encode the configured wake phrase") from exc
    token_names = {line.split()[0] for line in tokens_path.read_text(encoding="utf-8").splitlines() if line.split()}
    result: list[str] = []
    for item in pinyin(text.strip()):
        syllable = item[0]
        initial = to_initials(syllable, strict=False)
        final = to_finals_tone(syllable, strict=False)
        for token in (initial, final):
            if token and token in token_names:
                result.append(token)
            elif token:
                raise VoiceEngineError(f"Wake phrase token {token!r} is absent from {tokens_path}")
    return result


class SherpaVoiceEngine:
    """Local wake word and VAD. Recognition and speech synthesis go through the speech API."""

    def __init__(
        self,
        resource_root: str | Path,
        wake_phrase: str | Sequence[str],
        sample_rate: int = 16000,
        speech: Any = None,
        language: str = "zh-CN",
    ):
        if isinstance(wake_phrase, str):
            phrases = tuple(phrase.strip() for phrase in wake_phrase.split(",") if phrase.strip())
        else:
            phrases = tuple(phrase.strip() for phrase in wake_phrase if isinstance(phrase, str) and phrase.strip())
        if not phrases:
            raise ValueError("wake_phrase must not be empty")
        self.resource_root = Path(resource_root)
        self.wake_phrases = phrases
        self.wake_phrase = phrases[0]
        self.sample_rate = sample_rate
        self.language = language
        self._speech = speech
        self._sherpa: Any = None
        self._kws: Any = None
        self._kws_stream: Any = None
        self._vad: Any = None
        self._keywords_file: Any = None

    def load(self) -> None:
        _voice_log("Info", "开始加载语音模型")
        try:
            self._load_models()
        except VoiceEngineError as exc:
            _voice_log("Error", f"语音模型加载失败：{exc}")
            raise
        _voice_log("Info", "语音模型加载完成")

    def _load_models(self) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise VoiceEngineError("sherpa-onnx is not installed; install the pinned ARM64 CPU runtime and Python bindings") from exc
        try:
            import numpy as np
        except ImportError as exc:
            raise VoiceEngineError("NumPy is required by sherpa-onnx audio inference") from exc

        _, kws = _model_dir(self.resource_root, "kws-zh-en-3m", (
            "tokens.txt", "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
            "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx", "en.phone",
        ))
        _, vad_files = _model_dir(self.resource_root, "vad-silero-int8", ("silero_vad.int8.onnx",))

        try:
            _voice_log("Info", "开始加载唤醒模型")
            encoded_rows: list[tuple[list[str], str]] = []
            for phrase in self.wake_phrases:
                try:
                    encoded = sherpa_onnx.text2token(
                        [phrase],
                        tokens=str(kws["tokens.txt"]), tokens_type="phone+ppinyin", lexicon=str(kws["en.phone"]),
                    )
                except ModuleNotFoundError as exc:
                    if exc.name != "sentencepiece":
                        raise
                    encoded = [_fallback_phone_pinyin(phrase, kws["tokens.txt"])]
                if not encoded or not encoded[0]:
                    raise VoiceEngineError(f"Wake phrase cannot be encoded by the configured KWS model: {phrase}")
                encoded_rows.append((encoded[0], phrase))
            phones = "；".join(" ".join(row[0]) for row in encoded_rows)
            boost = _env_float("ROBOTSOUL_VOICE_KWS_SCORE", _KEYWORD_BOOST)
            threshold = _env_float("ROBOTSOUL_VOICE_KWS_THRESHOLD", _KEYWORD_THRESHOLD)
            kws_threads = _env_int("ROBOTSOUL_VOICE_KWS_THREADS", 1)
            keyword_lines = [
                keyword_line(tokens, phrase, score=boost, threshold=threshold)
                for tokens, phrase in encoded_rows
            ]
            self._keywords_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".keywords", delete=False)
            self._keywords_file.write("\n".join(keyword_lines) + "\n")
            self._keywords_file.flush()
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(kws["tokens.txt"]), encoder=str(kws["encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"]),
                decoder=str(kws["decoder-epoch-13-avg-2-chunk-8-left-64.onnx"]),
                joiner=str(kws["joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"]),
                num_threads=kws_threads, provider="cpu", keywords_file=self._keywords_file.name,
                max_active_paths=_KEYWORD_ACTIVE_PATHS, keywords_score=boost,
                keywords_threshold=threshold, num_trailing_blanks=_KEYWORD_TRAILING_BLANKS,
            )
            self._kws_stream = self._kws.create_stream()
            _voice_log(
                "Info",
                f"唤醒模型已加载 短语={','.join(self.wake_phrases)} 音素={phones} "
                f"门槛={threshold} 加权={boost} 线程={kws_threads}",
            )
            _voice_log("Info", "识别与合成使用语音 API")
            _voice_log("Info", "开始加载端点模型")
            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.silero_vad.model = str(vad_files["silero_vad.int8.onnx"])
            vad_config.silero_vad.threshold = 0.5
            # 0.25 s splits Mandarin commands on natural pauses ("然后"/"请").
            # Keep one spoken command in a single segment for the speech API.
            vad_config.silero_vad.min_silence_duration = _env_float(
                "ROBOTSOUL_VOICE_VAD_MIN_SILENCE", 0.7
            )
            vad_config.silero_vad.min_speech_duration = 0.25
            vad_config.silero_vad.max_speech_duration = 20.0
            vad_config.sample_rate = self.sample_rate
            vad_config.num_threads = 1
            vad_config.provider = "cpu"
            self._vad_config = vad_config
            if not vad_config.validate():
                raise VoiceEngineError("Invalid Silero VAD configuration")
            self._vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
            _voice_log("Info", "端点模型已加载")
        except VoiceEngineError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise VoiceEngineError(f"Failed to initialize sherpa-onnx voice models: {exc}") from exc
        self._sherpa = sherpa_onnx
        self._np = np

    def _ensure_loaded(self) -> None:
        if self._sherpa is None:
            self.load()

    def accept_wake(self, samples: Sequence[float], sample_rate: int) -> str | None:
        self._ensure_loaded()
        self._kws_stream.accept_waveform(sample_rate, self._np.asarray(samples, dtype=self._np.float32))
        while self._kws.is_ready(self._kws_stream):
            self._kws.decode_stream(self._kws_stream)
        result = self._kws.get_result(self._kws_stream)
        if result:
            self._kws.reset_stream(self._kws_stream)
            return str(result)
        return None

    def accept_asr(self, samples: Sequence[float], sample_rate: int) -> AsrResult:
        """Streaming frames stay uncommitted; the speech API decodes a finished VAD segment."""
        del samples, sample_rate
        self._ensure_loaded()
        return AsrResult("", False)

    def transcribe_segments(self, segments: Sequence[Any]) -> AsrResult:
        """Send one finished VAD utterance to the configured speech API."""
        self._ensure_loaded()
        samples = _segment_samples(segments)
        if not samples:
            return AsrResult("", True)
        if self._speech is None:
            raise VoiceEngineError("Speech API is not configured")
        text = self._speech.transcribe(samples, self.sample_rate, language=self.language)
        return AsrResult(text, True)

    def reset_asr(self) -> None:
        self._ensure_loaded()

    def reset_vad(self) -> None:
        self._ensure_loaded()
        self._vad.reset()

    def accept_vad(self, samples: Sequence[float]) -> bool:
        self._ensure_loaded()
        self._vad.accept_waveform(self._np.asarray(samples, dtype=self._np.float32))
        return not self._vad.empty()

    def speech_detected(self) -> bool:
        self._ensure_loaded()
        return bool(self._vad.is_speech_detected())

    def has_speech_segment(self) -> bool:
        self._ensure_loaded()
        return not self._vad.empty()

    def drain_speech_segments(self) -> list[Any]:
        self._ensure_loaded()
        segments = []
        while not self._vad.empty():
            segments.append(_vad_segment(self._vad))
            self._vad.pop()
        return segments

    def synthesize(self, text: str, speaker_id: int = 0, speed: float = 1.0) -> SynthesizedAudio:
        del speaker_id, speed
        if not text.strip():
            raise ValueError("text must not be empty")
        self._ensure_loaded()
        if self._speech is None:
            raise VoiceEngineError("Speech API is not configured")
        samples, sample_rate = self._speech.synthesize(text)
        return SynthesizedAudio(tuple(samples), int(sample_rate))

    def close(self) -> None:
        """Release model references and the generated KWS keyword list."""
        self._kws_stream = self._vad = None
        self._kws = self._vad = self._sherpa = None
        if self._keywords_file is not None:
            path = Path(self._keywords_file.name)
            self._keywords_file.close()
            self._keywords_file = None
            path.unlink(missing_ok=True)


def _voice_log(level: str, message: str) -> None:
    from robotsoul.runlog import log

    log("Voice", level, message)


def _segment_samples(segments: Sequence[Any]) -> list[float]:
    samples: list[float] = []
    for segment in segments:
        chunk = getattr(segment, "samples", segment)
        if chunk is None:
            continue
        samples.extend(float(value) for value in chunk)
    return samples


def _vad_segment(vad: Any) -> Any:
    """Read one VAD segment. sherpa-onnx exposes ``front`` as a property."""
    front = vad.front
    if inspect.ismethod(front) or inspect.isfunction(front):
        return front()
    return front
