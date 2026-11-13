"""Optional sherpa-onnx adapters for the Go2 voice service."""

from __future__ import annotations

from dataclasses import dataclass
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
# Sherpa treats rule 3 as "endpoint once the utterance reaches this many seconds".
# Zero makes the first audio chunk an endpoint, so the command after the wake word is discarded.
_ASR_MAX_UTTERANCE_SECONDS = 20.0


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
    """CPU-only sherpa implementation; imports native dependencies only on load()."""

    def __init__(self, resource_root: str | Path, wake_phrase: str, sample_rate: int = 16000):
        if not wake_phrase.strip():
            raise ValueError("wake_phrase must not be empty")
        self.resource_root = Path(resource_root)
        self.wake_phrase = wake_phrase.strip()
        self.sample_rate = sample_rate
        self._sherpa: Any = None
        self._kws: Any = None
        self._kws_stream: Any = None
        self._recognizer: Any = None
        self._asr_stream: Any = None
        self._vad: Any = None
        self._tts: Any = None
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
        _, asr = _model_dir(self.resource_root, "asr-zh-streaming-int8",
                            ("tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"))
        _, vad_files = _model_dir(self.resource_root, "vad-silero-int8", ("silero_vad.int8.onnx",))
        _, tts_files = _model_dir(self.resource_root, "tts-zh-xiao-ya-int8", (
            "tokens.txt", "lexicon.txt", "phone.fst", "date.fst", "number.fst",
        ))
        tts_models = sorted((self.resource_root / "models" / "tts-zh-xiao-ya-int8").rglob("*.onnx"))
        if len(tts_models) != 1:
            raise VoiceEngineError(f"Expected one TTS ONNX model under {self.resource_root / 'models' / 'tts-zh-xiao-ya-int8'}; found {len(tts_models)}")
        lexicons = sorted((self.resource_root / "models" / "tts-zh-xiao-ya-int8").rglob("lexicon.txt"))

        try:
            _voice_log("Info", "开始加载唤醒模型")
            try:
                encoded = sherpa_onnx.text2token(
                    [self.wake_phrase],
                    tokens=str(kws["tokens.txt"]), tokens_type="phone+ppinyin", lexicon=str(kws["en.phone"]),
                )
            except ModuleNotFoundError as exc:
                if exc.name != "sentencepiece":
                    raise
                encoded = [_fallback_phone_pinyin(self.wake_phrase, kws["tokens.txt"])]
            if not encoded or not encoded[0]:
                raise VoiceEngineError(f"Wake phrase cannot be encoded by the configured KWS model: {self.wake_phrase}")
            phones = " ".join(encoded[0])
            keyword = f"{phones} :{_KEYWORD_BOOST} #{_KEYWORD_THRESHOLD} @{self.wake_phrase.replace(' ', '_')}"
            self._keywords_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".keywords", delete=False)
            self._keywords_file.write(keyword + "\n")
            self._keywords_file.flush()
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(kws["tokens.txt"]), encoder=str(kws["encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"]),
                decoder=str(kws["decoder-epoch-13-avg-2-chunk-8-left-64.onnx"]),
                joiner=str(kws["joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"]),
                num_threads=1, provider="cpu", keywords_file=self._keywords_file.name,
                max_active_paths=_KEYWORD_ACTIVE_PATHS, keywords_score=_KEYWORD_BOOST,
                keywords_threshold=_KEYWORD_THRESHOLD, num_trailing_blanks=_KEYWORD_TRAILING_BLANKS,
            )
            self._kws_stream = self._kws.create_stream()
            _voice_log(
                "Info",
                f"唤醒模型已加载 短语={self.wake_phrase} 音素={phones} "
                f"门槛={_KEYWORD_THRESHOLD} 加权={_KEYWORD_BOOST}",
            )
            _voice_log("Info", "开始加载识别模型")
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(asr["tokens.txt"]), encoder=str(asr["encoder.int8.onnx"]),
                decoder=str(asr["decoder.onnx"]), joiner=str(asr["joiner.int8.onnx"]),
                num_threads=1, sample_rate=self.sample_rate, feature_dim=80, enable_endpoint_detection=True,
                rule1_min_trailing_silence=0.5, rule2_min_trailing_silence=0.3,
                rule3_min_utterance_length=_ASR_MAX_UTTERANCE_SECONDS,
                decoding_method="greedy_search", provider="cpu",)
            self._asr_stream = self._recognizer.create_stream()
            _voice_log("Info", f"识别模型已加载 目录={asr['encoder.int8.onnx'].parent.name}")
            _voice_log("Info", "开始加载端点模型")
            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.silero_vad.model = str(vad_files["silero_vad.int8.onnx"])
            vad_config.silero_vad.threshold = 0.5
            vad_config.silero_vad.min_silence_duration = 0.25
            vad_config.silero_vad.min_speech_duration = 0.1
            vad_config.silero_vad.max_speech_duration = 20.0
            vad_config.sample_rate = self.sample_rate
            vad_config.num_threads = 1
            vad_config.provider = "cpu"
            self._vad_config = vad_config
            if not vad_config.validate():
                raise VoiceEngineError("Invalid Silero VAD configuration")
            self._vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
            _voice_log("Info", "端点模型已加载")
            _voice_log("Info", "开始加载合成模型")

            tts_config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(tts_models[0]), lexicon=str(tts_files["lexicon.txt"]),
                        tokens=str(tts_files["tokens.txt"])),
                    provider="cpu", debug=False, num_threads=1),
                rule_fsts=",".join(str(tts_files[name]) for name in ("phone.fst", "date.fst", "number.fst")))
            if not tts_config.validate():
                raise VoiceEngineError("Invalid Xiao-Ya TTS configuration")
            self._tts = sherpa_onnx.OfflineTts(tts_config)
            _voice_log("Info", "合成模型已加载")
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
        self._ensure_loaded()
        self._asr_stream.accept_waveform(sample_rate, self._np.asarray(samples, dtype=self._np.float32))
        while self._recognizer.is_ready(self._asr_stream):
            self._recognizer.decode_stream(self._asr_stream)
        text = str(self._recognizer.get_result(self._asr_stream))
        endpoint = bool(self._recognizer.is_endpoint(self._asr_stream))
        if endpoint:
            self._recognizer.reset(self._asr_stream)
            self._asr_stream = self._recognizer.create_stream()
        return AsrResult(text, endpoint)

    def reset_asr(self) -> None:
        self._ensure_loaded()
        self._recognizer.reset(self._asr_stream)
        self._asr_stream = self._recognizer.create_stream()

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

    def drain_speech_segments(self) -> list[Any]:
        self._ensure_loaded()
        segments = []
        while not self._vad.empty():
            segments.append(_vad_segment(self._vad))
            self._vad.pop()
        return segments

    def synthesize(self, text: str, speaker_id: int = 0, speed: float = 1.0) -> SynthesizedAudio:
        if not text.strip():
            raise ValueError("text must not be empty")
        self._ensure_loaded()
        generation = self._sherpa.GenerationConfig()
        generation.sid = speaker_id
        generation.speed = speed
        generation.silence_scale = 0.2
        audio = self._tts.generate(text, generation)
        return SynthesizedAudio(tuple(float(x) for x in audio.samples), int(audio.sample_rate))

    def close(self) -> None:
        """Release model references and the generated KWS keyword list."""
        self._kws_stream = self._asr_stream = self._vad = None
        self._kws = self._recognizer = self._vad = self._tts = self._sherpa = None
        if self._keywords_file is not None:
            path = Path(self._keywords_file.name)
            self._keywords_file.close()
            self._keywords_file = None
            path.unlink(missing_ok=True)


def _voice_log(level: str, message: str) -> None:
    from robotsoul.runlog import log

    log("Voice", level, message)


def _vad_segment(vad: Any) -> Any:
    """Read one VAD segment. sherpa-onnx exposes ``front`` as a property."""
    front = vad.front
    if inspect.ismethod(front) or inspect.isfunction(front):
        return front()
    return front
