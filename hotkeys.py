"""Глобальные горячие клавиши через RegisterHotKey.

RegisterHotKey не требует прав администратора и не ставит глобальный хук
клавиатуры: Windows сама «съедает» комбинацию и присылает WM_HOTKEY в очередь
нашего потока. Поток хоткеев также владеет невидимым окном-владельцем буфера
обмена (см. capture.py) и потому постоянно крутит цикл сообщений.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
from concurrent.futures import Future
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

import winapi as w

log = logging.getLogger(__name__)

VK_ESCAPE = 0x1B

_MODIFIERS = {
    "ctrl": w.MOD_CONTROL, "control": w.MOD_CONTROL,
    "alt": w.MOD_ALT,
    "shift": w.MOD_SHIFT,
    "win": w.MOD_WIN, "windows": w.MOD_WIN,
}
_MODIFIER_ORDER = ((w.MOD_CONTROL, "Ctrl"), (w.MOD_ALT, "Alt"), (w.MOD_SHIFT, "Shift"), (w.MOD_WIN, "Win"))

_NAMED_KEYS = {
    "space": (0x20, "Space"), "enter": (0x0D, "Enter"), "return": (0x0D, "Enter"),
    "tab": (0x09, "Tab"), "backspace": (0x08, "Backspace"),
    "esc": (VK_ESCAPE, "Esc"), "escape": (VK_ESCAPE, "Esc"),
    "insert": (0x2D, "Insert"), "ins": (0x2D, "Insert"),
    "delete": (0x2E, "Delete"), "del": (0x2E, "Delete"),
    "home": (0x24, "Home"), "end": (0x23, "End"),
    "pageup": (0x21, "PageUp"), "pgup": (0x21, "PageUp"),
    "pagedown": (0x22, "PageDown"), "pgdn": (0x22, "PageDown"),
    "up": (0x26, "Up"), "down": (0x28, "Down"), "left": (0x25, "Left"), "right": (0x27, "Right"),
    "pause": (0x13, "Pause"), "printscreen": (0x2C, "PrintScreen"),
    "plus": (0xBB, "Plus"), "minus": (0xBD, "-"),
}
# Знаки препинания – по физическим клавишам US-раскладки, чтобы хоткей
# не зависел от текущей раскладки.
_PUNCT_KEYS = {
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    modifiers: int  # MOD_* без MOD_NOREPEAT
    vk: int
    text: str       # нормализованная запись для показа: «Ctrl+Alt+G»

    def __str__(self) -> str:
        return self.text


def _parse_key(name: str) -> tuple[int, str]:
    if len(name) == 1 and ("a" <= name <= "z" or "0" <= name <= "9"):
        return ord(name.upper()), name.upper()
    if name in _PUNCT_KEYS:
        return _PUNCT_KEYS[name], name
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1, name.upper()
    if name.startswith("num") and name[3:].isdigit() and len(name) == 4:
        return 0x60 + int(name[3:]), f"Num{name[3:]}"
    raise HotkeyError(f"неизвестная клавиша «{name}»")


def parse_hotkey(spec: str) -> Hotkey:
    """Разбирает строку вида «ctrl+alt+g» (регистр и пробелы не важны)."""
    raw = spec.strip().lower().replace(" ", "")
    if not raw:
        raise HotkeyError("пустая комбинация")
    if raw.endswith("++"):  # «ctrl++» – плюс как клавиша
        raw = raw[:-2] + "+plus"
    parts = raw.split("+")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise HotkeyError(f"неизвестный модификатор «{mod}» в «{spec}»")
        modifiers |= _MODIFIERS[mod]
    if key in _MODIFIERS:
        raise HotkeyError(f"в «{spec}» нет основной клавиши")
    try:
        vk, key_text = _parse_key(key)
    except HotkeyError as exc:
        raise HotkeyError(f"{exc} в «{spec}»") from None
    if vk == VK_ESCAPE:
        raise HotkeyError("Esc зарезервирован для отмены обработки")
    is_function_key = 0x70 <= vk <= 0x87
    if not modifiers and not is_function_key:
        raise HotkeyError(f"«{spec}»: нужен хотя бы один модификатор (Ctrl, Alt, Shift или Win)")
    text = "+".join([label for flag, label in _MODIFIER_ORDER if modifiers & flag] + [key_text])
    return Hotkey(modifiers, vk, text)


MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}  # Shift, Ctrl, Alt, Win


def key_name(vk: int) -> str | None:
    """Имя клавиши по её виртуальному коду (как в config.json) или None, если клавиша не подходит."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"num{vk - 0x60}"
    for name, code in _PUNCT_KEYS.items():
        if code == vk:
            return name
    for name, (code, _label) in _NAMED_KEYS.items():
        if code == vk:
            return name
    return None


def hotkey_from_keys(modifiers: int, vk: int) -> Hotkey:
    """Нажатые клавиши (MOD_* и виртуальный код основной клавиши) → проверенное сочетание.

    Клавиша определяется физически, поэтому раскладка (русская или английская) не важна.
    """
    name = key_name(vk)
    if name is None:
        raise HotkeyError("эту клавишу нельзя использовать в сочетании")
    if vk == 0xBB:
        name = "plus"  # «=» и «+» на одной клавише: в записи «Ctrl+Plus» понятнее
    mods = [label.lower() for flag, label in _MODIFIER_ORDER if modifiers & flag]
    return parse_hotkey("+".join(mods + [name]))


