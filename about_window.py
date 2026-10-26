"""Окно «О программе»: версия, описание и автор. Подпись автора – только здесь, мелко внизу."""
from __future__ import annotations

import tkinter as tk
import webbrowser

import widgets
from config import APP_NAME, AUTHOR, UPDATE_REPO, VERSION
from theme import COLORS, Chrome
from widgets import sans

DESCRIPTION = ("Исправляет и улучшает выделенный текст в любой программе одним сочетанием клавиш, "
               "а ещё превращает надиктованную речь в аккуратный текст.")
PRIVACY = "Тексты и голос обрабатываются нейросетью прямо на вашем компьютере и никуда не отправляются."


def repo_url(repo: str | None) -> str | None:
    return f"https://github.com/{repo}" if repo else None


class AboutWindow(Chrome):
    def __init__(self, root: tk.Tk):
        self.window = tk.Toplevel(root)
        super().__init__(self.window, f"О программе {APP_NAME}", min_size=(560, 340))
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.bind("<Escape>", lambda e: self.window.destroy())
        self.set_titles(APP_NAME, f"Версия {VERSION}")
        url = repo_url(UPDATE_REPO)
        self.buttons(("Закрыть", self.window.destroy),
                     ("Страница проекта", lambda: webbrowser.open(url)) if url else None)
        self.footer_note.configure(text=f"by {AUTHOR}", fg=COLORS["faint"])
        self.label(DESCRIPTION, size=11, pady=(self.px(4), self.px(12)))
        note = tk.Frame(self.body, bg=COLORS["bg"])
        note.pack(fill="x")
        widgets.icon_label(note, "lock", 16, COLORS["muted"]).pack(side="left", padx=(self.px(2), self.px(10)))
        widgets.text(note, PRIVACY, "muted", sans(9), wrap=480).pack(side="left", fill="x", expand=True)
        self.center(580, 360)
        self.window.lift()
        self.window.focus_force()
