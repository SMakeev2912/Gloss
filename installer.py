"""Мастер установки Gloss: проверка компьютера → установка → готово.

Запуск без аргументов показывает мастер. Флаги:
  --yes            начать установку сразу после проверки (без вопросов) и закрыться по завершении.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler

import config
import engine
import speech
import sysinfo
import system
import widgets
import winapi
from setup_steps import STEPS, Aborted, Installer, Options, Reporter, SetupError, load_config_or_defaults
from theme import COLORS, Chrome, apply_theme
from widgets import mono, sans

log = logging.getLogger("setup")
MODEL = sysinfo.DEFAULT_MODEL  # установщик ставит одну модель, без выбора

def setup_logging() -> None:
    config.log_dir().mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.log_dir() / "setup.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")


class _QueueReporter(Reporter):
    def __init__(self, events: queue.SimpleQueue):
        self.events = events

    def step(self, step_id, state, note=""):
        self.events.put(("step", step_id, state, note))

    def progress(self, fraction, detail):
        self.events.put(("progress", fraction, detail))


class Wizard(Chrome):
    def __init__(self, root: tk.Tk, auto: bool):
        super().__init__(root, "Установка Gloss")
        self.root = root
        self.auto = auto
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.info: sysinfo.SystemInfo | None = None
        self.installer: Installer | None = None
        self.installing = False
        self.exit_code = 0
        self.autostart = tk.BooleanVar(value=True)
        self.desktop = tk.BooleanVar(value=False)
        self._show_scanning()
        threading.Thread(target=self._scan, name="scan", daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(50, self._poll)

    # --- Страница 1: проверка ---

    def _show_titles(self) -> None:
        installed = system.installed_version()
        if installed:
            self.title.configure(text="Обновление Gloss")
            self.subtitle.configure(text=f"Установлена версия {installed}, будет установлена {config.VERSION}. "
                                         "Ваши настройки сохранятся.")
        else:
            self.title.configure(text="Установка Gloss")
            self.subtitle.configure(text="Улучшение выделенного текста и диктовка в любой программе с помощью "
                                         "локальной нейросети. Тексты и голос не покидают ваш компьютер.")

    def _show_scanning(self) -> None:
        self.page = "scanning"
        self._show_titles()
        self.clear_body()
        self.heading("Проверяю компьютер…")
        bar = widgets.ProgressBar(self.body)
        bar.pack(fill="x", pady=self.px(10))
        bar.set(None)
        self.buttons(("Установить", None), ("Отмена", self.root.destroy))
        self.primary.configure(state="disabled")

    def _scan(self) -> None:
        info = sysinfo.collect()
        self.events.put(("scanned", info))
        info.internet = sysinfo.internet_ok()
        self.events.put(("internet", info.internet))

    def _render_table(self, checks: list[sysinfo.Check]) -> None:
        panel = widgets.Panel(self.body, padding=(16, 8))
        panel.pack(fill="x", pady=(0, self.px(10)))
        table = panel.inner
        for column, title in ((1, "КОМПОНЕНТ"), (2, "ТРЕБУЕТСЯ"), (3, "У ВАС")):
            widgets.text(table, title, "faint", mono(8)).grid(row=0, column=column, sticky="w", padx=(0, self.px(12)),
                                                              pady=(0, self.px(6)))
        for index, check in enumerate(checks):
            row = 1 + index * 2
            tk.Frame(table, bg=COLORS["line"], height=max(1, self.px(1))).grid(row=row, column=0, columnspan=4,
                                                                              sticky="ew")
            pad = (self.px(4), self.px(4))
            widgets.status_icon(table, check.status, 16).grid(row=row + 1, column=0, sticky="nw",
                                                              padx=(0, self.px(10)), pady=(self.px(5), 0))
            widgets.text(table, check.name, "text", sans(9)).grid(row=row + 1, column=1, sticky="nw",
                                                                  padx=(0, self.px(12)), pady=pad)
            widgets.text(table, check.required, "muted", sans(9), wrap=250).grid(row=row + 1, column=2, sticky="new",
                                                                                padx=(0, self.px(12)), pady=pad)
            widgets.text(table, check.actual, "text", sans(9), wrap=260).grid(row=row + 1, column=3, sticky="new",
                                                                              pady=pad)
        table.columnconfigure(3, weight=1)

    def _render_plan(self, info: sysinfo.SystemInfo) -> None:
        """Коротко: что произойдёт после нажатия «Установить»."""
        installed = system.installed_version()
        spec = sysinfo.MODELS[MODEL]
        if info.engine_installed:
            engine_step = "Движок нейросети уже установлен, скачивать его не нужно."
        else:
            engine_step = (f"Скачает движок нейросети ({engine.download_mb(info.arch):.0f} МБ). "
                           "Это небольшая программа, которая запускает модель прямо на компьютере.")
        steps = [
            engine_step,
            f"Модель {spec['title']} уже загружена." if sysinfo.has_model(info, MODEL) else
            f"Загрузит модель {spec['title']} ({sysinfo.format_gb(spec['size_gb'])}): она и правит текст.",
            "Распознавание речи уже установлено." if info.speech_installed else
            f"Скачает распознавание речи ({speech.download_mb(info.arch):.0f} МБ) для диктовки голосом.",
            f"Обновит Gloss до версии {config.VERSION}, настройки сохранятся." if installed else
            "Установит Gloss, добавит ярлык в «Пуск» и, если захотите, автозапуск.",
            "Проверит, что всё работает, и запустит Gloss.",
        ]
        panel = widgets.Panel(self.body, padding=(18, 12))
        panel.pack(fill="x", pady=(0, self.px(14)))
        self.heading("Что сделает установщик", parent=panel.inner, pady=(0, self.px(8)))
        plan = widgets.StepList(panel.inner, icon=22, spacing=5)
        plan.pack(fill="x")
        for number, text in enumerate(steps, start=1):
            row = plan.add("number", str(number))
            widgets.text(row, text, "text", sans(10), wrap=600).pack(fill="x", pady=(self.px(1), 0))
        note = tk.Frame(panel.inner, bg=COLORS["panel"])
        note.pack(fill="x", pady=(self.px(2), 0))
        widgets.icon_label(note, "lock", 16, COLORS["faint"]).pack(side="left", padx=(self.px(3), self.px(15)))
        widgets.text(note, "Права администратора не нужны. Удалить всё можно через «Параметры → Приложения».",
                     "muted", sans(9)).pack(side="left")

    def _verdict(self, text: str, status: str) -> None:
        row = tk.Frame(self.body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(self.px(2), 0))
        widgets.status_icon(row, status, 18).pack(side="left", anchor="n", padx=(self.px(2), self.px(10)))
        widgets.text(row, text, status, sans(10, bold=True), wrap=600).pack(side="left", fill="x", expand=True)

    def _step_heading(self, number: int, title: str) -> None:
        row = tk.Frame(self.body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(0, self.px(10)))
        widgets.text(row, title, "text", mono(11, bold=True)).pack(side="left")
        widgets.tag(row, f"шаг {number} из 2").pack(side="right")

    def _show_system(self) -> None:
        self.page = "system"
        self.clear_body()
        self._show_titles()
        info = self.info
        checks = sysinfo.evaluate_system(info)
        self._render_plan(info)
        self._step_heading(1, "Проверка компьютера")
        self._render_table(checks)
        blockers = sysinfo.system_blockers(info)
        if blockers:
            self._verdict(f"Установка невозможна: {', '.join(blockers).lower()}.", "fail")
        elif any(c.status == "warn" for c in checks if c.name != "Интернет"):
            self._verdict("Установить можно, но есть замечания. На следующем шаге видно, как на вашем ПК "
                          "будет работать модель.", "warn")
        else:
            self._verdict("Компьютер подходит.", "ok")
        self.footer_note.configure(text="")
        self.buttons(("Далее  →", self._show_models), ("Отмена", self.root.destroy))
        self.primary.configure(state="disabled" if blockers else "normal")
        if self.auto and info.internet is not None:
            if blockers:
                self.exit_code = 2
                self.root.after(1500, self.root.destroy)
            else:
                self.root.after(600, self._show_models)

    def _show_models(self) -> None:
        self.page = "models"
        self.clear_body()
        info = self.info
        fit = sysinfo.model_fit(info, MODEL)
        self._step_heading(2, "Модель")
        widgets.text(self.body, "Модель отвечает за правки и работает прямо на компьютере: тексты никуда не "
                                "отправляются. Её скачивают один раз, дальше интернет для работы не нужен.",
                     "muted", sans(9), wrap=640).pack(fill="x", pady=(0, self.px(10)))
        self._model_card(sysinfo.MODELS[MODEL], fit)

        options = tk.Frame(self.body, bg=COLORS["bg"])
        options.pack(fill="x", pady=(self.px(4), 0))
        widgets.Check(options, "Запускать Gloss вместе с Windows", self.autostart).pack(side="left")
        widgets.Check(options, "Ярлык на рабочем столе", self.desktop).pack(side="left", padx=(self.px(24), 0))

        download = sysinfo.download_gb(info, MODEL)
        self.footer_note.configure(text=f"Будет скачано ≈ {download:.1f} ГБ".replace(".", ",") if download else
                                   "Ничего скачивать не нужно")
        self.buttons(("Обновить" if system.installed_version() else "Установить", self._start_install),
                      ("←  Назад", self._show_system))
        pending_internet = info.internet is None and sysinfo.needs_download(info, MODEL)
        blocked = fit.status == "fail" or pending_internet
        self.primary.configure(state="disabled" if blocked else "normal")
        if self.auto and not blocked:
            self.root.after(600, self._start_install)

    def _model_card(self, spec: dict, fit: sysinfo.ModelFit) -> None:
        card = widgets.Panel(self.body, fill=COLORS["selected"], accent=True, padding=(16, 10))
        card.pack(fill="x", pady=(0, self.px(7)))
        inner = card.inner
        head = tk.Frame(inner, bg=inner["bg"])
        head.pack(fill="x")
        widgets.text(head, spec["title"], "text", mono(10, bold=True)).pack(side="left")
        if sysinfo.has_model(self.info, MODEL):
            widgets.tag(head, "уже загружена").pack(side="right")
        widgets.text(inner, spec["about"], "muted", sans(9), wrap=620).pack(fill="x", pady=(self.px(4), 0))
        facts = tk.Frame(inner, bg=inner["bg"])
        facts.pack(fill="x", pady=(self.px(6), 0))
        widgets.status_icon(facts, fit.status, 14).pack(side="left", padx=(0, self.px(7)))
        widgets.text(facts, fit.verdict, fit.status, sans(9, bold=True)).pack(side="left")
        widgets.text(facts, sysinfo.model_requirements(MODEL), "muted", mono(8)).pack(side="right")

    # --- Страница 2: установка ---

    def _start_install(self) -> None:
        if self.installing:
            return
        self.installing = True
        options = Options(model=MODEL, backend="builtin", autostart=self.autostart.get(),
                          desktop_shortcut=self.desktop.get())
        self.title.configure(text="Устанавливаю Gloss")
        self.subtitle.configure(text="Можно заниматься своими делами, всё произойдёт само. "
                                     "Если прервать, при следующем запуске установка продолжится.")
        self.footer_note.configure(text="")
        self.clear_body()
        panel = widgets.Panel(self.body, padding=(18, 14))
        panel.pack(fill="x")
        self.heading("Ход установки", parent=panel.inner, pady=(0, self.px(12)))
        self.steps = widgets.StepList(panel.inner, icon=24, spacing=10)
        self.steps.pack(fill="x")
        self.step_rows: dict[str, tuple[int, tk.Label, tk.Label]] = {}
        for index, (step_id, title) in enumerate(STEPS):
            row = self.steps.add("pending")
            label = widgets.text(row, title, "muted", sans(10))
            label.pack(side="left", pady=(self.px(2), 0))
            note = widgets.text(row, "", "muted", mono(8))
            note.pack(side="right", pady=(self.px(4), 0))
            self.step_rows[step_id] = (index, label, note)
        tk.Frame(panel.inner, bg=COLORS["line"], height=max(1, self.px(1))).pack(fill="x", pady=(self.px(4), self.px(14)))
        self.bar = widgets.ProgressBar(panel.inner)
        self.bar.pack(fill="x")
        self.detail = widgets.text(panel.inner, "", "muted", mono(9))
        self.detail.pack(fill="x", pady=(self.px(8), 0))
        self.buttons(None, ("Отмена", self._on_close))

        self.installer = Installer(options, _QueueReporter(self.events), threading.Event())
        threading.Thread(target=self._install_worker, name="install", daemon=True).start()

    def _install_worker(self) -> None:
        try:
            self.installer.run()
            self.events.put(("finished", None))
        except Aborted:
            self.events.put(("finished", "aborted"))
        except SetupError as exc:
            log.error("Установка не удалась: %s", exc)
            self.events.put(("finished", str(exc)))

    def _on_step(self, step_id: str, state: str, note: str) -> None:
        index, label, note_label = self.step_rows[step_id]
        self.steps.set_state(index, state)
        label.configure(font=sans(10, bold=state == "active"),
                        fg=COLORS["faint"] if state == "skipped" else COLORS["text"] if state != "pending" else COLORS["muted"])
        note_label.configure(text=note, fg=COLORS["warn"] if state == "warn" else COLORS["muted"])
        self.secondary.configure(state="normal")

    def _on_progress(self, fraction: float | None, detail: str) -> None:
        self.bar.set(fraction)
        self.detail.configure(text=detail)

    # --- Страница 3: итог ---

    def _show_finished(self, error: str | None) -> None:
        self.installing = False
        self.clear_body()
        if error is None:
            self.title.configure(text="Gloss установлен и запущен")
            self.subtitle.configure(text="Значок G находится в области уведомлений рядом с часами (может прятаться под «^»).")
            self.heading("Выделите текст в любой программе и нажмите")
            panel = widgets.Panel(self.body, padding=(18, 12))
            panel.pack(fill="x")
            cfg = load_config_or_defaults()
            rows = [(str(m.hotkey), m.name) for m in cfg.modes if m.enabled and m.hotkey]
            widgets.hotkey_table(panel.inner, rows).pack(fill="x")
            dictation = cfg.dictation.mode
            if dictation.enabled and dictation.hotkey:
                self.heading("Чтобы надиктовать, поставьте курсор в поле ввода и нажмите", wrap=640,
                             pady=(self.px(14), self.px(8)))
                panel = widgets.Panel(self.body, padding=(18, 12))
                panel.pack(fill="x")
                widgets.hotkey_table(panel.inner, [(str(dictation.hotkey), "говорите, затем нажмите ещё раз")]
                                     ).pack(fill="x")
            self.label("Esc во время обработки отменяет её. Настройки и выход находятся в меню значка в трее.",
                       color="muted", size=9, pady=(self.px(12), 0))
            for warning in self.installer.warnings:
                self._verdict(warning, "warn")
            self.buttons(("Готово", self.root.destroy), None)
            if self.auto:
                self.root.after(2500, self.root.destroy)
            return

        self.exit_code = 1
        aborted = error == "aborted"
        self.title.configure(text="Установка прервана" if aborted else "Установка не завершена")
        self.subtitle.configure(text="Запустите установщик ещё раз, и он продолжит с того же места.")
        if not aborted:
            self._verdict(error, "fail")
        self.label(f"Журнал установки: {config.log_dir() / 'setup.log'}", color="muted", size=9, pady=(self.px(12), 0))
        self.buttons(("Повторить", self._retry), ("Закрыть", self.root.destroy))
        if self.auto:
            self.root.after(4000, self.root.destroy)

    def _retry(self) -> None:
        self.info = None
        self._show_scanning()
        threading.Thread(target=self._scan, name="scan", daemon=True).start()

    # --- События ---

    def _poll(self) -> None:
        while True:
            try:
                event, *args = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "scanned":
                self.info = args[0]
                self._show_system()
            elif event == "internet" and self.info is not None and not self.installing:
                self.info.internet = args[0]
                (self._show_models if self.page == "models" else self._show_system)()
            elif event == "step":
                self._on_step(*args)
            elif event == "progress":
                self._on_progress(*args)
            elif event == "finished":
                self._show_finished(args[0])
        self.root.after(50, self._poll)

    def _on_close(self) -> None:
        if not self.installing:
            self.root.destroy()
            return
        if self.secondary.instate(["disabled"]):
            return  # уже прерываем
        if self.ask("Прервать установку?", "Скачанное сохранится, и при следующем запуске установка продолжится.",
                    yes="Прервать", no="Продолжить"):
            self.secondary.configure(state="disabled")
            self.detail.configure(text="Прерываю…")
            self.installer.abort()


def main() -> int:
    winapi.set_dpi_awareness()
    setup_logging()
    args = sys.argv[1:]
    log.info("Установщик Gloss %s запущен, аргументы: %s", config.VERSION, args)
    root = tk.Tk()
    apply_theme(root)
    wizard = Wizard(root, auto="--yes" in args)
    wizard.center(720, 640)
    root.mainloop()
    log.info("Установщик завершён, код %s", wizard.exit_code)
    if wizard.installer is None and system.installed_version() is None:
        _remove_own_log()
    return wizard.exit_code


def _remove_own_log() -> None:
    """Установщик открыли и закрыли, ничего не поставив: не оставляем после себя папку с журналом."""
    logging.shutdown()
    logs = config.log_dir()
    for path in logs.glob("setup.log*"):
        path.unlink(missing_ok=True)
    for folder in (logs, config.data_dir()):
        try:
            folder.rmdir()  # только если опустела
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
