"""Окно «Сочетания клавиш»: изменить сочетание режима, включить или выключить режим.

Пока вводится новое сочетание, сочетания Gloss сняты: иначе Ctrl+Alt+G перехватил бы сам Gloss,
и до окна оно бы не дошло. Клавиши определяются физически, раскладка не важна.
Изменения сразу пишутся в config.json и применяются без перезапуска.
"""
from __future__ import annotations

import logging
import tkinter as tk

import config
import widgets
import winapi as w
from config import ConfigError
from hotkeys import MODIFIER_VKS, VK_ESCAPE, HotkeyError, hotkey_from_keys
from theme import COLORS, Chrome
from widgets import PillButton, mono, sans

log = logging.getLogger(__name__)

_MODIFIER_KEYS = ((0x11, w.MOD_CONTROL, "Ctrl"), (0x12, w.MOD_ALT, "Alt"), (0x10, w.MOD_SHIFT, "Shift"),
                  (0x5B, w.MOD_WIN, "Win"), (0x5C, w.MOD_WIN, "Win"))


def _held_modifiers() -> tuple[int, list[str]]:
    """Какие из Ctrl, Alt, Shift, Win сейчас зажаты (по состоянию клавиатуры, а не по событию Tk)."""
    flags, names = 0, []
    for vk, flag, name in _MODIFIER_KEYS:
        if w.GetAsyncKeyState(vk) & 0x8000 and not flags & flag:
            flags |= flag
            names.append(name)
    return flags, names


