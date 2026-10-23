"""Автозапуск, защита от второй копии, установка в систему и полное удаление.

GLOSS_SANDBOX=<папка> – режим для тестов: ярлыки создаются в этой папке, ключи
реестра получают суффикс «-Sandbox», а модели в настоящей Ollama не затрагиваются. Пути к данным берутся из LOCALAPPDATA/APPDATA/TEMP/
USERPROFILE, поэтому тест подменяет и их.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path

import winapi as w
from config import APP_NAME, VERSION, data_dir, installed_config_path, is_frozen

log = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MUTEX_NAME = "Local\\Gloss.SingleInstance"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def sandbox() -> Path | None:
    value = os.environ.get("GLOSS_SANDBOX")
    return Path(value) if value else None


def _reg_name() -> str:
    return f"{APP_NAME}-Sandbox" if sandbox() else APP_NAME


def _uninstall_key() -> str:
    return rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{_reg_name()}"


def engine_dir() -> Path:
    return data_dir() / "engine"


def engine_dirs() -> list[Path]:
    """Движок и его временные копии при обновлении (см. engine.install)."""
    return [engine_dir(), data_dir() / "engine.new", data_dir() / "engine.old"]


def models_dir() -> Path:
    return data_dir() / "models"


def install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / APP_NAME


def installed_exe() -> Path:
    return install_dir() / f"{APP_NAME}.exe"


def temp_dirs() -> list[Path]:
    """Временные папки установщика и обновлений."""
    temp = Path(tempfile.gettempdir())
    return [temp / "GlossSetup", temp / f"{APP_NAME}-update", temp / f"{APP_NAME}-engine"]


def acquire_single_instance():
    """Возвращает хэндл мьютекса или None, если Gloss уже запущен."""
    handle = w.CreateMutexW(None, False, _MUTEX_NAME + ("-Sandbox" if sandbox() else ""))
    if not handle or ctypes.get_last_error() == w.ERROR_ALREADY_EXISTS:
        if handle:
            w.CloseHandle(handle)
        return None
    return handle  # держим открытым до выхода из процесса


# --- Автозапуск -------------------------------------------------------------------

def autostart_command() -> str:
    if is_frozen():
        return f'"{sys.executable}"'
    # Из исходников запускаем через pythonw.exe – без консольного окна.
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    main = Path(__file__).resolve().with_name("main.py")
    return f'"{pythonw if pythonw.exists() else exe}" "{main}"'


def _autostart_value() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _reg_name())
            return value or None
    except OSError:
        return None


def is_autostart_enabled() -> bool:
    return _autostart_value() is not None


def sync_autostart() -> None:
    """Исправляет путь в автозапуске, если он указывает не на установленный Gloss.

    Только из установленной копии: запуск из исходников или из dist\\Gloss.exe
    не должен перехватывать автозапуск установленной.
    """
    value = _autostart_value()
    if value is not None and value != autostart_command() and is_frozen() and Path(sys.executable) == installed_exe():
        set_autostart(True)


def set_autostart(enabled: bool, command: str | None = None) -> None:
    """Пишет в HKCU\\…\\Run – права администратора не нужны."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _reg_name(), 0, winreg.REG_SZ, command or autostart_command())
        else:
            try:
                winreg.DeleteValue(key, _reg_name())
            except FileNotFoundError:
                pass
    log.info("Автозапуск %s", "включён" if enabled else "выключен")


# --- Ярлыки и запись в «Приложения» ------------------------------------------------

def _shell_folder(name: str) -> Path:
    path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        return Path(winreg.QueryValueEx(key, name)[0])


def shortcut_paths() -> dict[str, Path]:
    if sandbox():
        return {"start_menu": sandbox() / "StartMenu" / f"{APP_NAME}.lnk",
                "desktop": sandbox() / "Desktop" / f"{APP_NAME}.lnk"}
    paths = {"start_menu": _shell_folder("Programs") / f"{APP_NAME}.lnk"}
    try:
        paths["desktop"] = _shell_folder("Desktop") / f"{APP_NAME}.lnk"
    except OSError:
        pass
    return paths


def run_powershell(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW,
    )


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(link: Path, target: Path, description: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(link) + "); "
        f"$s.TargetPath = {_ps_quote(target)}; $s.WorkingDirectory = {_ps_quote(target.parent)}; "
        f"$s.Description = {_ps_quote(description)}; $s.IconLocation = {_ps_quote(str(target) + ',0')}; $s.Save()"
    )
    result = run_powershell(script)
    if result.returncode != 0 or not link.exists():
        raise OSError(f"Не удалось создать ярлык {link.name}: {result.stderr.strip()[:200]}")


