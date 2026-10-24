"""Gloss – улучшение выделенного текста в любом приложении с помощью локальной LLM.

Потоки:
  main     – tkinter (индикатор «Обработка…»);
  tray     – значок и меню pystray;
  hotkeys  – RegisterHotKey и цикл сообщений Win32;
  job      – одна обработка текста за раз;
  engine-*  – встроенный движок нейросети (llama-server) и проверка простоя;
  остальные – короткоживущие фоновые проверки и загрузки.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import replace
from logging.handlers import RotatingFileHandler

import pystray

import changelog
import config
import speech
import system
import winapi
from capture import Clipboard, ClipboardError, TextCapture, release_keys
from config import APP_NAME, VERSION, Config, ConfigError
from dictation import Action, Gesture, dictate, paste_target_ok
from hotkeys import HotkeyManager
from hotkeys_window import HotkeysWindow
import engine
import sysinfo
from llm import BackendUnavailable, Cancelled, Health, LlmClient, LlmError, ModelMissing, make_client
from mic import MicError, Recorder, level_bar
from updater import UpdateError
import uninstaller
import updater
from onboarding import OnboardingWindow
from theme import apply_theme
from ui import TrayIcon, Ui
from updates_window import UpdatesWindow

log = logging.getLogger("gloss")


def setup_logging() -> None:
    log_dir = config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir / "gloss.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stderr is not None:  # под pythonw.exe и в exe консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("GLOSS_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s",
        handlers=handlers,
    )

    def log_uncaught(exc_type, exc, tb):
        log.critical("Необработанное исключение", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


UPDATE_FIRST_CHECK_SEC = 60          # первая проверка новых версий после запуска
UPDATE_INTERVAL_SEC = 24 * 3600      # дальше не чаще раза в сутки


def _bullets(points: list[str], limit: int = 2) -> str:
    """Главные пункты «Что нового» для уведомления (в нём помещается около 250 знаков)."""
    def short(point: str) -> str:
        return point if len(point) <= 80 else point[:79].rstrip(" ,.;:") + "…"  # полный текст в окне «Обновления»
    return "\n".join(f"• {short(p)}" for p in points[:limit])


class App:
    def __init__(self, after_update: bool = False) -> None:
        self.ui = Ui()
        self.after_update = after_update
        self._windows: dict[str, object] = {}
        self.hotkeys = HotkeyManager(self._on_hotkey, self._on_escape)
        self.health: Health | None = None
        self.busy = False
        self._job_lock = threading.Lock()
        self._cancel = threading.Event()
        self._job_llm: LlmClient | None = None
        self._dictating = False
        self._dictation_presses: queue.SimpleQueue[float] = queue.SimpleQueue()  # повторные нажатия во время записи
        self._speech_installing = False
        self._config_error: str | None = None
        self.available_version = self._remembered_update()  # найденная, но ещё не установленная версия
        self._update_wake = threading.Event()

        path = config.config_path()
        try:
            self.cfg = config.load(path)
        except ConfigError as exc:
            log.error("Ошибка в config.json, использую настройки по умолчанию: %s", exc)
            self._config_error = str(exc)
            self.cfg = config.defaults(path)
        self.llm = make_client(self.cfg)
        self.tray = TrayIcon(f"{APP_NAME}: запуск…", self._build_menu())
        self.capture: TextCapture | None = None

    # --- Жизненный цикл ---

    def run(self) -> None:
        log.info("%s %s запускается, config: %s", APP_NAME, VERSION, self.cfg.path)
        self.hotkeys.start()
        self.capture = TextCapture(Clipboard(self.hotkeys.hwnd))
        self.tray.start()
        errors = self._register_hotkeys()
        if self._config_error:
            self.tray.notify("Ошибка в config.json", f"Использую настройки по умолчанию.\n{self._config_error}")
        if errors:
            self.tray.notify("Не все горячие клавиши доступны", "\n".join(errors.values()))
        try:
            system.sync_autostart()
        except OSError:
            log.exception("Не удалось обновить автозапуск")
        self._update_tray()
        self.ui.post(lambda: apply_theme(self.ui.root))
        if self.after_update:
            self._finish_update()
        elif not config.load_state().get("onboarding_done"):
            self.show_onboarding()
        threading.Thread(target=self._startup_check, name="startup-check", daemon=True).start()
        threading.Thread(target=self._update_loop, name="update-check", daemon=True).start()
        self.ui.run()  # блокируется до выхода
        self._shutdown()

    def quit(self) -> None:
        log.info("Выход по команде пользователя")
        self._cancel.set()
        if self._job_llm:
            self._job_llm.abort()
        self.ui.quit()

    def _shutdown(self) -> None:
        self.llm.close()  # остановить встроенный движок
        self.hotkeys.stop()
        self.tray.stop()
        log.info("%s остановлен", APP_NAME)

    # --- Модель ---

    def _startup_check(self) -> None:
        if self.cfg.backend == "builtin" and not engine.is_installed():
            self._install_engine()  # после обновления Gloss может требоваться новая сборка движка
        if not speech.is_installed():  # у тех, кто обновился с версии без диктовки
            threading.Thread(target=self._install_speech, name="speech-install", daemon=True).start()
        health = self.check_model(notify=False)
        if not health.ok:
            self.tray.notify(health.status, health.message)
            return
        if not self.after_update:
            hotkeys = ", ".join(f"{m.hotkey}: {m.name}" for m in self.cfg.all_modes if m.enabled and m.hotkey)
            self.tray.notify(f"{APP_NAME} готов", hotkeys or "Нет активных сочетаний клавиш. Настройте их в config.json.")
        if self.cfg.model.preload_on_start:
            self.llm.preload()

    def _install_engine(self) -> None:
        log.info("Движок нейросети отсутствует или устарел, устанавливаю %s", engine.ENGINE_BUILD)
        self.tray.notify("Обновляю движок нейросети", "Это займёт около минуты.")
        try:
            engine.install(sysinfo.arch(), lambda fraction, detail: None, threading.Event())
        except UpdateError as exc:
            log.warning("Движок не установлен: %s", exc)
            self.tray.notify("Не удалось установить движок нейросети", str(exc))
        except Exception:  # проверка модели и предзагрузка после этого всё равно должны пройти
            log.exception("Движок не установлен")
            self.tray.notify("Не удалось установить движок нейросети", "Подробности в журнале Gloss.")

    def _install_speech(self) -> None:
        if self._speech_installing:
            return
        self._speech_installing = True
        log.info("Распознавание речи отсутствует или устарело, устанавливаю %s", speech.ASR_BUILD)
        self.tray.notify("Загружаю распознавание речи",
                         f"Около {speech.download_mb(sysinfo.arch()):.0f} МБ. Диктовка заработает, когда загрузка закончится.")
        try:
            speech.install(sysinfo.arch(), lambda fraction, detail: None, threading.Event())
        except UpdateError as exc:
            log.warning("Распознавание речи не установлено: %s", exc)
            self.tray.notify("Не удалось загрузить распознавание речи", str(exc))
        except Exception:
            log.exception("Распознавание речи не установлено")
            self.tray.notify("Не удалось загрузить распознавание речи", "Подробности в журнале Gloss.")
        else:
            mode = self.cfg.dictation.mode
            self.tray.notify("Диктовка готова", f"Поставьте курсор в поле ввода, нажмите {mode.hotkey} и говорите."
                             if mode.enabled and mode.hotkey else "Включить её можно в меню значка → Настройки.")
        finally:
            self._speech_installing = False

    # --- Обновления и окна ---

    def _finish_update(self) -> None:
        log.info("Gloss обновлён до %s", VERSION)
        threading.Thread(target=updater.cleanup_update_dir, daemon=True).start()
        try:
            system.refresh_registered_version()
        except OSError:
            log.exception("Не удалось обновить версию в списке приложений")
        points = changelog.version_points(VERSION)  # CHANGELOG.md лежит внутри exe: интернет не нужен
        self.tray.notify(f"{APP_NAME} обновлён до версии {VERSION}",
                         _bullets(points) if points else "Настройки и модели сохранены.")

    @staticmethod
    def _remembered_update() -> str | None:
        version = config.load_state().get("update_available")
        return version if isinstance(version, str) and updater.is_newer(version) else None

    def _update_loop(self) -> None:
        """Сам ищет новые версии: через минуту после запуска, затем не чаще раза в сутки.

        Время последней проверки хранится в state.json, поэтому перезапуски Gloss не учащают
        проверки, а сон компьютера их не сбивает.
        """
        self._update_wake.wait(UPDATE_FIRST_CHECK_SEC)
        while True:
            last = config.load_state().get("update_checked_at", 0)
            due = not isinstance(last, (int, float)) or time.time() - last >= UPDATE_INTERVAL_SEC
            if self.cfg.updates.auto_check and config.UPDATE_REPO and due:
                self._check_for_update()
            self._update_wake.wait(3600)
            self._update_wake.clear()

    def _check_for_update(self) -> None:
        try:
            notes = updater.notes_since(config.UPDATE_REPO)
        except UpdateError as exc:
            log.info("Проверка обновлений не удалась: %s", exc)
            return
        config.save_state(update_checked_at=int(time.time()), update_available=notes[0][0] if notes else None)
        self.available_version = notes[0][0] if notes else None
        self.tray.refresh_menu()
        if not notes:
            log.info("Обновлений нет, установлена последняя версия %s", VERSION)
            return
        version, points = notes[0]
        log.info("Доступна новая версия %s (пропущено версий: %d)", version, len(notes))
        if config.load_state().get("update_notified") == version:
            return  # о каждой версии сообщаем один раз, пункт в меню остаётся
        config.save_state(update_notified=version)
        lines = [_bullets(points) or "Исправления и улучшения."]
        if len(notes) > 1:
            lines.append(f"А ещё изменения из версий {', '.join(v for v, _ in notes[1:])}.")
        lines.append("Обновить: меню значка → «Доступно обновление».")
        self.tray.notify(f"Доступна новая версия {APP_NAME} {version}", "\n".join(lines))

    def toggle_auto_check(self) -> None:
        enabled = not self.cfg.updates.auto_check
        try:
            config.set_auto_check(self.cfg.path, enabled)
        except (ConfigError, OSError) as exc:
            self.tray.notify("Не удалось изменить config.json", str(exc))
            return
        self.cfg = replace(self.cfg, updates=config.UpdateSettings(auto_check=enabled))
        log.info("Сообщения о новых версиях %s", "включены" if enabled else "выключены")
        if enabled:
            self._update_wake.set()  # проверить сейчас, если давно не проверяли
        self.tray.refresh_menu()

    def _updates_label(self) -> str:
        if self.available_version:
            return f"Доступно обновление {self.available_version}…"
        return "Проверить обновления…"

    def show_updates(self) -> None:
        def open_window():
            if not self._raise_window("updates"):
                self._windows["updates"] = UpdatesWindow(
                    self.ui.root, self.cfg, self.llm,
                    on_app_ready=self.quit, on_model_updated=lambda: self.check_model(notify=False))
        self.ui.post(open_window)

    def show_hotkeys(self) -> None:
        def open_window():
            if not self._raise_window("hotkeys"):
                self._windows["hotkeys"] = HotkeysWindow(self.ui.root, self)
        self.ui.post(open_window)

    def show_onboarding(self) -> None:
        def problem() -> str | None:
            return None if self.health is None or self.health.ok else self.health.message

        def open_window():
            if not self._raise_window("onboarding"):
                self._windows["onboarding"] = OnboardingWindow(
                    self.ui.root, self.cfg, problem, on_close=lambda: config.save_state(onboarding_done=True))
        self.ui.post(open_window)

    def _raise_window(self, name: str) -> bool:
        window = self._windows.get(name)
        if window is not None and window.window.winfo_exists():
            window.window.deiconify()
            window.window.lift()
            window.window.focus_force()
            return True
        return False

    def check_model(self, notify: bool = True) -> Health:
        health = self.llm.check()
        self.health = health
        log.info("Проверка модели (%s): %s", self.cfg.backend, health.status)
        self._update_tray()
        if notify:
            self.tray.notify("Модель: всё в порядке" if health.ok else health.status, health.message)
        return health

    def _status_text(self) -> str:
        if self.busy:
            return "Обработка…"
        return "Проверяю модель…" if self.health is None else self.health.status

    def _update_tray(self) -> None:
        if self.busy:
            state = "busy"
        elif self.health is not None and not self.health.ok:
            state = "warning"
        else:
            state = "idle"
        self.tray.set_state(state, f"{APP_NAME}: {self._status_text()}")
        self.tray.refresh_menu()

    # --- Настройки и хоткеи ---

    def _register_hotkeys(self) -> dict[str, str]:
        bindings = {m.id: m.hotkey for m in self.cfg.all_modes if m.enabled and m.hotkey}
        errors = self.hotkeys.register(bindings)
        return {mode_id: f"{self.cfg.mode(mode_id).name}: {text}" for mode_id, text in errors.items()}

    def reload_config(self, notify: bool = True) -> list[str]:
        """Перечитывает config.json и применяет его. Возвращает список проблем (пустой – всё хорошо).

        notify=False – без уведомлений: так перечитывает окно «Сочетания клавиш», которое
        показывает проблемы само.
        """
        try:
            new_cfg = config.load(self.cfg.path)
        except ConfigError as exc:
            log.error("Ошибка в config.json: %s", exc)
            if notify:
                self.tray.notify("Ошибка в config.json, оставлены прежние настройки", str(exc))
            return [str(exc)]
        model_changed = (new_cfg.backend, new_cfg.model, new_cfg.ollama) != (self.cfg.backend, self.cfg.model,
                                                                             self.cfg.ollama)
        self.cfg = new_cfg
        if model_changed:
            old, self.llm = self.llm, make_client(new_cfg)
            if old is not self._job_llm:  # иначе его закроет задача, когда закончит
                old.close()
        errors = self._register_hotkeys()
        log.info("Настройки перечитаны")
        if notify and errors:
            self.tray.notify("Настройки применены, но не все сочетания клавиш доступны", "\n".join(errors.values()))
        elif notify:
            active = sum(1 for m in new_cfg.all_modes if m.enabled and m.hotkey)
            self.tray.notify("Настройки применены", f"Активных режимов: {active}. Модель: {new_cfg.model.name}.")
        self._update_tray()
        if model_changed:
            self.check_model(notify=False)
        return list(errors.values())

    def pause_hotkeys(self, paused: bool) -> None:
        """Окно «Сочетания клавиш» снимает сочетания Gloss, пока вы вводите новое: иначе их перехватил бы Gloss."""
        if paused:
            self.hotkeys.register({})
        else:
            self._register_hotkeys()

    def toggle_mode(self, mode_id: str) -> None:
        mode = self.cfg.mode(mode_id)
        if mode is None:
            return
        try:
            config.set_mode_enabled(self.cfg.path, mode_id, not mode.enabled)
        except (ConfigError, OSError) as exc:
            self.tray.notify("Не удалось изменить config.json", str(exc))
            return
        self.reload_config()

    def toggle_autostart(self) -> None:
        try:
            system.set_autostart(not system.is_autostart_enabled())
        except OSError as exc:
            log.exception("Не удалось изменить автозапуск")
            self.tray.notify("Не удалось изменить автозапуск", str(exc))
        self.tray.refresh_menu()

    def open_config(self) -> None:
        try:
            os.startfile(self.cfg.path)
        except OSError:
            subprocess.Popen(["notepad.exe", str(self.cfg.path)])

    def open_logs(self) -> None:
        os.startfile(config.log_dir())

    # --- Меню трея ---

    def _build_menu(self) -> pystray.Menu:
        def background(action):
            # Действия меню выполняются в потоке трея – долгие уводим в фон.
            return lambda: threading.Thread(target=action, daemon=True).start()

        def toggle_items():
            for mode in self.cfg.all_modes:
                yield pystray.MenuItem(
                    mode.name,
                    background(lambda mode_id=mode.id: self.toggle_mode(mode_id)),
                    checked=lambda item, enabled=mode.enabled: enabled,
                )

        settings = pystray.Menu(
            pystray.MenuItem("Сочетания клавиш…", self.show_hotkeys),
            pystray.MenuItem("Режимы (включить или выключить)", pystray.Menu(toggle_items)),
            pystray.MenuItem("Запускать вместе с Windows", self.toggle_autostart,
                             checked=lambda item: system.is_autostart_enabled()),
            pystray.MenuItem("Сообщать о новых версиях", background(self.toggle_auto_check),
                             checked=lambda item: self.cfg.updates.auto_check),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть файл настроек", self.open_config),
            pystray.MenuItem("Перечитать настройки", background(self.reload_config)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить модель", background(self.check_model)),
            pystray.MenuItem("Открыть папку журналов", self.open_logs),
        )
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"{APP_NAME} {VERSION} · {self._status_text()}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Отменить обработку", self.cancel, visible=lambda item: self.busy),
            *[pystray.MenuItem(lambda item, i=i: self._hotkey_label(i), self._hotkey_action(i),
                               visible=lambda item, i=i: i < len(self._hotkey_modes()))
              for i in range(9)],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Как пользоваться", self.show_onboarding),
            pystray.MenuItem(lambda item: self._updates_label(), self.show_updates),
            pystray.MenuItem("Настройки", settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    # Список режимов в меню трея. У pystray вложенное динамическое меню нельзя
    # «развернуть» на верхний уровень, поэтому держим 9 слотов и скрываем лишние.
    def _hotkey_modes(self) -> list:
        return [m for m in self.cfg.all_modes if m.hotkey]

    def _hotkey_label(self, index: int) -> str:
        modes = self._hotkey_modes()
        if index >= len(modes):
            return ""
        mode = modes[index]
        return f"{mode.name}\t{mode.hotkey if mode.enabled else 'выключен'}"

    def _hotkey_action(self, index: int):
        def show():
            modes = self._hotkey_modes()
            if index >= len(modes):
                return
            mode = modes[index]
            if mode.enabled and mode.id == "dictation":
                self.tray.notify(mode.name, f"Поставьте курсор в поле ввода, нажмите {mode.hotkey} и говорите. "
                                            "Чтобы закончить, нажмите ещё раз или отпустите, если держали.")
            elif mode.enabled:
                self.tray.notify(mode.name, f"Выделите текст в любой программе и нажмите {mode.hotkey}.")
            else:
                self.tray.notify(mode.name, "Режим выключен. Включить: меню значка → Настройки → Режимы.")
        return show

    # --- Обработка текста ---

    def _on_hotkey(self, mode_id: str) -> None:
        """Вызывается в потоке хоткеев – только запускаем задачу."""
        if mode_id == "dictation" and self._dictating:
            self._dictation_presses.put(time.monotonic())  # повторное нажатие заканчивает запись
            return
        if not self._job_lock.acquire(blocking=False):
            log.info("Хоткей проигнорирован: обработка уже идёт")
            return
        target = winapi.GetForegroundWindow()
        if mode_id == "dictation":
            pressed_at = time.monotonic()  # до открытия микрофона: оно бывает долгим (Bluetooth-гарнитура)
            while not self._dictation_presses.empty():  # нажатия, оставшиеся от прошлой диктовки
                self._dictation_presses.get_nowait()
            self._dictating = True
            threading.Thread(target=self._run_dictation, args=(target, pressed_at), name="job", daemon=True).start()
        else:
            threading.Thread(target=self._run_job, args=(mode_id, target), name="job", daemon=True).start()

    def _on_escape(self) -> None:
        if self.busy:
            self.cancel()

    def cancel(self) -> None:
        if not self.busy:
            return
        log.info("Отмена обработки")
        self._cancel.set()
        if self._job_llm:
            self._job_llm.abort()
        self.ui.update_progress("Отменяю…")

    def _set_busy(self, busy: bool, mode_name: str = "") -> None:
        if busy == self.busy:
            return
        self.busy = busy
        if busy:
            escape = self.hotkeys.set_escape(True)
            if self.cfg.ui.show_overlay:
                hint = "Esc: отменить" if escape else "Отменить можно в меню значка"
                self.ui.show_progress(f"Gloss · {mode_name}", hint)
        else:
            self.hotkeys.set_escape(False)
            self.ui.hide_progress()
        self._update_tray()

    def _run_job(self, mode_id: str, target) -> None:
        cfg, llm, capture = self.cfg, self.llm, self.capture
        self._job_llm = llm
        self._cancel.clear()
        started = time.monotonic()
        captured = None
        try:
            mode = cfg.mode(mode_id)
            if mode is None:
                return
            release_keys(mode.hotkey.vk if mode.hotkey else None, cfg.capture.modifier_release_timeout_ms)
            try:
                captured = capture.copy_selection(cfg.capture)
            except ClipboardError as exc:
                self.tray.notify("Буфер обмена занят", f"{exc}. Попробуйте ещё раз.")
                return
            if captured is None and winapi.is_window_elevated(target) and not winapi.is_current_process_elevated():
                # UIPI молча отбрасывает наш Ctrl+C в окна с повышенными правами.
                log.info("Активное окно запущено от администратора – копирование заблокировано Windows")
                self.tray.notify("Окно запущено от администратора",
                                 "Windows не даёт обычным программам управлять таким окном. "
                                 "Чтобы Gloss работал в нём, запустите Gloss от имени администратора.")
                return
            if captured is None:
                hotkey = f" и нажмите {mode.hotkey}" if mode.hotkey else ""
                self.tray.notify("Нет выделенного текста", f"Выделите текст{hotkey} ещё раз.")
                return

            self._set_busy(True, mode.name)

            def on_progress(index: int, total: int) -> None:
                self.ui.update_progress(f"Часть {index} из {total} · Esc: отменить")

            try:
                result = llm.process(captured.text, mode, cfg.common_rules, cfg.max_chunk_chars,
                                     self._cancel, on_progress, protect_quotes=cfg.protect_quotes)
            finally:
                self._set_busy(False)

            if result.strip() == captured.text.strip():
                capture.restore(captured)
                log.info("Модель вернула текст без изменений")
                if cfg.ui.notify_unchanged:
                    self.tray.notify("Без изменений", "Модель не нашла, что поправить.")
                return

            if winapi.GetForegroundWindow() != target:
                capture.put_result(result)
                log.info("Активное окно сменилось – результат оставлен в буфере")
                self.tray.notify("Окно сменилось", "Результат скопирован в буфер обмена. Вставьте его через Ctrl+V.")
                return

            capture.paste(result, captured, cfg.capture)
            log.info("Готово: режим=%s, %d → %d символов за %.1f с",
                     mode.id, len(captured.text), len(result), time.monotonic() - started)
            if self.health is not None and not self.health.ok:
                self.check_model(notify=False)

        except Cancelled:
            log.info("Обработка отменена пользователем")
            capture.restore(captured)
            self.tray.notify("Отменено", "Текст не изменён.")
        except LlmError as exc:
            log.warning("Ошибка LLM: %s", exc)
            capture.restore(captured)
            if isinstance(exc, ModelMissing):
                self.health = Health(False, "Модель не скачана", str(exc), can_download=True)
            elif isinstance(exc, BackendUnavailable):
                self.health = Health(False, "Движок недоступен", str(exc))
            self._update_tray()
            self.tray.notify("Gloss: ошибка", str(exc))
        except Exception:
            log.exception("Непредвиденная ошибка при обработке")
            capture.restore(captured)
            self.tray.notify("Gloss: ошибка", "Непредвиденная ошибка, текст не изменён. Подробности в журнале.")
        finally:
            self._set_busy(False)
            if llm is not self.llm:  # настройки сменились во время обработки
                llm.close()
            self._job_llm = None
            self._job_lock.release()

    # --- Диктовка ---

    def _say(self, title: str, detail: str) -> None:
        """Короткое сообщение: на плашке у курсора, а если плашка выключена – уведомлением."""
        if self.cfg.ui.show_overlay:
            self.ui.flash(title, detail)
        else:
            self.tray.notify(title, detail)

    def _record(self, recorder: Recorder, mode, max_sec: int, pressed_at: float) -> None:
        """Ждёт конца записи: повторного нажатия, отпускания клавиши, Esc или предела длины.

        pressed_at – время первого нажатия: пока открывался микрофон, клавишу могли уже отпустить
        (это удержание) или нажать ещё раз (это конец записи), и эти нажатия уже лежат в очереди."""
        gesture = Gesture(pressed_at)
        shown = 0.0
        while not self._cancel.is_set():
            now = time.monotonic()
            action = Action.NONE
            while not self._dictation_presses.empty():
                if gesture.press(self._dictation_presses.get_nowait()) is Action.FINISH:
                    action = Action.FINISH
            if action is Action.NONE:
                action = gesture.poll(now, bool(winapi.GetAsyncKeyState(mode.hotkey.vk) & 0x8000))
            if action is Action.FINISH:
                return
            if recorder.elapsed() >= max_sec:
                log.info("Запись остановлена по пределу %d с", max_sec)
                self.tray.notify("Запись остановлена", f"Прошло {max_sec // 60} мин. Обрабатываю то, что успел записать.")
                return
            if now - shown >= 0.1:
                seconds = int(recorder.elapsed())
                self.ui.update_progress(f"{seconds // 60}:{seconds % 60:02d} {level_bar(recorder.level())} · Esc: отменить")
                shown = now
            time.sleep(0.02)

    def _run_dictation(self, target, pressed_at: float) -> None:
        cfg, llm, capture = self.cfg, self.llm, self.capture
        mode = cfg.dictation.mode
        title = f"Gloss · {mode.name}"
        self._job_llm = llm
        self._cancel.clear()
        recorder: Recorder | None = None
        try:
            if self._speech_installing:
                self._say(title, "Распознавание речи ещё загружается")
                return
            if not speech.is_installed():
                threading.Thread(target=self._install_speech, name="speech-install", daemon=True).start()
                return
            recorder = Recorder()
            try:
                recorder.start()
            except MicError as exc:
                self.tray.notify(str(exc), "Проверьте, что микрофон подключён и выбран в параметрах звука Windows.")
                return
            self._set_busy(True, f"{mode.name}: слушаю")
            self._record(recorder, mode, cfg.dictation.max_record_sec, pressed_at)
            pcm = recorder.stop()
            if self._cancel.is_set():
                raise Cancelled
            seconds = len(pcm) / 32000
            self.ui.update_progress("Esc: отменить", title=f"{title}: распознаю")
            started = time.monotonic()
            timings: dict[str, float] = {}

            def transcribe(audio: bytes) -> str:
                text = speech.transcribe(audio, self._cancel)
                timings["asr"] = time.monotonic() - started
                return text

            def improve(text: str) -> str:
                self.ui.update_progress("Esc: отменить", title=f"{title}: улучшаю")
                return llm.process(text, mode, cfg.common_rules, cfg.max_chunk_chars, self._cancel,
                                   protect_quotes=False)

            outcome = dictate(pcm, transcribe, improve)
            self._set_busy(False)
            log.info("Диктовка: запись %.1f с, распознавание %.1f с, всего %.1f с, итог=%s",
                     seconds, timings.get("asr", 0), time.monotonic() - started, outcome.reason or "ok")
            if outcome.reason == "silence":
                self._say(title, "Не расслышал")
                return
            if outcome.reason == "mic_blocked":
                self.tray.notify("Windows не даёт доступ к микрофону",
                                 "Разрешите его: Параметры → Конфиденциальность и защита → Микрофон.")
                return

            end = winapi.GetForegroundWindow()
            elevated = winapi.is_window_elevated(end) and not winapi.is_current_process_elevated()
            if not paste_target_ok(target, end) or elevated:
                capture.put_result(outcome.text)
                log.info("Окно сменилось или запущено от администратора – текст оставлен в буфере")
                self.tray.notify("Текст в буфере обмена", "Окно сменилось во время записи. Вставьте текст через Ctrl+V.")
                return
            capture.paste_text(outcome.text, mode.hotkey.vk if mode.hotkey else None, cfg.capture)
            if outcome.reason == "llm_failed":
                self.tray.notify("Вставил текст без улучшения", "Модель сейчас недоступна, текст вставлен как распознан.")
            elif outcome.reason == "llm_rejected":
                log.warning("Модель ответила намного длиннее сказанного – вставлен распознанный текст")
                self.tray.notify("Вставил текст без улучшения", "Модель ответила не по делу, текст вставлен как распознан.")

        except Cancelled:
            log.info("Диктовка отменена пользователем")
            self.tray.notify("Отменено", "Ничего не вставлено.")
        except (speech.SpeechMissing, speech.SpeechError) as exc:
            log.warning("Распознавание не удалось: %s", exc)
            self.tray.notify("Не удалось распознать речь", "Подробности в журнале Gloss.")
        except Exception:
            log.exception("Непредвиденная ошибка при диктовке")
            self.tray.notify("Gloss: ошибка", "Непредвиденная ошибка, ничего не вставлено. Подробности в журнале.")
        finally:
            if recorder is not None:
                recorder.stop()  # микрофон освобождается при любой ошибке
            self._set_busy(False)
            if llm is not self.llm:
                llm.close()
            self._job_llm = None
            self._dictating = False
            self._job_lock.release()


def main() -> int:
    winapi.set_dpi_awareness()
    setup_logging()
    args = sys.argv[1:]
    if "--uninstall" in args:  # запускает Windows: «Параметры → Приложения → Gloss → Удалить»
        uninstaller.run(quiet="--quiet" in args)
        return 0
    if "--apply-update" in args:  # новая версия из временной папки подменяет старую
        index = args.index("--apply-update")
        updater.apply_update(args[index + 1], int(args[index + 2]))
        return 0
    mutex = system.acquire_single_instance()
    if mutex is None:
        winapi.message_box(f"{APP_NAME} уже запущен. Его значок находится в области уведомлений (в трее).", APP_NAME)
        return 0
    try:
        App(after_update="--after-update" in args).run()
    except Exception:
        log.exception("Критическая ошибка")
        winapi.message_box(f"{APP_NAME} аварийно завершился. Подробности в логе:\n{config.log_dir()}",
                           APP_NAME, 0x10)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
