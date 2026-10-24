"""Запись с микрофона и обработка записи для диктовки.

Формат везде один: 16 кГц, моно, 16 бит (PCM little-endian) – его ждёт
модель распознавания GigaAM. Функции обработки работают с сырыми байтами
и не зависят от WinAPI, поэтому покрываются тестами.
"""
from __future__ import annotations

import ctypes
import io
import logging
import math
import threading
import time
import wave
from array import array
from collections import deque

import winapi as w

log = logging.getLogger(__name__)

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


def has_speech(pcm: bytes, threshold: float = _SPEECH_LEVEL, min_sec: float = 0.3) -> bool:
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


def all_zero(pcm: bytes) -> bool:
    """Одни нули: так Windows отдаёт звук, если доступ к микрофону запрещён в параметрах."""
    return not pcm.strip(b"\x00")


# --- Запись ------------------------------------------------------------------------

class MicError(Exception):
    """Ошибка микрофона, которую можно показать пользователю как есть."""


def mic_error_text(code: int) -> str:
    if code in (w.MMSYSERR_BADDEVICEID, w.MMSYSERR_NODRIVER):
        return "Микрофон не найден"
    if code == w.MMSYSERR_ALLOCATED:
        return "Микрофон занят другой программой"
    return f"Не удалось включить микрофон (код {code})"


class Recorder:
    """Запись с микрофона Windows по умолчанию через waveIn.

    Четыре буфера по 100 мс по кругу: Windows заполняет их и сигналит событием,
    поток mic забирает заполненные по порядку и возвращает их в очередь.
    """

    _BUFFERS = 4
    _BUFFER_SEC = 0.1

    def __init__(self) -> None:
        self._handle = w.HWAVEIN()
        self._event = None
        self._queue: deque = deque()  # (заголовок, буфер) в порядке заполнения
        self._data = bytearray()
        self._lock = threading.Lock()
        self._level = 0.0
        self._started = 0.0
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._event = w.CreateEventW(None, False, False, None)
        fmt = w.WAVEFORMATEX(w.WAVE_FORMAT_PCM, 1, SAMPLE_RATE, _BYTES_PER_SEC, 2, 16, 0)
        code = w.waveInOpen(ctypes.byref(self._handle), w.WAVE_MAPPER, ctypes.byref(fmt), self._event, 0,
                            w.CALLBACK_EVENT)
        if code:
            w.CloseHandle(self._event)
            self._event = None
            log.warning("waveInOpen: код %s", code)
            raise MicError(mic_error_text(code))
        size = _bytes(self._BUFFER_SEC)
        for _ in range(self._BUFFERS):
            buffer = ctypes.create_string_buffer(size)
            header = w.WAVEHDR(lpData=ctypes.cast(buffer, ctypes.c_void_p), dwBufferLength=size)
            w.waveInPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
            w.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header))
            self._queue.append((header, buffer))
        code = w.waveInStart(self._handle)
        if code:
            self._close()
            log.warning("waveInStart: код %s", code)
            raise MicError(mic_error_text(code))
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="mic", daemon=True)
        self._thread.start()

    def stop(self) -> bytes:
        """Останавливает запись и возвращает её целиком. Повторный вызов ничего не делает."""
        if self._thread is not None:
            self._stopping.set()
            self._thread.join(2)
            self._thread = None
            w.waveInReset(self._handle)  # отдаёт и недозаполненный буфер
            self._collect(requeue=False)
            self._close()
        with self._lock:
            return bytes(self._data)

    def level(self) -> float:
        return self._level

    def elapsed(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    def _run(self) -> None:
        while not self._stopping.is_set():
            w.WaitForSingleObject(self._event, 100)
            self._collect(requeue=True)

    def _collect(self, requeue: bool) -> None:
        """Забирает заполненные буферы по порядку; requeue – вернуть их Windows для записи дальше."""
        for _ in range(len(self._queue)):
            header, buffer = self._queue[0]
            if not header.dwFlags & w.WHDR_DONE:
                break
            self._queue.rotate(-1)
            chunk = buffer.raw[:header.dwBytesRecorded]
            if chunk:
                with self._lock:
                    self._data += chunk
                self._level = rms(chunk)
            if requeue:
                w.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header))

    def _close(self) -> None:
        for header, _ in self._queue:
            w.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
        self._queue.clear()
        w.waveInClose(self._handle)
        if self._event:
            w.CloseHandle(self._event)
            self._event = None
