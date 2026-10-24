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
    assert g.poll(0.1, key_down=False) is Action.NONE and g.mode == "pressed"  # отпускание ещё проверяется
    assert g.poll(0.2, key_down=False) is Action.NONE and g.mode == "latched"
    assert g.poll(30.0, key_down=False) is Action.NONE
    assert g.press(31.0) is Action.FINISH and g.mode == "done"


def test_tap_released_just_before_limit_latches():
    g = Gesture(now=0.0)
    g.poll(0.35, key_down=False)
    assert g.poll(0.45, key_down=False) is Action.NONE and g.mode == "latched"  # отпустили в 0,35 с


def test_hold_finishes_on_release():
    g = Gesture(now=0.0)
    assert g.poll(0.2, key_down=True) is Action.NONE and g.mode == "pressed"
    assert g.poll(0.5, key_down=True) is Action.NONE and g.mode == "held"
    assert g.poll(4.0, key_down=False) is Action.NONE and g.mode == "held"
    assert g.poll(4.1, key_down=False) is Action.FINISH and g.mode == "done"


def test_autorepeat_while_held_is_ignored():
    # некоторые клавиатуры при удержании присылают сочетание снова, несмотря на MOD_NOREPEAT
    g = Gesture(now=0.0)
    g.poll(0.2, key_down=True)
    assert g.press(0.3) is Action.NONE
    g.poll(0.5, key_down=True)
    assert g.press(0.55) is Action.NONE and g.press(0.58) is Action.NONE
    assert g.mode == "held" and g.ignored == 3


def test_brief_key_up_while_held_is_ignored():
    g = Gesture(now=0.0)
    g.poll(0.5, key_down=True)
    assert g.poll(1.0, key_down=False) is Action.NONE
    assert g.poll(1.02, key_down=True) is Action.NONE
    assert g.poll(1.1, key_down=False) is Action.NONE and g.mode == "held"


def test_late_first_poll_after_long_hold_counts_as_hold():
    g = Gesture(now=0.0)
    assert g.poll(0.9, key_down=False) is Action.FINISH  # опрос опоздал, клавишу держали дольше 0,4 с


def test_quick_double_tap_finishes():
    g = Gesture(now=0.0)
    g.poll(0.1, key_down=False)  # отпустили, отпускание ещё проверяется
    assert g.press(0.15) is Action.FINISH and g.mode == "done"


def test_press_after_done_does_nothing():
    g = Gesture(now=0.0)
    g.poll(0.1, key_down=False)
    g.poll(0.2, key_down=False)
    g.press(0.5)
    assert g.press(0.6) is Action.NONE


def test_latched_ignores_key_state_until_press():
    g = Gesture(now=0.0)
    g.poll(0.1, key_down=False)
    g.poll(0.2, key_down=False)
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


# --- Ожидание конца записи в main.App ---

def _record_with(monkeypatch, presses, key_down, pressed_at):
    import queue
    import threading
    import types

    import main
    notes = []
    fake_self = types.SimpleNamespace(
        _cancel=threading.Event(), _dictation_presses=queue.SimpleQueue(),
        tray=types.SimpleNamespace(notify=lambda *a: notes.append(a)),
        ui=types.SimpleNamespace(update_progress=lambda *a, **k: None))
    for press in presses:
        fake_self._dictation_presses.put(press)
    recorder = types.SimpleNamespace(elapsed=lambda: 0.3, level=lambda: 0.0)  # уже за пределом max_sec
    monkeypatch.setattr(main.winapi, "GetAsyncKeyState", lambda vk: 0x8000 if key_down else 0)
    mode = types.SimpleNamespace(hotkey=types.SimpleNamespace(vk=0x20))
    main.App._record(fake_self, recorder, mode, max_sec=0.2, pressed_at=pressed_at)
    return notes


def test_hold_released_while_mic_was_opening_finishes(monkeypatch):
    import time
    # держали 1 с и отпустили, пока открывался микрофон: это удержание, запись заканчивается сразу
    notes = _record_with(monkeypatch, [], key_down=False, pressed_at=time.monotonic() - 1.0)
    assert notes == []  # закончилось по отпусканию, а не по пределу длины


def test_second_tap_while_mic_was_opening_is_kept(monkeypatch):
    import time
    start = time.monotonic() - 0.6
    # нажали, отпустили (короткое нажатие) и нажали ещё раз, пока открывался микрофон
    notes = _record_with(monkeypatch, [start + 0.5], key_down=False, pressed_at=start)
    assert notes == []


def test_model_writing_a_letter_instead_of_cleaning_is_rejected():
    raw = "напиши Ивану что встреча в четыре"
    letter = "Здравствуйте, Иван! Сообщаю, что встреча переносится на четыре часа. " * 3
    assert dictate(tone(2), asr(raw), lambda t: letter) == Outcome(raw, False, "llm_rejected")


def test_slightly_longer_cleanup_is_accepted():
    raw = "ну в общем завтра в три"
    assert dictate(tone(2), asr(raw), lambda t: "В общем, завтра в три часа.").improved


def test_quiet_laptop_mic_speech_is_heard():
    assert has_speech(silence(1) + tone(1, amplitude=400))      # тихая речь, около -41 дБ
    assert not has_speech(tone(3, amplitude=100))               # фоновый шум комнаты, около -53 дБ
