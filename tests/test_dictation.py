import io
import math
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from dictation import Action, Gesture, Outcome, dictate, paste_target_ok  # noqa: E402
from llm import BackendUnavailable, Cancelled  # noqa: E402
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


# --- Клавиша: короткое нажатие или удержание ---

def test_tap_latches_until_next_press():
    g = Gesture(now=0.0)
    assert g.poll(0.1, key_down=False) is Action.NONE and g.mode == "latched"
    assert g.poll(30.0, key_down=False) is Action.NONE
    assert g.press(31.0) is Action.FINISH and g.mode == "done"


def test_hold_finishes_on_release():
    g = Gesture(now=0.0)
    assert g.poll(0.2, key_down=True) is Action.NONE and g.mode == "pressed"
    assert g.poll(0.5, key_down=True) is Action.NONE and g.mode == "held"
    assert g.poll(4.0, key_down=False) is Action.FINISH and g.mode == "done"


def test_late_first_poll_after_long_hold_counts_as_hold():
    g = Gesture(now=0.0)
    assert g.poll(0.9, key_down=False) is Action.FINISH  # опрос опоздал, клавишу держали дольше 0,4 с


def test_press_while_held_is_ignored():
    g = Gesture(now=0.0)
    g.poll(0.5, key_down=True)
    assert g.press(0.6) is Action.NONE and g.mode == "held"


def test_latched_ignores_key_state_until_press():
    g = Gesture(now=0.0)
    g.poll(0.1, key_down=False)
    assert g.poll(5.0, key_down=True) is Action.NONE and g.mode == "latched"  # клавиша нажата, WM_HOTKEY ещё не пришёл


# --- Распознать и улучшить ---

def asr(text):
    return lambda pcm: text


def test_silence_skips_recognition():
    calls = []
    out = dictate(silence(3) + bytes([1, 0]), lambda p: calls.append("asr") or "x", lambda t: t)
    assert out == Outcome("", False, "silence") and calls == []


def test_all_zero_means_mic_blocked():
    assert dictate(silence(3), asr("x"), lambda t: t).reason == "mic_blocked"


def test_empty_transcript_is_silence():
    assert dictate(tone(2), asr("  "), lambda t: t).reason == "silence"


def test_improved_text():
    assert dictate(tone(2), asr("ну короче привет"), lambda t: "Привет.") == Outcome("Привет.", True, "")


def test_llm_failure_falls_back_to_raw():
    def broken(_):
        raise BackendUnavailable("нет")
    assert dictate(tone(2), asr("ну привет"), broken) == Outcome("ну привет", False, "llm_failed")


def test_cancel_propagates():
    def cancelled(_):
        raise Cancelled
    with pytest.raises(Cancelled):
        dictate(tone(2), asr("текст"), cancelled)


def test_target_window_changed_keeps_result_in_clipboard():
    assert paste_target_ok(100, 100)
    assert not paste_target_ok(100, 200)
    assert not paste_target_ok(100, None)
    assert not paste_target_ok(None, None)