class HotkeysWindow(Chrome):
    def __init__(self, root: tk.Tk, app):
        self.window = tk.Toplevel(root)
        super().__init__(self.window, "Сочетания клавиш", min_size=(600, 360))
        self.app = app
        self.capturing: str | None = None  # режим, для которого сейчас вводят сочетание
        self.message: tuple[str, str] | None = None
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<KeyPress>", self._on_key, add="+")
        self.set_titles("Сочетания клавиш", "Нажмите «Изменить», а затем новое сочетание. Изменения применяются сразу.")
        self.buttons(("Готово", self.close), ("Вернуть по умолчанию", self._reset))
        self._render()
        self.center(640, 380)
        self.window.lift()
        self.window.focus_force()

    def _on_return(self, event) -> None:
        # Привязка <Return> точнее <KeyPress>, и Tk вызывает только её: во время ввода Enter –
        # часть сочетания (Ctrl+Enter), а не кнопка «Готово».
        if self.capturing is None:
            super()._on_return(event)
        else:
            self._on_key(event)

    # --- Отрисовка ---

    def _render(self) -> None:
        self.clear_body()
        panel = widgets.Panel(self.body, padding=(16, 10))
        panel.pack(fill="x")
        table = panel.inner
        self._live: tk.Label | None = None
        self._vars = []
        for row, mode in enumerate(self.app.cfg.modes):
            if row:
                tk.Frame(table, bg=COLORS["line"], height=max(1, self.px(1))).grid(
                    row=2 * row - 1, column=0, columnspan=3, sticky="ew", pady=self.px(2))
            enabled = tk.BooleanVar(value=mode.enabled)
            self._vars.append(enabled)  # переменная живёт, пока живёт строка
            widgets.Check(table, mode.name, enabled, command=lambda m=mode.id, v=enabled: self._toggle(m, v.get()),
                          font=sans(10)).grid(row=2 * row, column=0, sticky="w", pady=self.px(6))
            cell = tk.Frame(table, bg=COLORS["panel"])
            cell.grid(row=2 * row, column=1, sticky="w", padx=(self.px(16), self.px(12)))
            if self.capturing == mode.id:
                self._live = widgets.text(cell, "Нажмите сочетание…  (Esc: отмена)", "accent", mono(9, bold=True))
                self._live.pack(side="left")
            elif mode.hotkey:
                widgets.keycaps(cell, str(mode.hotkey)).pack(side="left")
            else:
                widgets.text(cell, "не задано", "faint", sans(9)).pack(side="left")
            capturing = self.capturing == mode.id
            PillButton(table, "Отмена" if capturing else "Изменить",
                       self._cancel_capture if capturing else (lambda m=mode.id: self._start_capture(m)),
                       min_width=110, height=30).grid(row=2 * row, column=2, sticky="e")
        table.columnconfigure(1, weight=1)
        if self.message:
            text, status = self.message
            line = tk.Frame(self.body, bg=COLORS["bg"])
            line.pack(fill="x", pady=(self.px(12), 0))
            widgets.status_icon(line, status, 18).pack(side="left", anchor="n", padx=(self.px(2), self.px(10)))
            widgets.text(line, text, status, sans(10, bold=True), wrap=540).pack(side="left", fill="x", expand=True)
        widgets.text(self.body, "Нужен хотя бы один из Ctrl, Alt или Win и обычная клавиша, например Ctrl+Alt+K. "
                                "Esc занят под отмену обработки.", "muted", sans(9), wrap=600
                     ).pack(fill="x", pady=(self.px(12), 0))

    def _say(self, text: str | None, status: str = "ok") -> None:
        self.message = (text, status) if text else None

    # --- Ввод сочетания ---

    def _start_capture(self, mode_id: str) -> None:
        if self.capturing is None:
            self.app.pause_hotkeys(True)
        self.capturing = mode_id
        self._say(None)
        self._render()
        self.window.focus_force()

    def _cancel_capture(self) -> None:
        if self.capturing is not None:
            self.capturing = None
            self.app.pause_hotkeys(False)
        self._render()

    def _on_key(self, event) -> str | None:
        if self.capturing is None:
            return None
        vk = event.keycode
        if vk == VK_ESCAPE:
            self._cancel_capture()
            return "break"
        flags, names = _held_modifiers()
        if vk in MODIFIER_VKS:
            if self._live is not None:
                self._live.configure(text=" + ".join(names + ["…"]))
            return "break"
        try:
            hotkey = hotkey_from_keys(flags, vk)
            if not flags & (w.MOD_CONTROL | w.MOD_ALT | w.MOD_WIN) and not 0x70 <= vk <= 0x87:
                raise HotkeyError("нужен Ctrl, Alt или Win: одного Shift мало, он нужен для набора текста")
        except HotkeyError as exc:
            self._say(f"Не подходит: {exc}.", "warn")
            self._render()
            return "break"
        mode_id = self.capturing
        other = next((m for m in self.app.cfg.modes if m.id != mode_id and m.hotkey and m.hotkey.text == hotkey.text),
                     None)
        if other is not None:
            self._say(f"{hotkey} уже у режима «{other.name}». Выберите другое сочетание.", "warn")
        elif self.app.hotkeys.busy_elsewhere(hotkey):
            self._say(f"{hotkey} уже занято другой программой. Выберите другое сочетание.", "warn")
        else:
            self.capturing = None
            self._apply(lambda: config.set_mode_hotkey(self.app.cfg.path, mode_id, hotkey.text.lower()),
                        f"Готово: «{self.app.cfg.mode(mode_id).name}» теперь по {hotkey}.")
            return "break"
        self._render()
        return "break"

    # --- Изменения ---

    def _apply(self, change, done: str) -> None:
        """Записывает изменение в config.json и применяет его (заодно возвращает сочетания Gloss)."""
        try:
            change()
        except (ConfigError, OSError) as exc:
            log.warning("Не удалось изменить config.json: %s", exc)
            self.app.pause_hotkeys(False)
            self._say(f"Не удалось сохранить: {exc}", "fail")
        else:
            problems = self.app.reload_config(notify=False)
            self._say("\n".join(problems), "warn") if problems else self._say(done)
        self._render()

    def _toggle(self, mode_id: str, enabled: bool) -> None:
        name = self.app.cfg.mode(mode_id).name
        self._cancel_capture()
        self._apply(lambda: config.set_mode_enabled(self.app.cfg.path, mode_id, enabled),
                    f"Режим «{name}» {'включён' if enabled else 'выключен'}.")

    def _reset(self) -> None:
        self._cancel_capture()
        self._apply(lambda: config.reset_hotkeys(self.app.cfg.path), "Сочетания возвращены к исходным.")

    def close(self) -> None:
        if self.capturing is not None:
            self.capturing = None
            self.app.pause_hotkeys(False)
        self.window.destroy()
