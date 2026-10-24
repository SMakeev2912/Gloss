"""Захват выделенного текста через буфер обмена и вставка результата.

Схема: сохранить буфер целиком (все форматы, не только текст) → очистить →
эмулировать Ctrl+C → дождаться текста → … → положить результат → Ctrl+V →
вернуть исходное содержимое буфера.
"""
from __future__ import annotations

import ctypes
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

from config import CaptureSettings
import winapi as w

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
# Форматы, данные которых – GDI-объекты, а не HGLOBAL. Скопировать их байты
# нельзя; Windows сама синтезирует CF_BITMAP из сохранённого CF_DIB.
_GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_SKIP_FORMAT_NAMES = {"DataObject", "Ole Private Data"}
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024

VK_C = 0x43
VK_V = 0x56
VK_CONTROL = 0x11
VK_DUMMY = 0xE8  # неназначенная клавиша: гасит открытие меню по Alt и «Пуск» по Win
_MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)  # L/R Shift, Ctrl, Alt, Win
_EXTENDED_VKS = {0xA3, 0xA5, 0x5B, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x2C, 0x6F, 0x90}


class ClipboardError(Exception):
    pass


@dataclass
class Snapshot:
    items: list[tuple[int, bytes]]


@dataclass
class Captured:
    text: str
    snapshot: Snapshot | None


# --- Клавиатура -----------------------------------------------------------------

