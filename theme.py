"""Общий вид окон Gloss: установщик, удаление, обновления, знакомство с программой."""
from __future__ import annotations

import tkinter as tk
import tkinter.font

import winapi
import widgets
from ui import make_icon
from widgets import COLORS, GLYPHS, Panel, PillButton, mono, sans  # noqa: F401  (COLORS, GLYPHS – для окон)

_themed = False


def apply_theme(root: tk.Tk) -> None:
    """Шрифты и цвета – один раз на интерпретатор Tk."""
    global _themed
    if not _themed:
        widgets.load_fonts()
        _themed = True
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkinter.font.nametofont(name).configure(family="Segoe UI", size=10)
        except tk.TclError:
            pass
    root.configure(bg=COLORS["bg"])


def dark_frame(window: tk.Misc) -> None:
    """Тёмный заголовок окна Windows в цвет фона."""
    window.update_idletasks()
    try:
        hwnd = int(window.wm_frame(), 16)
    except (tk.TclError, ValueError):
        return
    winapi.dark_title_bar(hwnd, COLORS["bg"], COLORS["muted"], COLORS["line"])


class Chrome:
    """Окно с шапкой (значок, заголовок, подзаголовок), телом и кнопками внизу."""

    def __init__(self, window: tk.Tk | tk.Toplevel, title: str, min_size: tuple[int, int] = (700, 560)):
        self.window = window
        self.scale = window.winfo_fpixels("1i") / 96
        window.title(title)
        window.configure(bg=COLORS["bg"])
        window.minsize(self.px(min_size[0]), self.px(min_size[1]))
        dark_frame(window)
        self._small_icon = widgets.photo(make_icon("idle", 32), window)
        window.iconphoto(False, self._small_icon)

        header = Panel(window, radius=26, padding=(12, 12))
        header.pack(fill="x", padx=self.px(16), pady=(self.px(14), 0))
        self._icon = widgets.photo(make_icon("idle", self.px(52), bg=COLORS["panel"]), window)
        tk.Label(header.inner, image=self._icon, bg=COLORS["panel"], bd=0).pack(side="left", anchor="n")
        titles = tk.Frame(header.inner, bg=COLORS["panel"])
        titles.pack(side="left", fill="x", expand=True, padx=(self.px(16), self.px(8)))
        self.title = tk.Label(titles, bg=COLORS["panel"], fg=COLORS["text"], font=mono(14, bold=True), anchor="w")
        self.title.pack(fill="x", pady=(self.px(2), 0))
        self.subtitle = tk.Label(titles, bg=COLORS["panel"], fg=COLORS["muted"], font=sans(10), anchor="w",
                                 justify="left", wraplength=self.px(560))
        self.subtitle.pack(fill="x")

        footer = tk.Frame(window, bg=COLORS["bg"])
        footer.pack(side="bottom", fill="x")
        tk.Frame(footer, bg=COLORS["line"], height=max(1, self.px(1))).pack(fill="x")
        bar = tk.Frame(footer, bg=COLORS["bg"], padx=self.px(20), pady=self.px(12))
        bar.pack(fill="x")
        self.secondary = PillButton(bar, kind="secondary")
        self.primary = PillButton(bar, kind="primary", min_width=150)
        self.footer_note = tk.Label(bar, bg=COLORS["bg"], fg=COLORS["muted"], font=mono(9))
        self.footer_note.pack(side="left")

        self.body = tk.Frame(window, bg=COLORS["bg"], padx=self.px(24), pady=self.px(14))
        self.body.pack(fill="both", expand=True)
        window.bind("<Return>", self._on_return, add="+")

    def px(self, value: float) -> int:
        return int(value * self.scale)

    def _on_return(self, event) -> None:
        if not isinstance(event.widget, tk.Text) and self.primary.winfo_ismapped():
            self.primary.invoke()

    def set_titles(self, title: str, subtitle: str) -> None:
        self.title.configure(text=title)
        self.subtitle.configure(text=subtitle)

    def clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.window.after_idle(self.fit_window)  # когда новая страница будет построена

    def fit_window(self) -> None:
        """Увеличивает окно, если страница в него не помещается (но не больше экрана)."""
        win = self.window
        win.update_idletasks()
        need = min(win.winfo_reqheight(), win.winfo_screenheight() - self.px(80))
        if need > win.winfo_height():
            y = max(0, min(win.winfo_y(), win.winfo_screenheight() - need - self.px(60)))
            win.geometry(f"{win.winfo_width()}x{need}+{win.winfo_x()}+{y}")

    def center(self, width: int, height: int) -> None:
        win = self.window
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), self.px(width)), max(win.winfo_reqheight(), self.px(height))
        win.geometry(f"{w}x{h}+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")

    def buttons(self, primary: tuple[str, object] | None, secondary: tuple[str, object] | None) -> None:
        for button, spec in ((self.primary, primary), (self.secondary, secondary)):
            if spec is None:
                button.pack_forget()
                continue
            text, command = spec
            button.configure(text=text, command=command, state="normal")
        # Основная кнопка всегда справа, второстепенная левее неё с зазором.
        self.primary.pack_forget()
        self.secondary.pack_forget()
        if primary:
            self.primary.pack(side="right")
        if secondary:
            self.secondary.pack(side="right", padx=(0, self.px(14)) if primary else 0)

    def label(self, text: str, color: str = "text", bold: bool = False, size: int = 10, parent: tk.Misc | None = None,
              **pack) -> tk.Label:
        """Строка текста в теле окна с переносом по ширине."""
        widget = widgets.text(parent or self.body, text, color, sans(size, bold), wrap=640)
        widget.pack(fill="x", **({"pady": (0, self.px(6))} | pack))
        return widget

    def heading(self, text: str, parent: tk.Misc | None = None, **pack) -> tk.Label:
        """Заголовок раздела моноширинным шрифтом."""
        widget = widgets.text(parent or self.body, text, "text", mono(11, bold=True))
        widget.pack(fill="x", **({"pady": (0, self.px(8))} | pack))
        return widget

    def ask(self, title: str, message: str, yes: str = "Да", no: str = "Нет") -> bool:
        """Тёмное окно подтверждения вместо системного (оно было бы светлым)."""
        dialog = tk.Toplevel(self.window, bg=COLORS["bg"])
        dialog.title(title)
        dialog.transient(self.window)
        dialog.resizable(False, False)
        dark_frame(dialog)
        box = tk.Frame(dialog, bg=COLORS["bg"], padx=self.px(22), pady=self.px(18))
        box.pack(fill="both", expand=True)
        widgets.text(box, title, "text", mono(11, bold=True)).pack(fill="x")
        widgets.text(box, message, "muted", sans(10), wrap=380).pack(fill="x", pady=(self.px(6), self.px(16)))
        result = {"value": False}

        def finish(value: bool) -> None:
            result["value"] = value
            dialog.destroy()

        row = tk.Frame(box, bg=COLORS["bg"])
        row.pack(fill="x")
        PillButton(row, yes, lambda: finish(True), kind="primary", min_width=110).pack(side="right")
        PillButton(row, no, lambda: finish(False), min_width=110).pack(side="right", padx=(0, self.px(14)))
        dialog.bind("<Escape>", lambda e: finish(False))
        dialog.update_idletasks()
        x = self.window.winfo_rootx() + (self.window.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.window.winfo_rooty() + (self.window.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_force()
        self.window.wait_window(dialog)
        return result["value"]
