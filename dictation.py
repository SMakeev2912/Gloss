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
RELEASE_SEC = 0.08  # столько клавиша должна быть отпущена, чтобы это считалось отпусканием


class Action(Enum):
    NONE = "none"
    FINISH = "finish"   # закончить запись и обработать


class Gesture:
    """Автомат клавиши диктовки. Создаётся в момент первого нажатия: запись уже идёт.

    RegisterHotKey сообщает только о нажатии, поэтому отпускание main.py узнаёт
    опросом GetAsyncKeyState и передаёт сюда через poll().

    Некоторые клавиатуры при удержании присылают сочетание снова, несмотря на MOD_NOREPEAT,
    и на мгновение «отпускают» клавишу между повторами. Поэтому, пока клавишу держат, новые
    нажатия не считаются, а отпускание засчитывается, только если оно длится RELEASE_SEC.
    """

    def __init__(self, now: float, tap_max_sec: float = TAP_MAX_SEC, release_sec: float = RELEASE_SEC):
        self._pressed_at = now
        self._tap_max = tap_max_sec
        self._release = release_sec
        self._up_since: float | None = None   # с какого опроса клавиша отпущена
        self._seen_down = False
        self.ignored = 0        # сколько повторов сочетания пропущено при удержании
        self.mode = "pressed"   # pressed / held / latched / done

    def press(self, now: float) -> Action:
        """Сочетание нажато ещё раз (пришёл WM_HOTKEY).

        Новым нажатием это считается, если клавишу перед ним отпускали: после короткого
        нажатия или когда опрос уже видел её отпущенной. Иначе это повтор при удержании."""
        if self.mode == "done":
            return Action.NONE
        if self.mode == "latched" or (self.mode == "pressed" and self._up_since is not None):
            self.mode = "done"
            return Action.FINISH
        self.ignored += 1
        return Action.NONE

    def poll(self, now: float, key_down: bool) -> Action:
        if self.mode not in ("pressed", "held"):
            return Action.NONE
        if key_down:
            self._seen_down = True
            self._up_since = None
            if self.mode == "pressed" and now - self._pressed_at > self._tap_max:
                self.mode = "held"
            return Action.NONE
        if self._up_since is None:
            self._up_since = now
            if not self._seen_down and now - self._pressed_at > self._tap_max:
                self.mode = "done"  # опрос опоздал (долго открывался микрофон), а держали дольше короткого нажатия
                return Action.FINISH
        if now - self._up_since < self._release:
            return Action.NONE
        if self.mode == "pressed" and self._up_since - self._pressed_at <= self._tap_max:
            self.mode = "latched"
            return Action.NONE
        self.mode = "done"
        return Action.FINISH


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
