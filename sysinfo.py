"""Сведения о компьютере и сравнение с требованиями Gloss и выбранной модели."""
from __future__ import annotations

import ctypes
import http.client
import json
import math
import os
import shutil
import sys
import urllib.request
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

import engine
import model_files
from catalog import DEFAULT_MODEL, MODELS

GB = 1024 ** 3

GLOSS_DISK_GB = 0.1
ENGINE_DISK_GB = 0.1
OLLAMA_API = ("127.0.0.1", 11434)


@dataclass
class Gpu:
    name: str
    vram_gb: float
    vendor: str  # nvidia / amd / intel / other

    @property
    def accelerates(self) -> bool:
        # Vulkan ускоряет дискретные видеокарты; встроенная графика Intel почти не помогает.
        discrete = self.vendor in ("nvidia", "amd") or "arc" in self.name.lower()
        return discrete and self.vram_gb >= 2


@dataclass
class OllamaState:
    installed: bool = False
    exe: Path | None = None
    running: bool = False
    version: str = ""
    models: set[str] = field(default_factory=set)

    def has_model(self, model: str) -> bool:
        return model in self.models or f"{model}:latest" in self.models


@dataclass
class SystemInfo:
    windows: str
    build: int
    arch: str          # x64 / arm64 / x86
    cpu: str
    threads: int
    avx2: bool
    ram_gb: float
    gpu: Gpu | None
    free_gb: dict[str, float]   # диск → свободно
    ollama: OllamaState
    engine_installed: bool = False
    builtin_models: set[str] = field(default_factory=set)  # скачанные для встроенного движка
    internet: bool | None = None  # None – не проверяли


@dataclass
class Check:
    name: str
    required: str
    actual: str
    status: str  # ok / warn / fail


# --- Сбор сведений -------------------------------------------------------------

def _reg(root, path: str, name: str, default=None):
    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def _windows() -> tuple[str, int]:
    build = sys.getwindowsversion().build
    nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    display = _reg(winreg.HKEY_LOCAL_MACHINE, nt, "DisplayVersion", "") or _reg(winreg.HKEY_LOCAL_MACHINE, nt, "ReleaseId", "")
    name = "Windows 11" if build >= 22000 else "Windows 10" if build >= 10240 else "Windows (старая версия)"
    return f"{name} {display}".strip(), build


def _arch() -> str:
    try:
        process, native = wintypes.USHORT(), wintypes.USHORT()
        fn = ctypes.windll.kernel32.IsWow64Process2
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        if fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            return {0x8664: "x64", 0xAA64: "arm64", 0x014C: "x86"}.get(native.value, "x64")
    except (AttributeError, OSError):
        pass
    machine = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    return {"AMD64": "x64", "ARM64": "arm64", "x86": "x86"}.get(machine.upper(), "x64")


def arch() -> str:
    """Архитектура Windows: x64, arm64 или x86."""
    return _arch()


def _ram_gb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / GB


