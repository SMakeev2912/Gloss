"""Шаги установки: движок нейросети, модель, Gloss. Без интерфейса, его рисует installer.py.

Каждый шаг сначала проверяет, не сделан ли он уже, поэтому повторный запуск
установщика продолжает с места остановки (модель докачивается).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import config
import engine
import model_files
import sysinfo
import system
from catalog import DEFAULT_MODEL, MODELS
from llm import Cancelled, LlmError, OllamaClient, make_client
from updater import Aborted, PullProgress, UpdateError, format_size  # noqa: F401  (Aborted нужен installer.py)

log = logging.getLogger("setup")


class SetupError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


@dataclass(frozen=True)
class Options:
    model: str = DEFAULT_MODEL
    backend: str = "builtin"   # builtin – встроенный движок, ollama – уже установленная Ollama
    autostart: bool = True
    desktop_shortcut: bool = False


STEPS = [
    ("engine", "Скачать движок нейросети"),
    ("model", "Загрузить модель"),
    ("gloss", "Установить Gloss"),
    ("verify", "Проверить, что модель отвечает"),
    ("launch", "Запустить Gloss"),
]


class Reporter:
    """Интерфейс, через который шаги сообщают о ходе установки (реализует installer.py)."""

    def step(self, step_id: str, state: str, note: str = "") -> None:  # state: active/done/skipped/failed/warn
        pass

    def progress(self, fraction: float | None, detail: str) -> None:  # None – неопределённый прогресс
        pass


def payload_path() -> Path:
    """Gloss.exe, вшитый в установщик (при запуске из исходников – dist\\Gloss.exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "payload" / "Gloss.exe"
    return Path(__file__).resolve().parent / "dist" / "Gloss.exe"


def load_config_or_defaults() -> config.Config:
    """Настройки установленного Gloss. С ошибкой в config.json сам Gloss запускается на настройках
    по умолчанию и показывает её, поэтому и установка из-за неё не прерывается."""
    path = config.installed_config_path()
    try:
        return config.load(path)
    except config.ConfigError as exc:
        log.warning("Ошибка в config.json, беру настройки по умолчанию: %s", exc)
        return config.defaults(path)


class Installer:
    def __init__(self, options: Options, reporter: Reporter, cancel: threading.Event):
        self.options = options
        self.report = reporter
        self.cancel = cancel
        self.warnings: list[str] = []
        self.pulled_models: list[str] = []   # модели, скачанные в Ollama для Gloss (удалятся вместе с ним)
        self._abort_hooks: list[Callable[[], None]] = []

    def abort(self) -> None:
        self.cancel.set()
        for hook in list(self._abort_hooks):
            hook()

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Aborted

    def run(self) -> None:
        builtin = self.options.backend == "builtin"
        log.info("Установка: движок=%s, модель=%s", self.options.backend, self.options.model)
        plan: list[tuple[str, Callable[[], str] | None, str]] = [
            ("engine", self._install_engine if builtin else None, "используется Ollama"),
            ("model", self._download_model if builtin else self._pull_ollama_model, ""),
            ("gloss", self._install_gloss, ""),
            ("verify", self._verify, ""),
            ("launch", self._launch, ""),
        ]
        for step_id, action, skip_note in plan:
            self._check_cancel()
            if action is None:
                self.report.step(step_id, "skipped", skip_note)
                continue
            self.report.step(step_id, "active")
            self.report.progress(None, "")
            try:
                note = action() or ""
            except (Aborted, SetupError):
                self.report.step(step_id, "failed")
                raise
            except Exception as exc:
                log.exception("Шаг %s упал", step_id)
                self.report.step(step_id, "failed")
                raise SetupError(f"Непредвиденная ошибка: {exc}") from exc
            self.report.step(step_id, "warn" if note.startswith("!") else "done", note.lstrip("!"))

    # --- Движок и модель ---

    def _install_engine(self) -> str:
        if engine.is_installed():
            return "уже установлен"
        # Старый Gloss держит файлы прежней сборки движка открытыми: закрываем его до замены.
        self.report.progress(None, "Останавливаю запущенный Gloss…")
        system.stop_running_gloss()
        system.stop_engine()
        try:
            engine.install(sysinfo.arch(), self.report.progress, self.cancel)
        except UpdateError as exc:
            raise SetupError(str(exc)) from None
        return f"сборка {engine.ENGINE_BUILD}"

    def _download_model(self) -> str:
        model = self.options.model
        if model_files.is_downloaded(model):
            return "уже загружена"
        try:
            model_files.download(model, self.report.progress, self.cancel)
        except UpdateError as exc:
            raise SetupError(str(exc)) from None
        return format_size(MODELS[model]["size_gb"] * sysinfo.GB)

    def _pull_ollama_model(self) -> str:
        model = self.options.model
        if sysinfo.ollama_state().has_model(model):
            return "уже загружена в Ollama"
        if system.sandbox():
            raise SetupError("Тестовый режим: качать модель в настоящую Ollama из песочницы нельзя.")
        progress = PullProgress(int(MODELS[model]["size_gb"] * sysinfo.GB))
        client = OllamaClient(config.defaults(config.installed_config_path()).model, config.OllamaSettings(
            url="http://localhost:11434"))
        self._abort_hooks.append(client.abort)
        try:
            client.pull(model, lambda event: self.report.progress(*progress.feed(event)), self.cancel)
        except Cancelled:
            raise Aborted from None
        except LlmError as exc:
            raise SetupError(str(exc)) from None
        finally:
            self._abort_hooks.remove(client.abort)
        self.pulled_models.append(model)
        return format_size(progress.total)

    # --- Gloss ---

    def _install_gloss(self) -> str:
        source = payload_path()
        if not source.exists():
            raise SetupError(f"В установщике нет Gloss.exe ({source}). Пересоберите установщик через build.bat.")
        target = system.installed_exe()
        target.parent.mkdir(parents=True, exist_ok=True)

        self.report.progress(None, "Останавливаю запущенный Gloss…")
        system.stop_running_gloss()
        temp = target.with_suffix(".new")
        shutil.copy2(source, temp)
        for _attempt in range(20):  # файл может быть занят, пока старый процесс завершается
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                time.sleep(0.25)
        else:
            raise SetupError("Не удалось заменить Gloss.exe. Закройте Gloss и попробуйте снова.")

        self.report.progress(None, "Настройки, ярлыки, автозапуск…")
        config.set_model(config.installed_config_path(), self.options.model, self.options.backend)
        links = system.shortcut_paths()
        system.create_shortcut(links["start_menu"], target, "Улучшение выделенного текста локальной нейросетью")
        if self.options.desktop_shortcut and "desktop" in links:
            system.create_shortcut(links["desktop"], target, "Gloss")
        system.set_autostart(self.options.autostart, f'"{target}"')
        system.register_uninstaller(target, self.pulled_models)
        log.info("Gloss установлен в %s", target.parent)
        return str(target.parent)

    def _verify(self) -> str:
        self.report.progress(None, "Загружаю модель в память и отправляю пробный текст…")
        cfg = load_config_or_defaults()
        cfg = replace(cfg, backend=self.options.backend,
                      model=replace(cfg.model, name=self.options.model, timeout_sec=max(cfg.model.timeout_sec, 300)))
        client = make_client(cfg)
        self._abort_hooks.append(client.abort)
        started = time.monotonic()
        try:
            client.process("превет как дила", cfg.mode("fix") or cfg.modes[0], cfg.common_rules,
                           cfg.max_chunk_chars, self.cancel)
        except Cancelled:
            raise Aborted from None
        except LlmError as exc:
            log.warning("Пробный запрос не прошёл: %s", exc)
            self.warnings.append(f"Пробный запрос к модели не прошёл: {exc}")
            return "!модель не ответила, подробности ниже"
        finally:
            self._abort_hooks.remove(client.abort)
            client.close()  # движок запустит уже сам Gloss
        seconds = time.monotonic() - started
        log.info("Пробный запрос: %.1f с", seconds)
        return f"ответ за {seconds:.1f} с"

    def _launch(self) -> str:
        if system.sandbox():
            return "пропущено (тестовый режим)"
        exe = system.installed_exe()
        subprocess.Popen([str(exe)], cwd=exe.parent, creationflags=system.DETACHED)
        return ""

