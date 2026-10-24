"""Окно «Знакомство с Gloss»: показывается при первом запуске и из меню «Как пользоваться»."""
from __future__ import annotations

import tkinter as tk
from typing import Callable

from config import Config
from modes import Mode
import widgets
from theme import COLORS, Chrome
from widgets import PillButton, mono, sans

SAMPLE = ("привет! я вчера ходил в магазин и купил там малоко, хлеб и ище немного фруктов. "
          "вобщем всё было харошо, но очередь была очень очень длинная")
SPOKEN_SAMPLE = "Ну, короче, давай созвонимся завтра в три, нет, в четыре."


def dictation_problem(mode: Mode, speech_state: str, model_problem: str | None) -> str | None:
    """Почему диктовку сейчас не попробовать (None – можно).

    speech_state: ready – распознавание установлено, installing – загружается, missing – его нет.
    Если недоступна только модель, диктовка работает, но вставляет текст без улучшения."""
    if not mode.enabled:
        return "Диктовка выключена. Включить: меню значка → Настройки → Режимы."
    if mode.hotkey is None:
        return "У диктовки нет сочетания. Задать: меню значка → Настройки → Сочетания клавиш…"
    if speech_state == "installing":
        return "Распознавание речи ещё загружается. Подождите уведомления «Диктовка готова»."
    if speech_state != "ready":
        return "Распознавание речи не установлено: Gloss загрузит его сам, это займёт пару минут."
    if model_problem:
        return f"{model_problem.rstrip('. ')}. Диктовка вставит текст без улучшения."
    return None