def _gpu() -> Gpu | None:
    """Видеокарта с наибольшей памятью. Читаем реестр драйверов: WMI ограничивает VRAM 4 ГБ."""
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best: Gpu | None = None
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return None
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            name = _reg(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{sub}", "DriverDesc", "") or ""
            if not name or any(skip in name for skip in ("Basic", "Remote", "Virtual", "Parsec", "Indirect")):
                continue
            memory = _reg(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{sub}", "HardwareInformation.qwMemorySize")
            if memory is None:
                memory = _reg(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{sub}", "HardwareInformation.MemorySize", 0)
            if isinstance(memory, bytes):
                memory = int.from_bytes(memory[:8], "little")
            lower = name.lower()
            vendor = ("nvidia" if "nvidia" in lower else "amd" if ("amd" in lower or "radeon" in lower)
                      else "intel" if "intel" in lower else "other")
            gpu = Gpu(name.strip(), (memory or 0) / GB, vendor)
            if best is None or gpu.vram_gb > best.vram_gb:
                best = gpu
    return best


def _free_space() -> dict[str, float]:
    drives = {}
    for path in (os.environ.get("LOCALAPPDATA", "C:\\"), ollama_models_dir()):
        drive = Path(path).anchor or "C:\\"
        try:
            drives[drive] = shutil.disk_usage(drive).free / GB
        except OSError:
            pass
    return drives


def ollama_models_dir() -> str:
    return os.environ.get("OLLAMA_MODELS") or str(Path.home() / ".ollama" / "models")


def _ollama_registered_dir() -> Path | None:
    """Папка установки из записи Ollama в «Приложениях» (Inno Setup, ключ …_is1)."""
    base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    return None
                index += 1
                name = _reg(winreg.HKEY_CURRENT_USER, rf"{base}\{sub}", "DisplayName", "") or ""
                if name.startswith("Ollama"):
                    location = _reg(winreg.HKEY_CURRENT_USER, rf"{base}\{sub}", "InstallLocation", "")
                    return Path(location) if location else None
    except OSError:
        return None


def ollama_exe() -> Path | None:
    candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"]
    registered = _ollama_registered_dir()
    if registered:
        candidates.append(registered / "ollama.exe")
    found = shutil.which("ollama")
    if found:
        candidates.append(Path(found))
    return next((p for p in candidates if p.is_file()), None)


def ollama_get(path: str, timeout: float = 2.0) -> dict | None:
    conn = http.client.HTTPConnection(*OLLAMA_API, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return json.loads(response.read() or b"{}") if response.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def ollama_state() -> OllamaState:
    state = OllamaState()
    state.exe = ollama_exe()
    state.installed = state.exe is not None
    version = ollama_get("/api/version")
    if version is not None:
        state.running = True
        state.installed = True
        state.version = str(version.get("version", ""))
        tags = ollama_get("/api/tags", timeout=5) or {}
        for item in tags.get("models") or []:
            state.models.update(n for n in (item.get("name"), item.get("model")) if isinstance(n, str))
    return state


def internet_ok() -> bool:
    try:
        request = urllib.request.Request("https://huggingface.co/", method="HEAD")
        with urllib.request.urlopen(request, timeout=6):
            return True
    except Exception:
        return False


def collect() -> SystemInfo:
    windows, build = _windows()
    avx2 = bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # PF_AVX2_INSTRUCTIONS_AVAILABLE
    cpu = _reg(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
               "ProcessorNameString", "Неизвестный процессор") or "Неизвестный процессор"
    return SystemInfo(
        windows=windows, build=build, arch=_arch(), cpu=" ".join(cpu.split()),
        threads=os.cpu_count() or 1, avx2=avx2, ram_gb=_ram_gb(), gpu=_gpu(),
        free_gb=_free_space(), ollama=ollama_state(),
        engine_installed=engine.is_installed(), builtin_models=set(model_files.downloaded_models()),
    )




# --- Оценка -----------------------------------------------------------------------

def format_gb(value: float) -> str:
    return f"{value:.0f} ГБ" if value >= 10 else f"{value:.1f} ГБ".replace(".", ",")


def _local_drive() -> str:
    return Path(os.environ.get("LOCALAPPDATA", "C:\\")).anchor


def has_model(info: SystemInfo, model: str, backend: str = "builtin") -> bool:
    return model in info.builtin_models if backend == "builtin" else info.ollama.has_model(model)


def needs_download(info: SystemInfo, model: str, backend: str = "builtin") -> bool:
    if backend == "builtin":
        return not info.engine_installed or not has_model(info, model, backend)
    return not has_model(info, model, backend)


def download_gb(info: SystemInfo, model: str, backend: str = "builtin") -> float:
    total = 0.0
    if backend == "builtin" and not info.engine_installed:
        total += engine.download_mb(info.arch) / 1024
    if not has_model(info, model, backend):
        total += MODELS[model]["size_gb"]
    return total


def disk_needed(info: SystemInfo, model: str | None, backend: str = "builtin") -> dict[str, float]:
    """Сколько места нужно на каждом диске. model=None – только сам Gloss и движок."""
    need = {_local_drive(): GLOSS_DISK_GB + (ENGINE_DISK_GB if backend == "builtin" else 0)}
    if model and not has_model(info, model, backend):
        drive = _local_drive() if backend == "builtin" else Path(ollama_models_dir()).anchor
        need[drive] = need.get(drive, 0) + MODELS[model]["size_gb"] + 0.5
    return need


def runs_on_gpu(info: SystemInfo, model: str) -> bool:
    return bool(info.gpu and info.gpu.accelerates and info.gpu.vram_gb >= MODELS[model]["vram_gb"])


def evaluate_system(info: SystemInfo) -> list[Check]:
    """Общие требования к компьютеру. Подробно, как пойдёт модель, показывает model_fit."""
    checks: list[Check] = []

    status = "ok" if info.build >= 19045 else "warn" if info.build >= 17763 else "fail"
    checks.append(Check("Windows", "Windows 10 22H2 или Windows 11", f"{info.windows} (сборка {info.build})", status))

    arch_text = {"x64": "64-бит", "arm64": "ARM64", "x86": "32-бит"}[info.arch]
    cpu_status = "fail" if info.arch == "x86" else "ok" if info.avx2 and info.threads >= 4 else "warn"
    cpu_note = f"{info.cpu}, {info.threads} потоков, {arch_text}" + ("" if info.avx2 or info.arch == "arm64" else ", без AVX2")
    checks.append(Check("Процессор", "64-битный, от 4 потоков, AVX2", cpu_note, cpu_status))

    model = MODELS[DEFAULT_MODEL]
    ram_status = "ok" if info.ram_gb >= model["ram_gb"] - 0.5 else "warn" if info.ram_gb >= model["size_gb"] + 2 else "fail"
    checks.append(Check("Оперативная память", f"от {model['ram_gb']} ГБ (минимум {math.ceil(model['size_gb'] + 2)} ГБ)",
                        format_gb(info.ram_gb), ram_status))

    if info.gpu is None:
        gpu_text, gpu_status = "не найдена, модель будет работать на процессоре", "warn"
    elif info.gpu.accelerates:
        gpu_text, gpu_status = f"{info.gpu.name}, {format_gb(info.gpu.vram_gb)}", "ok"
    else:
        gpu_text, gpu_status = f"{info.gpu.name}: почти не ускоряет, работа на процессоре", "warn"
    checks.append(Check("Видеокарта", "необязательно; ускоряет работу в несколько раз", gpu_text, gpu_status))

    drive = _local_drive()
    need = disk_needed(info, None)[drive]
    free = info.free_gb.get(drive, 0)
    checks.append(Check(f"Место на диске {drive[:2]}", f"{format_gb(need)} + место под модель", f"{format_gb(free)} свободно",
                        "ok" if free >= need + 2 else "warn" if free >= need else "fail"))

    if info.internet is None:
        checks.append(Check("Интернет", "нужен для загрузки", "проверяю…", "warn"))
    else:
        checks.append(Check("Интернет", "нужен для загрузки", "есть" if info.internet else "нет доступа",
                            "ok" if info.internet else "fail"))

    engine_text = "уже установлен" if info.engine_installed else \
        f"будет скачан ({engine.download_mb(info.arch):.0f} МБ)"
    checks.append(Check("Движок нейросети", "llama.cpp, ставится вместе с Gloss", engine_text, "ok"))
    return checks


def system_blockers(info: SystemInfo) -> list[str]:
    """Что мешает установке вообще. Интернет не нужен, если всё уже скачано: это решает model_fit."""
    return [c.name for c in evaluate_system(info) if c.status == "fail" and c.name != "Интернет"]


@dataclass
class ModelFit:
    status: str      # ok / warn / fail
    verdict: str     # одна строка: как модель пойдёт на этом ПК


def model_fit(info: SystemInfo, model: str, backend: str = "builtin") -> ModelFit:
    spec = MODELS[model]
    for drive, need in disk_needed(info, model, backend).items():
        if info.free_gb.get(drive, 0) < need:
            return ModelFit("fail", f"Не хватит места на диске {drive[:2]}: нужно {format_gb(need)}")
    if info.ram_gb < spec["size_gb"] + 2:
        return ModelFit("fail", f"Не хватит оперативной памяти: нужно от {math.ceil(spec['size_gb'] + 2)} ГБ")
    if needs_download(info, model, backend) and info.internet is False:
        return ModelFit("fail", "Нет интернета для загрузки")
    if runs_on_gpu(info, model):
        return ModelFit("ok", f"Быстро, на видеокарте: абзац {spec['gpu_speed']}")
    if info.ram_gb < spec["ram_gb"] - 0.5:
        return ModelFit("warn", f"Памяти впритык ({info.ram_gb:.0f} из {spec['ram_gb']} ГБ), медленно: "
                                f"абзац {spec['cpu_speed']}")
    if spec["cpu_ok"]:
        return ModelFit("ok", f"На процессоре: абзац {spec['cpu_speed']}")
    return ModelFit("warn", f"Медленно, на процессоре: абзац {spec['cpu_speed']}")


def model_requirements(model: str) -> str:
    spec = MODELS[model]
    gpu = "видеокарта по желанию" if spec["cpu_ok"] else f"видеокарта от {spec['vram_gb']} ГБ"
    return f"{format_gb(spec['size_gb'])} · ОЗУ от {spec['ram_gb']} ГБ · {gpu}"
