"""Окно удаления Gloss (запускается из «Параметры → Приложения» как Gloss.exe --uninstall)."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import system
import widgets
from theme import COLORS, Chrome, apply_theme
from updater import format_size
from widgets import mono, sans

log = logging.getLogger(__name__)


class UninstallWindow(Chrome):
    def __init__(self, root: tk.Tk):
        super().__init__(root, "Удаление Gloss", min_size=(620, 420))
        self.root = root
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.busy = False
        self.info = system.uninstall_info()
        self.remove_data = tk.BooleanVar(value=True)
        self.remove_models = tk.BooleanVar(value=True)
        self.ollama_vars = {name: tk.BooleanVar(value=True) for name, _ in self.info.ollama_models}
        root.protocol("WM_DELETE_WINDOW", lambda: None if self.busy else root.destroy())
        self._show_choice()
        root.after(50, self._poll)

    def _show_choice(self) -> None:
        self.clear_body()
        self.set_titles("Удаление Gloss", "Программа, движок нейросети, ярлыки, автозапуск и запись в списке "
                                          "приложений будут удалены.")
        self.heading("Что ещё удалить")
        panel = widgets.Panel(self.body, padding=(16, 12))
        panel.pack(fill="x")
        options = [("Настройки и журналы Gloss", self.remove_data)]
        if self.info.models_bytes:
            options.append((f"Модели нейросети ({format_size(self.info.models_bytes)})", self.remove_models))
        for name, size in self.info.ollama_models:
            suffix = f", {format_size(size)}" if size else ""
            options.append((f"Модель {name} в Ollama (скачана для Gloss{suffix})", self.ollama_vars[name]))
        for index, (text, variable) in enumerate(options):
            widgets.Check(panel.inner, text, variable).pack(fill="x", pady=(self.px(6) if index else 0, 0))
        if self.info.ollama_models:
            self.label("Сама Ollama и другие её модели останутся на месте.", color="muted", size=9,
                       parent=panel.inner, pady=(self.px(6), 0), padx=(self.px(27), 0))
        self.buttons(("Удалить", self._start), ("Отмена", self.root.destroy))

    def _start(self) -> None:
        self.busy = True
        choice = system.UninstallChoice(
            remove_data=self.remove_data.get(),
            remove_models=self.remove_models.get(),
            remove_ollama_models=[m for m, var in self.ollama_vars.items() if var.get()],
        )
        self.clear_body()
        self.set_titles("Удаляю Gloss…", "Это займёт несколько секунд.")
        self.status = widgets.text(self.body, "", "muted", mono(9))
        self.status.pack(fill="x")
        bar = widgets.ProgressBar(self.body)
        bar.pack(fill="x", pady=self.px(10))
        bar.set(None)
        self.buttons(None, None)

        def work():
            try:
                left = system.perform_uninstall(choice, report=lambda text: self.events.put(("status", text)))
                self.events.put(("done", left))
            except Exception as exc:  # журнал уже может быть удалён – показываем ошибку в окне
                self.events.put(("done", [f"ошибка: {exc}"]))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "status":
                self.status.configure(text=payload)
            elif event == "done":
                self._show_done(payload)
        self.root.after(50, self._poll)

    def _show_done(self, left: list[str]) -> None:
        self.busy = False
        self.clear_body()
        if left:
            self.set_titles("Gloss удалён не полностью", "Эти файлы или записи удалить не удалось. Их можно удалить вручную:")
            for item in left:
                self.label(f"• {item}", color="warn", size=9)
        else:
            self.set_titles("Gloss удалён", "Спасибо, что пользовались Gloss.")
            row = tk.Frame(self.body, bg=COLORS["bg"])
            row.pack(fill="x", pady=(0, self.px(8)))
            widgets.status_icon(row, "ok", 18).pack(side="left", anchor="n", padx=(self.px(2), self.px(10)))
            widgets.text(row, "Программа удалена полностью, ничего лишнего не осталось.", "ok",
                         sans(10, bold=True)).pack(side="left")
            kept = [] if self.remove_data.get() else ["настройки"]
            if self.info.models_bytes and not self.remove_models.get():
                kept.append("модели")
            if kept:
                self.label(f"По вашему выбору оставлены {' и '.join(kept)}: они пригодятся, если установите Gloss снова.",
                           color="muted", size=9)
        self.buttons(("Закрыть", self.root.destroy), None)


def run(quiet: bool) -> None:
    if quiet:  # тихое удаление: всё, что относится к Gloss; модели в Ollama пользователя остаются
        left = system.perform_uninstall(system.UninstallChoice(remove_data=True, remove_models=True))
        if left:
            log.warning("После удаления остались: %s", left)
        return
    root = tk.Tk()
    apply_theme(root)
    window = UninstallWindow(root)
    window.center(620, 420)
    root.mainloop()
