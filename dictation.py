"""Диктовка: логика клавиши и цепочка «распознать → улучшить».

Без WinAPI и tkinter: состояние клавиши и запись передаёт main.py, поэтому
поведение покрывается тестами на поддельных часах и подставных функциях.

Одно сочетание – два способа. Короткое нажатие (отпустили быстрее TAP_MAX_SEC)
включает запись до следующего нажатия; удержание работает как рация:
запись идёт, пока клавиша нажата.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from llm import LlmError
from mic import all_zero, has_speech

TAP_MAX_SEC = 0.4


class Action(Enum):
    NONE = "none"
    FINISH = "finish"   # закончить запись и обработать


class Gesture:
    """Автомат клавиши диктовки. Создаётся в момент первого нажатия: запись уже идёт.

    RegisterHotKey сообщает только о нажатии, поэтому отпускание main.py узнаёт
    опросом GetAsyncKeyState и передаёт сюда через poll().
    """

    def __init__(self, now: float, tap_max_sec: float = TAP_MAX_SEC):
        self._pressed_at = now
        self._tap_max = tap_max_sec
        self.mode = "pressed"   # pressed / held / latched / done

    def press(self, now: float) -> Action:
        """Сочетание нажато ещё раз (пришёл WM_HOTKEY).

        С MOD_NOREPEAT новое нажатие приходит только после отпускания клавиши, поэтому в любом
        состоянии записи оно означает «закончить», даже если отпускание опрос ещё не заметил."""
        if self.mode == "done":
            return Action.NONE
        self.mode = "done"
        return Action.FINISH

    def poll(self, now: float, key_down: bool) -> Action:
        if self.mode == "pressed":
            quick = now - self._pressed_at <= self._tap_max
            if key_down:
                if not quick:
                    self.mode = "held"
            elif quick:
                self.mode = "latched"
            else:  # опрос опоздал, а держали дольше короткого нажатия – это удержание
                self.mode = "done"
                return Action.FINISH
        elif self.mode == "held" and not key_down:
            self.mode = "done"
            return Action.FINISH
        return Action.NONE


def paste_target_ok(start_hwnd, end_hwnd) -> bool:
    """Вставлять, только если активно то же окно, что и в начале записи: иначе текст попал бы в чужое окно."""
    return bool(start_hwnd) and start_hwnd == end_hwnd


@dataclass(frozen=True)
class Outcome:
    text: str
    improved: bool
    reason: str   # "" – всё хорошо; silence, mic_blocked, llm_failed, llm_rejected


def _too_long(raw: str, improved: str) -> bool:
    """Очистка только убирает лишнее. Намного более длинный ответ значит, что модель
    выполнила сказанное («напиши письмо…») вместо того, чтобы его записать."""
    return len(improved) > len(raw) * 1.5 + 40


def dictate(pcm: bytes, transcribe: Callable[[bytes], str], improve: Callable[[str], str]) -> Outcome:
    """Запись → текст. Если модель недоступна или ответила не по делу, возвращает распознанный
    текст как есть, чтобы надиктованное не потерялось. Cancelled пробрасывается."""
    if all_zero(pcm):
        return Outcome("", False, "mic_blocked")
    if not has_speech(pcm):
        return Outcome("", False, "silence")
    raw = transcribe(pcm).strip()
    if not raw:
        return Outcome("", False, "silence")
    try:
        improved = improve(raw).strip()
    except LlmError:
        return Outcome(raw, False, "llm_failed")
    if _too_long(raw, improved):
        return Outcome(raw, False, "llm_rejected")
    return Outcome(improved, True, "")