def _read_uninstall_value(name: str, default=None):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _uninstall_key()) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def register_uninstaller(exe: Path, pulled_models: list[str] = ()) -> None:
    """Запись в «Параметры → Приложения». Помнит модели, скачанные в Ollama для Gloss, чтобы удалить их вместе с ним."""
    models = sorted(set(_read_uninstall_value("ModelsPulledByGloss", []) or []) | set(pulled_models))
    size_kb = (_dir_size(exe.parent) + _dir_size(engine_dir()) + _dir_size(models_dir())) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _uninstall_key()) as key:
        values = {
            "DisplayName": APP_NAME,
            "DisplayVersion": VERSION,
            "Publisher": APP_NAME,
            "DisplayIcon": f'"{exe}",0',
            "InstallLocation": str(exe.parent),
            "UninstallString": f'"{exe}" --uninstall',
            "QuietUninstallString": f'"{exe}" --uninstall --quiet',
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, min(size_kb, 0xFFFFFFFF))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ModelsPulledByGloss", 0, winreg.REG_MULTI_SZ, models)


def installed_version() -> str | None:
    return _read_uninstall_value("DisplayVersion")


def refresh_registered_version() -> None:
    """После самообновления: показать в «Приложениях» новую версию."""
    if installed_version() and Path(sys.executable).parent == install_dir():
        register_uninstaller(installed_exe())


def stop_running_gloss(exclude_self: bool = False) -> None:
    """Завершает запущенные копии Gloss (перед заменой exe или удалением).

    exclude_self – не трогать текущий процесс и его родителя: onefile-exe PyInstaller
    состоит из двух процессов Gloss.exe, и убитый загрузчик оставил бы в %TEMP% папку _MEI….
    """
    keep = [os.getpid(), os.getppid()] if exclude_self else []
    if sandbox():
        # В песочнице – только копии из неё: рабочий Gloss пользователя не трогаем.
        script = (f"Get-Process {APP_NAME} -ErrorAction SilentlyContinue | Where-Object "
                  f"{{ $_.Path -like {_ps_quote(str(sandbox()) + '*')} -and "
                  f"@({','.join(map(str, keep)) or 0}) -notcontains $_.Id }} | Stop-Process -Force")
        run_powershell(script)
        return
    command = ["taskkill", "/F"]
    for pid in keep:
        command += ["/FI", f"PID ne {pid}"]
    subprocess.run(command + ["/IM", f"{APP_NAME}.exe"], capture_output=True, creationflags=NO_WINDOW)


# --- Удаление ------------------------------------------------------------------------

def stop_engine() -> None:
    """Останавливает встроенный движок (только наш llama-server, а не чужие копии llama.cpp)."""
    run_powershell(f"Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object "
                   f"{{ $_.Path -like {_ps_quote(str(engine_dir()) + '*')} }} | Stop-Process -Force")


