"""Окно «Обновления»: версия программы (GitHub Releases) и версия модели (Hugging Face или реестр Ollama)."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from typing import Callable

import changelog
import updater
import widgets
from catalog import MODELS
from config import UPDATE_REPO, VERSION, Config, is_frozen
import model_files
from llm import Cancelled, LlmClient, LlmError, OllamaClient
from theme import COLORS, Chrome
from widgets import PillButton, mono, sans

log = logging.getLogger(__name__)


class UpdatesWindow(Chrome):
    def __init__(self, root: tk.Tk, cfg: Config, client: LlmClient,
                 on_app_ready: Callable[[], None], on_model_updated: Callable[[], None]):
        self.window = tk.Toplevel(root)
        super().__init__(self.window, "Обновления Gloss", min_size=(620, 400))
        self.cfg, self.client = cfg, client
        # Для Ollama у окна свой клиент: у общего одно активное соединение, и загрузка модели
        # мешала бы обработке текста (закрытие окна обрывало бы её, а Esc переставал работать).
        self.ollama = OllamaClient(cfg.model, cfg.ollama) if isinstance(client, OllamaClient) else None
        self.on_app_ready = on_app_ready          # новый exe готов – приложение должно перезапуститься
        self.on_model_updated = on_model_updated
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.cancel = threading.Event()
        self.busy = False
        self.release: updater.Release | None = None
        self.local_digest = ""
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.set_titles("Обновления", ("Gloss сам проверяет новые версии раз в сутки." if cfg.updates.auto_check else
                                       "Проверка выполняется только по вашей команде.") +
                        " Ваши тексты при этом никуда не отправляются.")
        self._build()
        self.center(640, 420)
        self.window.lift()
        self.window.focus_force()
        self.window.after(50, self._poll)
        self.check()

    def _build(self) -> None:
        self.rows = {}
        for key, title in (("app", f"Программа Gloss {VERSION}"), ("model", f"Модель {MODELS.get(self.cfg.model.name, {}).get('title', self.cfg.model.name)}")):
            card = widgets.Panel(self.body, padding=(16, 12))
            card.pack(fill="x", pady=(0, self.px(10)))
            inner = card.inner
            widgets.text(inner, title, "text", mono(10, bold=True)).grid(row=0, column=0, columnspan=2, sticky="w")
            icon = tk.Label(inner, bg=COLORS["panel"], bd=0)
            icon.grid(row=1, column=0, sticky="nw", padx=(0, self.px(8)), pady=(self.px(5), 0))
            status = widgets.text(inner, "Проверяю…", "muted", sans(9), wrap=420)
            status.grid(row=1, column=1, sticky="ew", pady=(self.px(4), 0))
            button = PillButton(inner, "Обновить", lambda k=key: self._update(k), kind="primary", min_width=120, height=32)
            button.grid(row=0, column=2, rowspan=2, sticky="e", padx=(self.px(10), 0))
            button.grid_remove()
            inner.columnconfigure(1, weight=1)
            self.rows[key] = (icon, status, button)
            if key == "app":
                self.notes = tk.Frame(inner, bg=COLORS["panel"])
                self.notes.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(self.px(10), 0))
                self.notes.grid_remove()
        self.bar = widgets.ProgressBar(self.body)
        self.detail = widgets.text(self.body, "", "muted", mono(9))
        self.buttons(("Проверить снова", self.check), ("Закрыть", self.close))

    def _set(self, key: str, text: str, status: str = "muted", button: str | None = None) -> None:
        icon, label, btn = self.rows[key]
        if status in ("ok", "warn", "fail"):
            icon.photo = widgets.photo(widgets.step_icon({"ok": "done", "warn": "warn", "fail": "failed"}[status],
                                                         self.px(14), COLORS["panel"]), icon)
            icon.configure(image=icon.photo)
            icon.grid()
        else:
            icon.grid_remove()
        label.configure(text=text, fg=COLORS[status])
        if button:
            btn.configure(text=button, state="normal")
            btn.grid()
        else:
            btn.grid_remove()

    # --- Проверка ---

    def check(self) -> None:
        if self.busy:
            return
        self._set("app", "Проверяю…")
        self._set("model", "Проверяю…")
        threading.Thread(target=self._check_app, daemon=True).start()
        threading.Thread(target=self._check_model, daemon=True).start()

    def _check_app(self) -> None:
        if not UPDATE_REPO:
            return self.events.put(("app", "Адрес для обновлений не задан в этой сборке.", "warn", None))
        try:
            release = updater.latest_release(UPDATE_REPO)
        except updater.UpdateError as exc:
            return self.events.put(("app", str(exc), "fail", None))
        self.release = release
        if not updater.is_newer(release.version):
            self.events.put(("notes", []))
            return self.events.put(("app", "Установлена последняя версия.", "ok", None))
        try:
            notes = updater.notes_since(UPDATE_REPO)
        except updater.UpdateError:
            notes = [(release.version, changelog.points(release.notes))]
        self.events.put(("notes", notes))
        size = f" ({updater.format_size(release.asset_size)})" if release.asset_size else ""
        skipped = f" Вы пропустили версий: {len(notes)}, ниже изменения во всех." if len(notes) > 1 else ""
        if is_frozen():
            self.events.put(("app", f"Доступна версия {release.version}{size}.{skipped}", "warn", "Обновить"))
        else:
            self.events.put(("app", f"Доступна версия {release.version}. Вы запускаете Gloss из исходников, "
                                    "обновитесь через git pull.", "warn", "Что нового"))

    def _check_model(self) -> None:
        model = self.cfg.model.name
        if self.ollama is not None:
            return self._check_ollama_model(model)
        if model_files.is_custom(model):
            return self.events.put(("model", "Своя модель: обновления не проверяются.", "muted", None))
        if not model_files.is_downloaded(model):
            return self.events.put(("model", "Модель ещё не скачана.", "warn", "Скачать"))
        try:
            newer = model_files.update_available(model)
        except updater.UpdateError as exc:
            return self.events.put(("model", str(exc), "fail", None))
        if newer:
            self.events.put(("model", "Вышла новая версия модели.", "warn", "Обновить"))
        else:
            self.events.put(("model", "Установлена последняя версия.", "ok", None))

    def _check_ollama_model(self, model: str) -> None:
        try:
            local = self.ollama.local_models()
        except LlmError:
            return self.events.put(("model", "Ollama не запущена, проверить модель нельзя.", "fail", None))
        digest = local.get(model) or local.get(f"{model}:latest")
        if digest is None:
            return self.events.put(("model", "Модель ещё не скачана в Ollama.", "warn", "Скачать"))
        try:
            newer = updater.model_update_available(model, digest)
        except updater.UpdateError as exc:
            return self.events.put(("model", str(exc), "fail", None))
        if newer:
            self.events.put(("model", "Вышла новая версия модели. Скачаются только изменившиеся части.", "warn", "Обновить"))
        else:
            self.events.put(("model", "Установлена последняя версия.", "ok", None))

    def _show_notes(self, notes: list[tuple[str, list[str]]]) -> None:
        """«Что нового» во всех версиях новее установленной: новые сверху, длинный список прокручивается."""
        for child in self.notes.winfo_children():
            child.destroy()
        if not notes:
            self.notes.grid_remove()
            return
        tk.Frame(self.notes, bg=COLORS["line"], height=max(1, self.px(1))).pack(fill="x", pady=(0, self.px(10)))
        widgets.text(self.notes, "Что нового", "text", mono(9, bold=True)).pack(fill="x", pady=(0, self.px(4)))
        lines = sum(1 + sum(1 + len(point) // 90 for point in points or ["-"]) for _, points in notes)
        box = tk.Text(self.notes, height=min(lines + len(notes) - 1, 12), wrap="word", relief="flat", bd=0,
                      highlightthickness=0, bg=COLORS["panel"], fg=COLORS["text"], font=sans(9), cursor="arrow",
                      padx=0, pady=0, takefocus=0)
        box.tag_configure("version", font=mono(9, bold=True), foreground=COLORS["muted"], spacing1=self.px(6))
        box.tag_configure("point", lmargin1=self.px(2), lmargin2=self.px(16), spacing1=self.px(2))
        for index, (version, points) in enumerate(notes):
            box.insert("end", f"Версия {version}\n", "version" if index else ("version", "first"))
            for point in points or ["Описание изменений не опубликовано."]:
                box.insert("end", f"•  {point}\n", "point")
        box.tag_configure("first", spacing1=0)
        box.delete("end-2c", "end-1c")  # без пустой строки в конце
        box.configure(state="disabled")
        box.pack(fill="x")
        self.notes.grid()
        self.window.after_idle(self.fit_window)

    # --- Обновление ---

    def _update(self, key: str) -> None:
        if key == "app" and not is_frozen():
            webbrowser.open(self.release.page_url)
            return
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        for _, _, button in self.rows.values():
            button.configure(state="disabled")
        self.primary.configure(state="disabled")
        self.bar.pack(fill="x", pady=(self.px(4), self.px(6)))
        self.detail.pack(fill="x")
        self.bar.set(0)
        self._set(key, "Загружаю…")
        target = self._download_app if key == "app" else self._pull_model
        threading.Thread(target=target, daemon=True).start()

    def _progress(self, fraction: float | None, detail: str) -> None:
        self.events.put(("progress", fraction, detail))

    def _download_app(self) -> None:
        try:
            new_exe = updater.download_release(self.release, self._progress, self.cancel)
        except updater.Aborted:
            return self.events.put(("finished", "app", "Обновление отменено.", "muted", "Обновить"))
        except updater.UpdateError as exc:
            return self.events.put(("finished", "app", str(exc), "fail", "Обновить"))
        self.events.put(("finished", "app", f"Версия {self.release.version} скачана. Перезапускаю Gloss…", "ok", None))
        self.events.put(("restart", new_exe))

    def _pull_model(self) -> None:
        model = self.cfg.model.name
        try:
            if self.ollama is not None:
                progress = updater.PullProgress()
                self.ollama.pull(model, lambda e: self._progress(*progress.feed(e)), self.cancel)
            else:
                self.client.close()  # движок держит файл модели открытым: его не заменить на ходу
                model_files.download(model, self._progress, self.cancel)
        except (Cancelled, updater.Aborted):
            return self.events.put(("finished", "model", "Загрузка отменена. При следующей попытке она продолжится.",
                                    "muted", "Обновить"))
        except (LlmError, updater.UpdateError) as exc:
            return self.events.put(("finished", "model", str(exc), "fail", "Обновить"))
        self.events.put(("finished", "model", "Модель готова к работе.", "ok", None))
        self.on_model_updated()

    # --- События и закрытие ---

    def _poll(self) -> None:
        if not self.window.winfo_exists():
            return
        while True:
            try:
                event, *args = self.events.get_nowait()
            except queue.Empty:
                break
            if event in ("app", "model"):
                self._set(event, *args)
            elif event == "notes":
                self._show_notes(args[0])
            elif event == "progress":
                fraction, detail = args
                self.bar.set(fraction)
                self.detail.configure(text=detail)
            elif event == "finished":
                key, text, status, button = args
                self.busy = False
                self.bar.set(0)
                self.bar.pack_forget()
                self.detail.pack_forget()
                self.primary.configure(state="normal")
                self._set(key, text, status, button)
            elif event == "restart":
                updater.start_apply(args[0])
                self.on_app_ready()
                return
        self.window.after(50, self._poll)

    def close(self) -> None:
        if self.busy:
            self.cancel.set()
            if self.ollama is not None:
                self.ollama.abort()
        self.window.destroy()
