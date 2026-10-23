"""Иконка в трее, уведомления и плашка у курсора во время обработки.

tkinter живёт в главном потоке; другие потоки общаются с ним только через
очередь (Ui.post). Окно индикатора никогда не получает фокус: иначе Ctrl+V
ушёл бы в него, а не в окно с выделенным текстом.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFont

import widgets
import winapi as w

log = logging.getLogger(__name__)

_STATES = ("idle", "busy", "warning")  # готов · идёт обработка · модель недоступна


def make_icon(state: str = "idle", size: int = 64, bg: str | None = None) -> Image.Image:
    """Значок Gloss: «G» на круге. Готов – тёплый градиент, обработка – градиентное кольцо, недоступна – серый.

    bg – цвет фона под значком (для окон); без него фон прозрачный (для трея и exe).
    """
    scale = 4  # рисуем крупно и уменьшаем – так края получаются гладкими
    big = size * scale
    image = Image.new("RGBA", (big, big), bg or (0, 0, 0, 0))
    disc = Image.new("L", (big, big), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, big - 1, big - 1), fill=255)
    gradient = widgets.accent_gradient((big, big)).convert("RGBA")
    letter = widgets.COLORS["bg"]
    if state == "idle":
        image.paste(gradient, mask=disc)
    elif state == "busy":
        image.paste(gradient, mask=disc)
        inner = Image.new("L", (big, big), 0)
        k = big * 0.13
        ImageDraw.Draw(inner).ellipse((k, k, big - 1 - k, big - 1 - k), fill=255)
        image.paste(Image.new("RGBA", (big, big), widgets.COLORS["raised"]), mask=inner)
        letter = widgets.COLORS["text"]
    else:
        image.paste(Image.new("RGBA", (big, big), "#6b6b73"), mask=disc)
        letter = widgets.COLORS["text"]
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(big * (0.6 if state == "busy" else 0.66)))
    except OSError:
        font = ImageFont.load_default(size=int(big * 0.66))
    ImageDraw.Draw(image).text((big / 2, big * 0.47), "G", font=font, fill=letter, anchor="mm")
    if state == "warning":
        r = big * 0.17
        cx, cy = big - r - big * 0.02, big - r - big * 0.02
        ImageDraw.Draw(image).ellipse((cx - r, cy - r, cx + r, cy + r), fill=widgets.COLORS["fail"],
                                      outline=bg or widgets.COLORS["bg"], width=int(big * 0.05))
    return image.resize((size, size), Image.LANCZOS)


def export_ico(path: str) -> None:
    """Иконка для exe (используется в build.bat)."""
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    make_icon("idle", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


class TrayIcon:
    """Обёртка над pystray: свой поток, смена состояния, уведомления."""

    def __init__(self, title: str, menu: pystray.Menu):
        self._images = {state: make_icon(state) for state in _STATES}
        self._title = title
        self._menu = menu
        self._ready = threading.Event()
        self._icon: pystray.Icon | None = None
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        self._icon = pystray.Icon("Gloss", self._images["idle"], self._title, menu=self._menu)

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            self._ready.set()

        self._icon.run(setup=setup)

    def set_state(self, state: str, tooltip: str) -> None:
        if not self._icon:
            return
        self._icon.icon = self._images[state]
        self._icon.title = tooltip[:127]

    def refresh_menu(self) -> None:
        if self._icon:
            self._icon.update_menu()

    def notify(self, title: str, message: str) -> None:
        log.info("Уведомление: %s", title)  # текст уведомления может содержать данные – не логируем
        if not self._ready.wait(5) or not self._icon:
            return
        try:
            self._icon.notify(message[:250] or " ", title[:60])
        except Exception:
            log.exception("Не удалось показать уведомление")

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()


class _Overlay:
    """Маленькая плашка у курсора: кольцо-индикатор, режим, прогресс и подсказка про Esc."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.window: tk.Toplevel | None = None
        self.hwnd = None
        self._frame = 0
        self._tick_job = None
        self._title = ""

    def _build(self) -> None:
        colors = widgets.COLORS
        bg = colors["panel"]
        win = tk.Toplevel(self.root, bg=bg)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        scale = widgets.scale_of(win)
        body = tk.Frame(win, bg=bg, padx=int(14 * scale), pady=int(10 * scale))
        body.pack(fill="both", expand=True)
        self._frames = [widgets.photo(f, win) for f in widgets.spinner_frames(int(20 * scale), bg)]
        self.spinner = tk.Label(body, bg=bg, bd=0, image=self._frames[0])
        self.spinner.pack(side="left", padx=(0, int(12 * scale)))
        text = tk.Frame(body, bg=bg)
        text.pack(side="left", fill="both", expand=True)
        self.title_label = tk.Label(text, bg=bg, fg=colors["text"], font=widgets.mono(10, bold=True), anchor="w")
        self.title_label.pack(fill="x")
        self.detail_label = tk.Label(text, bg=bg, fg=colors["muted"], font=widgets.sans(9), anchor="w")
        self.detail_label.pack(fill="x")
        win.update_idletasks()
        self.hwnd = int(win.wm_frame(), 16)
        style = w.GetWindowLongPtrW(self.hwnd, w.GWL_EXSTYLE)
        w.SetWindowLongPtrW(self.hwnd, w.GWL_EXSTYLE, style | w.WS_EX_NOACTIVATE | w.WS_EX_TOOLWINDOW | w.WS_EX_TOPMOST)
        # Windows 11 скругляет углы и рисует тонкую рамку; в Windows 10 плашка просто прямоугольная.
        w.set_window_attribute(self.hwnd, w.DWMWA_WINDOW_CORNER_PREFERENCE, w.DWMWCP_ROUND)
        w.set_window_attribute(self.hwnd, w.DWMWA_BORDER_COLOR, w.colorref(colors["line_strong"]))
        self.window = win

    def show(self, title: str, detail: str) -> None:
        if self.window is None:
            self._build()
        self._title = title
        self.update(detail)
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x, y, work = w.work_area_at_cursor()
        x = max(work.left, min(x + 16, work.right - width - 8))
        y = y + 24 if y + 24 + height <= work.bottom else y - height - 12
        y = max(work.top, y)
        # Показываем сами через WinAPI, без активации: deiconify у Tk может забрать фокус.
        w.SetWindowPos(self.hwnd, w.HWND_TOPMOST, x, y, 0, 0, w.SWP_NOSIZE | w.SWP_NOACTIVATE | w.SWP_SHOWWINDOW)
        w.ShowWindow(self.hwnd, w.SW_SHOWNOACTIVATE)
        if self._tick_job is None:
            self._tick()

    def update(self, detail: str | None = None, title: str | None = None) -> None:
        if self.window is None:
            return
        if title is not None:
            self._title = title
        if detail is not None:
            self.detail_label.configure(text=detail)
        self.title_label.configure(text=self._title)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._frames)
        self.spinner.configure(image=self._frames[self._frame])
        self._tick_job = self.root.after(80, self._tick)

    def hide(self) -> None:
        if self._tick_job is not None:
            self.root.after_cancel(self._tick_job)
            self._tick_job = None
        if self.hwnd:
            w.ShowWindow(self.hwnd, w.SW_HIDE)


class Ui:
    """Главный поток с tkinter. Методы show/update/hide/quit можно вызывать из любых потоков."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._root: tk.Tk | None = None
        self._overlay: _Overlay | None = None

    def run(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()
        self._overlay = _Overlay(self._root)
        self._root.after(50, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        while True:
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception:
                log.exception("Ошибка в интерфейсе")
        self._root.after(50, self._poll)

    @property
    def root(self) -> tk.Tk:
        """Корневое окно Tk – только для кода, выполняемого через post()."""
        return self._root

    def post(self, action: Callable[[], None]) -> None:
        self._queue.put(action)

    def show_progress(self, title: str, detail: str) -> None:
        self.post(lambda: self._overlay.show(title, detail))

    def update_progress(self, detail: str | None = None, title: str | None = None) -> None:
        self.post(lambda: self._overlay.update(detail, title))

    def hide_progress(self) -> None:
        self.post(lambda: self._overlay.hide())

    def quit(self) -> None:
        self.post(lambda: self._root.quit())