def _delete_ollama_models(models: list[str]) -> None:
    if sandbox():
        log.info("Песочница: модели %s в настоящей Ollama не удаляю", models)
        return
    import sysinfo
    from config import OllamaSettings, defaults  # импорт здесь: удаление не зависит от llm при старте
    from llm import OllamaClient
    server = None
    if sysinfo.ollama_get("/api/version") is None:
        exe = sysinfo.ollama_exe()
        if exe is None:
            log.info("Ollama не установлена, модели %s удалять негде", models)
            return
        # Ollama не запущена: запускаем её на время удаления, иначе модели остались бы на диске навсегда.
        server = subprocess.Popen([str(exe), "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        deadline = time.monotonic() + 15
        while server.poll() is None and time.monotonic() < deadline and sysinfo.ollama_get("/api/version") is None:
            time.sleep(0.5)
    client = OllamaClient(defaults(installed_config_path()).model, OllamaSettings(url="http://localhost:11434"))
    try:
        for model in models:
            try:
                log.info("Удаление модели %s из Ollama: %s", model, "ок" if client.delete_model(model) else "не найдена")
            except Exception as exc:  # удаление Gloss не должно останавливаться на полпути
                log.warning("Модель %s не удалена: %s", model, exc)
    finally:
        if server is not None:
            server.terminate()


@dataclass
class UninstallChoice:
    remove_data: bool = True        # настройки и журналы
    remove_models: bool = True      # модели встроенного движка
    remove_ollama_models: list[str] = field(default_factory=list)  # скачанные Gloss в Ollama пользователя


@dataclass
class UninstallInfo:
    models_bytes: int                      # модели встроенного движка на диске
    ollama_models: list[tuple[str, int]]   # (модель, байт) – скачанные в Ollama для Gloss


def uninstall_info() -> UninstallInfo:
    import sysinfo
    pulled = list(_read_uninstall_value("ModelsPulledByGloss", []) or [])
    if not pulled:
        return UninstallInfo(_dir_size(models_dir()), [])
    tags = sysinfo.ollama_get("/api/tags", timeout=3)
    if tags is None:
        # Ollama не запущена: размер не узнать, но модели на месте, пока она установлена.
        ollama_models = [(m, 0) for m in pulled] if sysinfo.ollama_exe() else []
    else:
        sizes = {item.get("name"): int(item.get("size") or 0) for item in tags.get("models") or []}
        ollama_models = [(m, sizes[m]) for m in pulled if m in sizes]
    return UninstallInfo(_dir_size(models_dir()), ollama_models)


def _gloss_items(include_data: bool, include_models: bool) -> list[Path]:
    items = [install_dir(), *shortcut_paths().values(), *temp_dirs(), *engine_dirs()]
    if include_data:
        items += [installed_config_path().parent, data_dir() / "logs", data_dir() / "state.json"]
    if include_models:
        items.append(models_dir())
    if include_data and include_models:
        items.append(data_dir())
    return items


def leftovers(include_data: bool = True, include_models: bool = True, include_program: bool = True) -> list[str]:
    """Что от Gloss ещё осталось в системе (пусто – удалён без следа)."""
    found = [str(p) for p in _gloss_items(include_data, include_models)
             if p.exists() and (include_program or p != install_dir())]
    if _autostart_value() is not None:
        found.append(rf"HKCU\{_RUN_KEY}\{_reg_name()}")
    if installed_version() is not None:
        found.append(rf"HKCU\{_uninstall_key()}")
    return found


def perform_uninstall(choice: UninstallChoice, report=lambda text: None) -> list[str]:
    """Удаляет Gloss и выбранные компоненты. Возвращает список того, что удалить не удалось."""
    log.info("Удаление: данные=%s, модели=%s, модели Ollama=%s",
             choice.remove_data, choice.remove_models, choice.remove_ollama_models)
    report("Останавливаю Gloss…")
    stop_running_gloss(exclude_self=True)
    stop_engine()

    report("Убираю ярлыки и записи в реестре…")
    set_autostart(False)
    for link in shortcut_paths().values():
        link.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _uninstall_key())
    except OSError:
        pass

    if choice.remove_ollama_models:
        report("Удаляю модели из Ollama…")
        _delete_ollama_models(choice.remove_ollama_models)

    report("Удаляю движок нейросети и временные файлы…")
    for folder in [*engine_dirs(), *temp_dirs()]:
        shutil.rmtree(folder, ignore_errors=True)
    if choice.remove_models:
        report("Удаляю модели…")
        shutil.rmtree(models_dir(), ignore_errors=True)
    report("Удаляю файлы программы…")
    folder = install_dir()
    running_from_folder = is_frozen() and Path(sys.executable).parent == folder
    log.info("Папка программы %s, удаление %s", folder, "после выхода" if running_from_folder else "сразу")
    if running_from_folder:
        # Свой exe заблокирован, пока жив процесс и его загрузчик PyInstaller: ждём выхода обоих
        # и удаляем папку с повторами (загрузчик ещё какое-то время чистит свои временные файлы).
        script = (f"Wait-Process -Id {os.getpid()},{os.getppid()} -Timeout 120 -ErrorAction SilentlyContinue; "
                  f"for ($i = 0; $i -lt 60 -and (Test-Path -LiteralPath {_ps_quote(folder)}); $i++) {{ "
                  f"Remove-Item -LiteralPath {_ps_quote(folder)} -Recurse -Force -ErrorAction SilentlyContinue; "
                  "Start-Sleep -Milliseconds 500 }")
        # Только CREATE_NO_WINDOW: с DETACHED_PROCESS PowerShell завершается, не выполнив скрипт.
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         cwd=tempfile.gettempdir(), creationflags=NO_WINDOW | 0x00000200)  # + NEW_PROCESS_GROUP
    else:
        shutil.rmtree(folder, ignore_errors=True)
    if choice.remove_data:
        report("Удаляю настройки и журналы…")
        logging.shutdown()  # закрыть gloss.log, иначе папку журналов не удалить
        logging.getLogger().handlers = [logging.NullHandler()]  # и не дать создать его заново
        shutil.rmtree(installed_config_path().parent, ignore_errors=True)
        shutil.rmtree(data_dir() / "logs", ignore_errors=True)
        (data_dir() / "state.json").unlink(missing_ok=True)
    try:
        data_dir().rmdir()  # только если опустела
    except OSError:
        pass

    return leftovers(choice.remove_data, choice.remove_models, include_program=not running_from_folder)