_WM_APP_CALL = w.WM_APP + 1
_ESCAPE_ID = 0xB000


class HotkeyManager:
    """Поток с циклом сообщений Win32: хоткеи режимов, Esc и окно для буфера обмена.

    Колбэки вызываются в потоке хоткеев – они должны быстро возвращать управление.
    """

    def __init__(self, on_hotkey: Callable[[str], None], on_escape: Callable[[], None]):
        self._on_hotkey = on_hotkey
        self._on_escape = on_escape
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._ready = threading.Event()
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._ids: dict[int, str] = {}
        self._escape_registered = False
        self._tid = 0
        self.hwnd = None

    # --- Публичное API (из любых потоков) ---

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5) or not self.hwnd:
            raise RuntimeError("Не удалось запустить поток горячих клавиш")

    def stop(self) -> None:
        if self._tid:
            w.PostThreadMessageW(self._tid, w.WM_QUIT, 0, 0)
            self._thread.join(2)

    def register(self, bindings: dict[str, Hotkey]) -> dict[str, str]:
        """Заменяет все хоткеи режимов. Возвращает {mode_id: текст ошибки}."""
        return self._call(lambda: self._register(bindings))

    def set_escape(self, enabled: bool) -> bool:
        return self._call(lambda: self._set_escape(enabled))

    def busy_elsewhere(self, hotkey: Hotkey) -> bool:
        """Занято ли сочетание другой программой (свои сочетания перед проверкой нужно снять)."""
        return self._call(lambda: self._busy_elsewhere(hotkey))

    # --- Внутреннее (выполняется в потоке хоткеев) ---

    def _call(self, fn):
        if threading.current_thread() is self._thread:
            return fn()
        future: Future = Future()
        self._calls.put((fn, future))
        if not w.PostThreadMessageW(self._tid, _WM_APP_CALL, 0, 0):
            raise RuntimeError("Поток горячих клавиш не отвечает")
        return future.result(timeout=5)

    def _register(self, bindings: dict[str, Hotkey]) -> dict[str, str]:
        for hotkey_id in list(self._ids):
            w.UnregisterHotKey(None, hotkey_id)
        self._ids.clear()
        errors: dict[str, str] = {}
        for hotkey_id, (mode_id, hotkey) in enumerate(bindings.items(), start=1):
            if w.RegisterHotKey(None, hotkey_id, hotkey.modifiers | w.MOD_NOREPEAT, hotkey.vk):
                self._ids[hotkey_id] = mode_id
                continue
            err = ctypes.get_last_error()
            if err == w.ERROR_HOTKEY_ALREADY_REGISTERED:
                errors[mode_id] = f"{hotkey} уже занята другой программой"
            else:
                errors[mode_id] = f"{hotkey}: не удалось зарегистрировать (код {err})"
            log.warning("Хоткей %s для режима %s не зарегистрирован: код %s", hotkey, mode_id, err)
        log.info("Зарегистрировано хоткеев: %d из %d", len(self._ids), len(bindings))
        return errors

    def _busy_elsewhere(self, hotkey: Hotkey) -> bool:
        probe_id = 0xB001
        if w.RegisterHotKey(None, probe_id, hotkey.modifiers | w.MOD_NOREPEAT, hotkey.vk):
            w.UnregisterHotKey(None, probe_id)
            return False
        return ctypes.get_last_error() == w.ERROR_HOTKEY_ALREADY_REGISTERED

    def _set_escape(self, enabled: bool) -> bool:
        if enabled and not self._escape_registered:
            self._escape_registered = bool(w.RegisterHotKey(None, _ESCAPE_ID, w.MOD_NOREPEAT, VK_ESCAPE))
            if not self._escape_registered:
                log.warning("Не удалось перехватить Esc: код %s", ctypes.get_last_error())
        elif not enabled and self._escape_registered:
            w.UnregisterHotKey(None, _ESCAPE_ID)
            self._escape_registered = False
        return self._escape_registered

    def _run(self) -> None:
        msg = wintypes.MSG()
        w.PeekMessageW(ctypes.byref(msg), None, 0, 0, w.PM_NOREMOVE)  # создаёт очередь потока
        self._tid = w.GetCurrentThreadId()
        self.hwnd = w.CreateWindowExW(0, "STATIC", "Gloss", 0, 0, 0, 0, 0, w.HWND_MESSAGE, None, None, None)
        self._ready.set()
        try:
            while True:
                result = w.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                if msg.message == w.WM_HOTKEY:
                    self._dispatch(int(msg.wParam))
                elif msg.message == _WM_APP_CALL:
                    self._drain_calls()
                else:
                    w.TranslateMessage(ctypes.byref(msg))
                    w.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._register({})
            self._set_escape(False)
            if self.hwnd:
                w.DestroyWindow(self.hwnd)

    def _drain_calls(self) -> None:
        while True:
            try:
                fn, future = self._calls.get_nowait()
            except queue.Empty:
                return
            try:
                future.set_result(fn())
            except Exception as exc:  # передаём в вызывающий поток
                future.set_exception(exc)

    def _dispatch(self, hotkey_id: int) -> None:
        try:
            if hotkey_id == _ESCAPE_ID:
                self._on_escape()
            elif hotkey_id in self._ids:
                self._on_hotkey(self._ids[hotkey_id])
        except Exception:
            log.exception("Ошибка в обработчике горячей клавиши")
