import io
import math
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mic import all_zero, has_speech, level_bar, mic_error_text, rms, split_on_pauses, to_wav  # noqa: E402

RATE = 16000
BYTES_PER_SEC = RATE * 2


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def tone(seconds: float, amplitude: int = 8000) -> bytes:
    samples = array("h", (int(amplitude * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(int(seconds * RATE))))
    return samples.tobytes()


# --- Обработка записи ---

def test_rms_of_silence_and_tone():
    assert rms(silence(1.0)) == 0
    assert 0.15 < rms(tone(1.0)) < 0.2  # 8000 / 32768 / √2 ≈ 0.17
    assert rms(b"") == 0


def test_has_speech():
    assert not has_speech(silence(5))
    assert not has_speech(silence(2) + tone(0.1) + silence(2))  # щелчок короче 0,3 с
    assert has_speech(silence(1) + tone(0.5))


def test_split_short_is_single_chunk():
    pcm = tone(5)
    assert split_on_pauses(pcm) == [pcm]


def test_split_prefers_pauses():
    pcm = tone(15) + silence(1) + tone(15)
    chunks = split_on_pauses(pcm, max_sec=20)
    assert b"".join(chunks) == pcm
    assert len(chunks) == 2
    assert 15 * BYTES_PER_SEC <= len(chunks[0]) <= 16 * BYTES_PER_SEC  # разрез внутри паузы


def test_split_without_pauses_cuts_hard_and_loses_nothing():
    pcm = tone(50)
    chunks = split_on_pauses(pcm, max_sec=20)
    assert b"".join(chunks) == pcm
    assert all(len(c) <= 20 * BYTES_PER_SEC for c in chunks)
    assert all(len(c) % 2 == 0 for c in chunks)  # не режем сэмпл пополам


def test_to_wav_header():
    with wave.open(io.BytesIO(to_wav(tone(1))), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 16000)


def test_level_bar():
    assert level_bar(0) == "▯" * 8
    assert level_bar(1) == "▮" * 8
    assert level_bar(5) == "▮" * 8  # больше 1 не ломает


# --- Микрофон ---

def test_mic_error_texts():
    assert mic_error_text(2) == "Микрофон не найден"  # MMSYSERR_BADDEVICEID
    assert mic_error_text(6) == "Микрофон не найден"  # MMSYSERR_NODRIVER
    assert mic_error_text(4) == "Микрофон занят другой программой"  # MMSYSERR_ALLOCATED
    assert "код 11" in mic_error_text(11)


def test_all_zero():
    assert all_zero(silence(1))
    assert not all_zero(silence(1) + b"\x01\x00")