def _key(vk: int, up: bool = False) -> w.INPUT:
    flags = (w.KEYEVENTF_KEYUP if up else 0) | (w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0)
    event = w.INPUT(type=w.INPUT_KEYBOARD)
    event.ki = w.KEYBDINPUT(wVk=vk, wScan=w.MapVirtualKeyW(vk, w.MAPVK_VK_TO_VSC), dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _send(events: list[w.INPUT]) -> None:
    array = (w.INPUT * len(events))(*events)
    sent = w.SendInput(len(events), array, ctypes.sizeof(w.INPUT))
    if sent != len(events):
        log.warning("SendInput отправил %d из %d событий (код %s)", sent, len(events), ctypes.get_last_error())


def _is_down(vk: int) -> bool:
    return bool(w.GetAsyncKeyState(vk) & 0x8000)


def release_keys(hotkey_vk: int | None, timeout_ms: int) -> None:
    """Ждёт, пока пользователь отпустит клавиши хоткея, а что не отпущено – отпускает сам.

    Без этого Ctrl+C при зажатом Alt превратится в Ctrl+Alt+C, а при зажатом
    Shift – в Ctrl+Shift+C (в Chrome это открывает инструменты разработчика).
    """
    keys = _MODIFIER_VKS + ((hotkey_vk,) if hotkey_vk else ())
    deadline = time.monotonic() + timeout_ms / 1000
    while any(_is_down(vk) for vk in keys) and time.monotonic() < deadline:
        time.sleep(0.01)
    held = [vk for vk in keys if _is_down(vk)]
    if held:
        log.info("Клавиши не отпущены за %d мс, отпускаю программно: %s", timeout_ms, [hex(vk) for vk in held])
        _send([_key(VK_DUMMY), _key(VK_DUMMY, up=True)] + [_key(vk, up=True) for vk in held])
        time.sleep(0.03)


def _send_ctrl(vk: int) -> None:
    _send([_key(VK_CONTROL), _key(vk), _key(vk, up=True), _key(VK_CONTROL, up=True)])


# --- Буфер обмена ---------------------------------------------------------------

class Clipboard:
    """Работа с буфером обмена через Win32. owner_hwnd – окно потока с циклом сообщений."""

    def __init__(self, owner_hwnd):
        self.owner_hwnd = owner_hwnd
        # Маркеры, по которым журнал буфера (Win+V), облачная синхронизация и
        # менеджеры буфера пропускают наши временные данные.
        self._private_formats = [
            (w.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), b"\0"),
            (w.RegisterClipboardFormatW("CanIncludeInClipboardHistory"), b"\0\0\0\0"),
            (w.RegisterClipboardFormatW("CanUploadToCloudClipboard"), b"\0\0\0\0"),
        ]
        self._vscode_format = w.RegisterClipboardFormatW("vscode-editor-data")

    @contextmanager
    def _opened(self, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while not w.OpenClipboard(self.owner_hwnd):
            if time.monotonic() > deadline:
                raise ClipboardError("Буфер обмена занят другой программой")
            time.sleep(0.01)
        try:
            yield
        finally:
            w.CloseClipboard()

    @staticmethod
    def _read_handle(handle) -> bytes | None:
        size = w.GlobalSize(handle)
        pointer = w.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            w.GlobalUnlock(handle)

    @staticmethod
    def _put(fmt: int, data: bytes) -> None:
        handle = w.GlobalAlloc(w.GMEM_MOVEABLE, max(len(data), 1))
        if not handle:
            raise ClipboardError("Не хватает памяти для буфера обмена")
        pointer = w.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        w.GlobalUnlock(handle)
        if not w.SetClipboardData(fmt, handle):
            w.GlobalFree(handle)  # при успехе памятью владеет система
            raise ClipboardError(f"Не удалось записать формат {fmt} в буфер (код {ctypes.get_last_error()})")

    @staticmethod
    def _format_name(fmt: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        return buffer.value if w.GetClipboardFormatNameW(fmt, buffer, 256) else ""

    def sequence(self) -> int:
        return w.GetClipboardSequenceNumber()

    def snapshot(self) -> Snapshot:
        items: list[tuple[int, bytes]] = []
        total = 0
        with self._opened():
            fmt = 0
            while True:
                fmt = w.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                if fmt in _GDI_FORMATS or 0x200 <= fmt <= 0x3FF or self._format_name(fmt) in _SKIP_FORMAT_NAMES:
                    continue
                handle = w.GetClipboardData(fmt)
                if not handle:
                    continue
                data = self._read_handle(handle)
                if data is None:
                    continue
                total += len(data)
                if total > _MAX_SNAPSHOT_BYTES:
                    log.warning("Буфер обмена слишком большой – сохранена только часть форматов")
                    break
                items.append((fmt, data))
        return Snapshot(items)

    def restore(self, snapshot: Snapshot) -> None:
        with self._opened():
            w.EmptyClipboard()
            for fmt, data in snapshot.items:
                try:
                    self._put(fmt, data)
                except ClipboardError as exc:
                    log.warning("Формат %s не восстановлен: %s", fmt, exc)
            if snapshot.items:  # иначе буфер был пуст – пусть пустым и остаётся
                self._mark_private()

    def clear(self) -> None:
        with self._opened():
            w.EmptyClipboard()

    def get_text(self) -> str | None:
        with self._opened():
            handle = w.GetClipboardData(CF_UNICODETEXT)
            data = self._read_handle(handle) if handle else None
        if not data:
            return None
        return data.decode("utf-16-le", errors="replace").split("\0", 1)[0]

    def set_text(self, text: str, private: bool = True) -> None:
        with self._opened():
            w.EmptyClipboard()
            self._put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
            if private:
                self._mark_private()

    def is_vscode_empty_selection(self) -> bool:
        """VS Code при пустом выделении копирует всю строку – это не выделение."""
        if not w.IsClipboardFormatAvailable(self._vscode_format):
            return False
        with self._opened():
            handle = w.GetClipboardData(self._vscode_format)
            data = self._read_handle(handle) if handle else None
        flat = (data or b"").replace(b"\0", b"").replace(b" ", b"")
        return b'"isFromEmptySelection":true' in flat

    def _mark_private(self) -> None:
        for fmt, data in self._private_formats:
            try:
                self._put(fmt, data)
            except ClipboardError:
                pass


# --- Сценарии захвата и вставки ---------------------------------------------------

class TextCapture:
    def __init__(self, clipboard: Clipboard):
        self.clipboard = clipboard

    def copy_selection(self, settings: CaptureSettings) -> Captured | None:
        """Копирует выделение. None – выделенного текста нет (буфер уже восстановлен)."""
        snapshot = None
        if settings.restore_clipboard:
            try:
                snapshot = self.clipboard.snapshot()
            except ClipboardError as exc:
                log.warning("Не удалось сохранить буфер обмена: %s", exc)

        text = None
        for attempt in range(settings.copy_retries + 1):
            self.clipboard.clear()
            sequence = self.clipboard.sequence()
            _send_ctrl(VK_C)
            text = self._wait_for_text(sequence, settings.copy_timeout_ms)
            if text is not None:
                break
            log.info("Попытка копирования %d: текст не появился", attempt + 1)

        captured = Captured(text or "", snapshot)
        if text is not None and self.clipboard.is_vscode_empty_selection():
            log.info("VS Code скопировал строку без выделения – считаю, что выделения нет")
            text = None
        if text is None or not text.strip():
            self.restore(captured)
            return None
        captured.text = text.replace("\r\n", "\n")
        return captured

    def _wait_for_text(self, sequence: int, timeout_ms: int) -> str | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            time.sleep(0.015)
            if self.clipboard.sequence() == sequence:
                continue
            try:
                text = self.clipboard.get_text()
            except ClipboardError:
                continue
            if text:
                return text
        return None

    def paste(self, text: str, captured: Captured, settings: CaptureSettings) -> None:
        self.clipboard.set_text(text.replace("\n", "\r\n"))
        our_sequence = self.clipboard.sequence()
        _send_ctrl(VK_V)
        if captured.snapshot is None:
            return
        # Приложение читает буфер асинхронно после Ctrl+V – даём ему время.
        time.sleep(settings.restore_delay_ms / 1000)
        if self.clipboard.sequence() != our_sequence:
            log.info("Буфер изменили во время вставки – исходное содержимое не возвращаю")
            return
        self.restore(captured)

    def paste_text(self, text: str, hotkey_vk: int | None, settings: CaptureSettings) -> None:
        """Вставляет текст в позицию курсора, не копируя выделение (для диктовки)."""
        release_keys(hotkey_vk, settings.modifier_release_timeout_ms)
        snapshot = None
        if settings.restore_clipboard:
            try:
                snapshot = self.clipboard.snapshot()
            except ClipboardError as exc:
                log.warning("Не удалось сохранить буфер обмена: %s", exc)
        self.paste(text, Captured(text, snapshot), settings)

    def put_result(self, text: str) -> None:
        """Оставляет результат в буфере (без пометки «приватно» – пользователь вставит сам)."""
        self.clipboard.set_text(text.replace("\n", "\r\n"), private=False)

    def restore(self, captured: Captured | None) -> None:
        if captured is None or captured.snapshot is None:
            return
        try:
            self.clipboard.restore(captured.snapshot)
        except ClipboardError as exc:
            log.warning("Не удалось восстановить буфер обмена: %s", exc)
