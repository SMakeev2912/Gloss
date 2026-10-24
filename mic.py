"""Запись с микрофона и обработка записи для диктовки.

Формат везде один: 16 кГц, моно, 16 бит (PCM little-endian) – его ждёт
модель распознавания GigaAM. Функции обработки работают с сырыми байтами
и не зависят от WinAPI, поэтому покрываются тестами.
"""
from __future__ import annotations

import io
import math
import wave
from array import array

SAMPLE_RATE = 16000
_BYTES_PER_SEC = SAMPLE_RATE * 2
_FULL_SCALE = 32768
_SPEECH_WINDOW_SEC = 0.05
_PAUSE_WINDOW_SEC = 0.1
_PAUSE_SEARCH_SEC = 5.0     # паузу для разреза ищем в последних секундах перед пределом
_PAUSE_LEVEL = 0.02         # тише этого – пауза, громче – речь


def _bytes(seconds: float) -> int:
    return int(seconds * SAMPLE_RATE) * 2


def rms(pcm: bytes) -> float:
    """Громкость: среднеквадратичное значение, 0..1 от полной шкалы."""
    samples = array("h", pcm[:len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(math.sumprod(samples, samples) / len(samples)) / _FULL_SCALE


def has_speech(pcm: bytes, threshold: float = _PAUSE_LEVEL, min_sec: float = 0.3) -> bool:
    """Есть ли в записи хотя бы min_sec звука громче порога (щелчки и шум тише не в счёт)."""
    window = _bytes(_SPEECH_WINDOW_SEC)
    loud = sum(1 for start in range(0, len(pcm), window) if rms(pcm[start:start + window]) > threshold)
    return loud * _SPEECH_WINDOW_SEC >= min_sec


def split_on_pauses(pcm: bytes, max_sec: float = 20.0) -> list[bytes]:
    """Режет запись на куски не длиннее max_sec – по самым тихим местам.

    GigaAM обучена на коротких фрагментах: длинная запись одним куском
    распознаётся в разы медленнее. Если паузы нет, режем ровно по пределу.
    """
    limit = _bytes(max_sec)
    window = _bytes(_PAUSE_WINDOW_SEC)
    step = window // 2
    chunks: list[bytes] = []
    rest = pcm
    while len(rest) > limit:
        cut = limit
        quietest = None
        for start in range(max(0, limit - _bytes(_PAUSE_SEARCH_SEC)), limit - window + 1, step):
            level = rms(rest[start:start + window])
            if quietest is None or level < quietest:
                quietest, cut = level, start + window // 2
        if quietest is None or quietest > _PAUSE_LEVEL:
            cut = limit
        cut -= cut % 2
        chunks.append(rest[:cut])
        rest = rest[cut:]
    chunks.append(rest)
    return chunks


def to_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def level_bar(level: float, width: int = 8) -> str:
    """Полоса громкости для плашки: «▮▮▮▯▯▯▯▯». Обычная речь заполняет её наполовину и больше."""
    filled = round(min(1.0, max(0.0, level) * 4) * width)
    return "▮" * filled + "▯" * (width - filled)
