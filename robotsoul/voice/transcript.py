"""Wake-phrase normalization shared by command matching and voice service."""

from __future__ import annotations

from typing import Sequence

_EDGE_PUNCTUATION = " \t\r\n，。！？、,.!?;；:："
_INITIALS = (
    "zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k",
    "h", "j", "q", "x", "r", "z", "c", "s", "y", "w",
)
_WAKE_PREFIX_SIMILARITY_THRESHOLD = 0.75


def compact_transcript(text: str) -> str:
    """Remove whitespace and punctuation at the utterance boundaries."""
    if not isinstance(text, str):
        return ""
    return "".join(text.split()).strip(_EDGE_PUNCTUATION)


def wake_phrases(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        phrases = value.split(",")
    else:
        phrases = value
    return tuple(
        cleaned
        for phrase in phrases
        if isinstance(phrase, str) and (cleaned := compact_transcript(phrase))
    )


def is_wake_only(text: str, wake_phrase: str) -> bool:
    """Return whether text is exactly a configured wake phrase or its homophone."""
    cleaned = compact_transcript(text)
    wake = compact_transcript(wake_phrase)
    if not cleaned or not wake:
        return False
    if cleaned == wake:
        return True
    wake_syllables = _toneless_pinyin(wake)
    text_syllables = _toneless_pinyin(cleaned)
    if wake_syllables is not None and text_syllables is not None:
        return text_syllables == wake_syllables
    # Keep the existing conservative fallback when the optional voice
    # dependency is unavailable; dynamic prefix stripping remains disabled.
    return len(cleaned) == len(wake) and cleaned[-1] == wake[-1]


def wake_prefix_candidates(text: str, wake_phrase: str | Sequence[str]) -> list[str]:
    """Yield exact and sufficiently similar configured wake-prefix removals."""
    source = compact_transcript(text)
    if not source:
        return []

    wakes = sorted(wake_phrases(wake_phrase), key=len, reverse=True)
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        candidate = value.lstrip(_EDGE_PUNCTUATION)
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    exact: list[str] = []
    for wake in wakes:
        if source.startswith(wake):
            exact.append(source[len(wake) :])

    near: list[tuple[float, str, int]] = []
    for wake in wakes:
        wake_syllables = _toneless_pinyin(wake)
        if not wake_syllables or not _is_chinese_text(wake):
            continue
        prefix_length = len(wake_syllables)
        prefix = source[:prefix_length]
        if len(prefix) != prefix_length or not _is_chinese_text(prefix):
            continue
        prefix_syllables = _toneless_pinyin(prefix)
        if prefix_syllables is None or len(prefix_syllables) != len(wake_syllables):
            continue
        score = _pinyin_similarity(prefix_syllables, wake_syllables)
        if score >= _WAKE_PREFIX_SIMILARITY_THRESHOLD and any(
            left == right for left, right in zip(prefix_syllables, wake_syllables)
        ):
            near.append((score, source[prefix_length:], len(wake)))

    for suffix in exact:
        add(suffix)
    for _, suffix, _ in sorted(near, key=lambda item: (item[0], item[2]), reverse=True):
        add(suffix)
    add(source)
    return candidates


def _toneless_pinyin(text: str) -> list[str] | None:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    return [str(item).lower() for item in lazy_pinyin(text)]


def _is_chinese_text(text: str) -> bool:
    return bool(text) and all(
        0x3400 <= ord(char) <= 0x4DBF
        or 0x4E00 <= ord(char) <= 0x9FFF
        or 0xF900 <= ord(char) <= 0xFAFF
        or 0x20000 <= ord(char) <= 0x323AF
        for char in text
    )


def _pinyin_similarity(candidate: Sequence[str], wake: Sequence[str]) -> float:
    if not candidate or len(candidate) != len(wake):
        return 0.0
    return sum(
        _syllable_similarity(left, right) for left, right in zip(candidate, wake)
    ) / len(wake)


def _syllable_similarity(candidate: str, wake: str) -> float:
    if candidate == wake:
        return 1.0
    candidate_initial, candidate_final = _split_initial_final(candidate)
    wake_initial, wake_final = _split_initial_final(wake)
    if (candidate_initial and candidate_initial == wake_initial) or (
        candidate_final and candidate_final == wake_final
    ):
        return 0.5
    return 0.0


def _split_initial_final(syllable: str) -> tuple[str, str]:
    for initial in _INITIALS:
        if syllable.startswith(initial):
            return initial, syllable[len(initial) :]
    return "", syllable