class OnboardingWindow(Chrome):
    PAGES = 4

    def __init__(self, root: tk.Tk, cfg: Config, health: Callable[[], str | None], on_close: Callable[[], None],
                 speech_state: Callable[[], str] = lambda: "ready"):
        self.window = tk.Toplevel(root)
        super().__init__(self.window, "Знакомство с Gloss", min_size=(660, 440))
        self.cfg = cfg
        self.health = health      # None – всё готово, иначе текст проблемы с моделью
        self.speech_state = speech_state  # ready / installing / missing, см. dictation_problem
        self.on_close = on_close
        self.page = 0
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._show()
        self.center(680, 460)
        self.window.lift()
        self.window.focus_force()

    def close(self) -> None:
        self.on_close()
        self.window.destroy()

    def _dots(self) -> None:
        """Точки страниц: текущая – градиентная пилюля, остальные – маленькие кружки."""
        dots = tk.Frame(self.body, bg=COLORS["bg"])
        dots.pack(side="bottom", pady=(self.px(10), 0))
        self._dot_photos = []
        for i in range(self.PAGES):
            w, h = (self.px(22) if i == self.page else self.px(8)), self.px(8)
            if i == self.page:
                image = widgets.accent_pill(w, h, COLORS["bg"])
            else:
                image = widgets.rounded(w, h, h // 2, COLORS["line_strong"], None, COLORS["bg"])
            self._dot_photos.append(widgets.photo(image, dots))
            tk.Label(dots, image=self._dot_photos[-1], bg=COLORS["bg"], bd=0).pack(side="left", padx=self.px(3))

    def _show(self) -> None:
        self.clear_body()
        self._dots()
        [self._page_intro, self._page_try, self._page_dictate, self._page_hotkeys][self.page]()
        back = ("←  Назад", lambda: self._go(-1)) if self.page else ("Пропустить", self.close)
        forward = ("Далее  →", lambda: self._go(1)) if self.page < self.PAGES - 1 else ("Начать работу", self.close)
        self.buttons(forward, back)

    def _go(self, delta: int) -> None:
        self.page += delta
        self._show()

    # --- Страницы ---

    def _page_intro(self) -> None:
        self.set_titles("Добро пожаловать в Gloss", "Исправляет и улучшает текст в любой программе одним сочетанием клавиш.")
        steps = tk.Frame(self.body, bg=COLORS["bg"])
        steps.pack(fill="x", pady=(self.px(4), self.px(14)))
        names = [m.name.lower() for m in self._modes()]
        choice = ", ".join(names[:-1]) + f" или {names[-1]}" if len(names) > 1 else "".join(names)
        for column, (number, title, text) in enumerate((
            ("1", "Выделите текст", "в Word, браузере, мессенджере, почте или где угодно"),
            ("2", "Нажмите сочетание", f"своё для каждого режима: {choice}" if choice else "у курсора появится плашка Gloss"),
            ("3", "Готово", "исправленный текст встанет на место выделенного"),
        )):
            cell = widgets.Panel(steps, padding=(14, 14))
            cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else self.px(10), 0))
            steps.columnconfigure(column, weight=1, uniform="step")
            badge = tk.Label(cell.inner, bg=COLORS["panel"], bd=0)
            badge.photo = widgets.photo(widgets.step_icon("number", self.px(26), COLORS["panel"], number), badge)
            badge.configure(image=badge.photo)
            badge.pack(anchor="w")
            widgets.text(cell.inner, title, "text", mono(10, bold=True), wrap=170).pack(fill="x", pady=(self.px(10), self.px(2)))
            widgets.text(cell.inner, text, "muted", sans(9), wrap=170).pack(fill="x")
        dictation = self.cfg.dictation.mode
        if dictation.enabled and dictation.hotkey:
            row = tk.Frame(self.body, bg=COLORS["bg"])
            row.pack(fill="x", pady=(0, self.px(4)))
            widgets.text(row, "А ещё текст можно надиктовать:", "text", sans(10, bold=True)).pack(side="left")
            widgets.keycaps(row, str(dictation.hotkey)).pack(side="left", padx=self.px(10))
            widgets.text(self.body, "Попробуете через страницу: скажете фразу, и Gloss вставит её без «ну» и оговорок.",
                         "muted", sans(9), wrap=600).pack(fill="x", pady=(0, self.px(14)))
        note = tk.Frame(self.body, bg=COLORS["bg"])
        note.pack(fill="x")
        widgets.icon_label(note, "lock", 16, COLORS["muted"]).pack(side="left", padx=(self.px(2), self.px(10)))
        widgets.text(note, "Тексты и голос обрабатываются нейросетью прямо на вашем компьютере и никуда не отправляются.",
                     "muted", sans(9), wrap=560).pack(side="left", fill="x", expand=True)

    def _modes(self) -> list:
        return [m for m in self.cfg.modes if m.enabled and m.hotkey]

    def _page_try(self) -> None:
        self.set_titles("Попробуйте прямо здесь", "Ниже текст с ошибками. Попробуйте на нём все режимы Gloss.")
        row = tk.Frame(self.body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, self.px(8)))
        widgets.text(row, "Выделите весь текст", "text", sans(10, bold=True)).pack(side="left")
        widgets.keycaps(row, "Ctrl+A").pack(side="left", padx=(self.px(10), self.px(10)))
        widgets.text(row, "и нажмите одно из сочетаний:", "text", sans(10, bold=True)).pack(side="left")
        widgets.hotkey_table(self.body, [(str(m.hotkey), m.name) for m in self._modes()]).pack(
            fill="x", padx=(self.px(2), 0), pady=(0, self.px(8)))
        problem = self.health()
        if problem:
            self._note(f"Сейчас не получится: {problem}", "warn")
        panel = widgets.Panel(self.body, padding=(12, 10))
        panel.pack(fill="x", pady=(0, self.px(10)))
        text = tk.Text(panel.inner, height=4, wrap="word", font=sans(11), relief="flat", bd=0, highlightthickness=0,
                       bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["accent"],
                       selectbackground="#5a3b26", selectforeground=COLORS["text"], undo=True)
        text.pack(fill="x")
        text.insert("1.0", SAMPLE)
        _bind_clipboard_keys(text)
        result = tk.Frame(self.body, bg=COLORS["bg"])
        result.pack(fill="x")

        def restore() -> None:
            text.delete("1.0", "end")
            text.insert("1.0", SAMPLE)
            for child in result.winfo_children():
                child.destroy()
            text.focus_set()

        def watch() -> None:
            if not text.winfo_exists():
                return
            if text.get("1.0", "end-1c").strip() != SAMPLE and not result.winfo_children():
                PillButton(result, "Вернуть исходный текст", restore, height=32).pack(side="right", anchor="n")
                self._note("Получилось! Верните исходный текст и попробуйте другой режим. Так же это работает "
                           "в любой другой программе.", "ok", parent=result)
                self.window.after_idle(self.fit_window)
            text.after(300, watch)

        watch()
        text.focus_set()

    def _page_dictate(self) -> None:
        mode = self.cfg.dictation.mode
        self.set_titles("Попробуйте диктовку", "Скажите фразу, и Gloss вставит её туда, где стоит курсор.")
        row = tk.Frame(self.body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, self.px(4)))
        widgets.text(row, "Нажмите", "text", sans(10, bold=True)).pack(side="left")
        widgets.keycaps(row, str(mode.hotkey or "…")).pack(side="left", padx=self.px(10))
        widgets.text(row, "скажите фразу и нажмите ещё раз.", "text", sans(10, bold=True)).pack(side="left")
        widgets.text(self.body, "Можно и удерживать: тогда запись идёт, пока клавиши нажаты.", "muted", sans(9)
                     ).pack(fill="x", pady=(0, self.px(10)))
        hint = widgets.Panel(self.body, padding=(12, 8))
        hint.pack(fill="x", pady=(0, self.px(10)))
        widgets.text(hint.inner, "Например, скажите вслух:", "muted", sans(9)).pack(fill="x")
        widgets.text(hint.inner, f"«{SPOKEN_SAMPLE}»", "text", sans(11), wrap=580).pack(fill="x", pady=(self.px(2), 0))
        problem = dictation_problem(mode, self.speech_state(), self.health())
        if problem:
            self._note(problem, "warn")
        panel = widgets.Panel(self.body, padding=(12, 10))
        panel.pack(fill="x", pady=(0, self.px(10)))
        text = tk.Text(panel.inner, height=3, wrap="word", font=sans(11), relief="flat", bd=0, highlightthickness=0,
                       bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["accent"],
                       selectbackground="#5a3b26", selectforeground=COLORS["text"], undo=True)
        text.pack(fill="x")
        _bind_clipboard_keys(text)  # диктовка вставляет через Ctrl+V
        result = tk.Frame(self.body, bg=COLORS["bg"])
        result.pack(fill="x")

        def clear() -> None:
            text.delete("1.0", "end")
            for child in result.winfo_children():
                child.destroy()
            text.focus_set()

        def watch() -> None:
            if not text.winfo_exists():
                return
            if text.get("1.0", "end-1c").strip() and not result.winfo_children():
                PillButton(result, "Очистить", clear, height=32).pack(side="right", anchor="n")
                self._note("Получилось! Так же это работает в любом поле ввода: в мессенджере, браузере, почте.",
                           "ok", parent=result)
                self.window.after_idle(self.fit_window)
            text.after(300, watch)

        watch()
        # Курсор сразу в поле: диктовка вставляет туда, где он стоит. С задержкой, чтобы фокус
        # не забрала нажатая кнопка «Далее».
        self.window.after(50, lambda: text.winfo_exists() and text.focus_set())

    def _page_hotkeys(self) -> None:
        self.set_titles("Режимы и подсказки", "Запоминать ничего не нужно: список всегда под рукой в меню значка.")
        panel = widgets.Panel(self.body, padding=(18, 12))
        panel.pack(fill="x", pady=(0, self.px(14)))
        rows = [(str(m.hotkey), m.name) for m in self._modes()]
        dictation = self.cfg.dictation.mode
        if dictation.enabled and dictation.hotkey:
            rows.append((str(dictation.hotkey), f"{dictation.name}: нажмите и говорите, затем нажмите ещё раз"))
        rows.append(("Esc", "отменить, пока идёт обработка"))
        widgets.hotkey_table(panel.inner, rows).pack(fill="x")
        for line in (
            "Значок G живёт в области уведомлений рядом с часами (может прятаться под стрелкой «^»). "
            "В его меню: список сочетаний клавиш, обновления, настройки и это окно («Как пользоваться»). "
            "Сочетания можно поменять: «Настройки» → «Сочетания клавиш…».",
            "Диктовка вставляет текст туда, где стоит курсор. Если держать сочетание, запись идёт, пока клавиши "
            "нажаты. Речь распознаётся прямо на компьютере, а слова-паразиты и оговорки Gloss убирает сам.",
            "Значок оранжевый, когда Gloss готов. Во время обработки вокруг буквы появляется кольцо, "
            "а серый значок с красной точкой означает, что нейросеть недоступна.",
        ):
            self.label(line, color="muted", size=9, pady=(0, self.px(8)))

    def _note(self, message: str, status: str, parent: tk.Misc | None = None) -> None:
        row = tk.Frame(parent or self.body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, self.px(8)))
        widgets.status_icon(row, status, 18).pack(side="left", anchor="n", padx=(self.px(2), self.px(10)))
        widgets.text(row, message, status, sans(10, bold=True), wrap=580).pack(side="left", fill="x", expand=True)


def _bind_clipboard_keys(text: tk.Text) -> None:
    """Tk не распознаёт Ctrl+C/V/X/A в русской раскладке – привязываем по коду клавиши."""
    actions = {0x43: "<<Copy>>", 0x56: "<<Paste>>", 0x58: "<<Cut>>"}

    def on_key(event):
        if event.keycode in actions:
            text.event_generate(actions[event.keycode])
            return "break"
        if event.keycode == 0x41:  # A
            text.tag_add("sel", "1.0", "end-1c")
            return "break"
        if event.keycode == 0x5A:  # Z
            try:
                text.edit_undo()
            except tk.TclError:
                pass
            return "break"
        return None

    text.bind("<Control-KeyPress>", on_key)
