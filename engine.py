"""Встроенный движок нейросети: llama.cpp (llama-server.exe) рядом с Gloss.

Сборка закреплена (ENGINE_BUILD + SHA-256 архива): установщик и Gloss скачивают
ровно её с официальной страницы выпусков llama.cpp. Сборка Vulkan ускоряется на
видеокартах NVIDIA, AMD и Intel через обычные драйверы, а без подходящей
видеокарты работает на процессоре.

Сервер слушает только 127.0.0.1, запускается по требованию, выгружается после
простоя и завершается вместе с Gloss (Job Object Windows).
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import winapi
from config import APP_NAME, VERSION, data_dir, log_dir
from updater import Aborted, Rate, UpdateError

log = logging.getLogger(__name__)

ENGINE_BUILD = "b11819"
_ASSETS = {
    "x64": ("llama-b11819-bin-win-vulkan-x64.zip",
            "c3b2484d18258425fe3f69d0b6d07dce01501d6e010578dad3e53150a30df557", 35.2),
    "arm64": ("llama-b11819-bin-win-vulkan-arm64.zip",
              "1488b71b8db3149a892b15bea90e41d751a06acaca8b55787929639db5c20fe2", 23.9),
}
_RELEASES_URL = "https://github.com/ggml-org/llama.cpp/releases/download"
# Из архива берём только сервер и библиотеки; утилиты (bench, quantize, cli…) не нужны.
_SKIP_PREFIXES = ("llama-batched", "llama-bench", "llama-cli", "llama-completion", "llama-perplexity",
                  "llama-quantize", "llama-fit", "llama-gguf", "llama-imatrix", "llama-mtmd", "llama-run",
                  "llama-tokenize", "llama-tts", "rpc-server")
NO_WINDOW = 0x08000000


def engine_dir() -> Path:
    return data_dir() / "engine"


def server_exe() -> Path:
    return engine_dir() / "llama-server.exe"


def installed_build() -> str | None:
    try:
        return (engine_dir() / "BUILD").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def is_installed() -> bool:
    return server_exe().is_file() and installed_build() == ENGINE_BUILD


def download_mb(arch: str = "x64") -> float:
    return _ASSETS.get(arch, _ASSETS["x64"])[2]


def install(arch: str, on_progress: Callable[[float | None, str], None], cancel: threading.Event) -> None:
    """Скачивает закреплённую сборку llama.cpp, проверяет SHA-256 и раскладывает в engine_dir()."""
    if arch not in _ASSETS:
        raise UpdateError("Встроенный движок не поддерживает 32-битную Windows.")
    name, sha256, _ = _ASSETS[arch]
    temp = Path(tempfile.gettempdir()) / f"{APP_NAME}-engine"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=True)
    archive = temp / name
    digest = hashlib.sha256()
    rate = Rate()
    log.info("Скачиваю движок %s (%s)", ENGINE_BUILD, name)
    try:
        request = urllib.request.Request(f"{_RELEASES_URL}/{ENGINE_BUILD}/{name}",
                                         headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1 << 20):
                if cancel.is_set():
                    raise Aborted
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                on_progress(done / total if total else None, rate.text(done, total))
    except Aborted:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    except (OSError, urllib.error.HTTPError) as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise UpdateError(f"Не удалось скачать движок нейросети ({exc.__class__.__name__}). "
                          "Проверьте подключение к интернету.") from None
    if digest.hexdigest() != sha256:
        shutil.rmtree(temp, ignore_errors=True)
        raise UpdateError("Контрольная сумма движка нейросети не совпала, установка остановлена ради безопасности.")

    on_progress(None, "Распаковываю…")
    staging = engine_dir().with_name("engine.new")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                base = Path(member.filename).name
                if member.is_dir() or not base or base.lower().startswith(_SKIP_PREFIXES) and "server" not in base:
                    continue
                if base.lower().endswith(".exe") and base.lower() != "llama-server.exe":
                    continue
                with zf.open(member) as src, open(staging / base, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        (staging / "BUILD").write_text(ENGINE_BUILD, encoding="utf-8")
        if not (staging / "llama-server.exe").exists():
            raise UpdateError("В архиве движка нет llama-server.exe.")
        # Прежнюю сборку не удаляем, а отодвигаем: переименовать папку Windows даёт и при запущенном
        # движке, а удалить его файлы нет, и движок остался бы удалённым наполовину. Если engine.old
        # сейчас не удалится, его уберёт следующая установка или удаление Gloss.
        old = engine_dir().with_name("engine.old")
        shutil.rmtree(old, ignore_errors=True)
        if engine_dir().exists():
            try:
                os.replace(engine_dir(), old)
            except PermissionError:
                raise UpdateError("Не удалось заменить движок нейросети: его файлы заняты. "
                                  "Закройте Gloss и повторите.") from None
        os.replace(staging, engine_dir())
        shutil.rmtree(old, ignore_errors=True)
    except (OSError, zipfile.BadZipFile) as exc:
        log.warning("Движок не распакован: %s", exc)
        raise UpdateError(f"Не удалось распаковать движок нейросети ({exc.__class__.__name__}). "
                          "Проверьте свободное место на диске.") from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    log.info("Движок %s установлен в %s", ENGINE_BUILD, engine_dir())


def remove() -> None:
    shutil.rmtree(engine_dir(), ignore_errors=True)


# --- Сервер ------------------------------------------------------------------------

class EngineError(Exception):
    """Ошибка движка, которую можно показать пользователю как есть."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _health(port: int) -> int | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", "/health")
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


class LlamaServer:
    """Один процесс llama-server на одну модель. Потокобезопасен."""

    def __init__(self, model_path: Path, context: int, use_gpu: bool, unload_after_min: float):
        self.model_path = model_path
        self.context = context
        self.use_gpu = use_gpu
        self.unload_after = unload_after_min * 60
        self.port = 0
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_used = time.monotonic()
        self._busy = 0   # запросов в работе (меняется только из потока обработки)
        self._gpu_failed = False
        self._closed = threading.Event()
        threading.Thread(target=self._idle_watch, name="engine-idle", daemon=True).start()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def touch(self) -> None:
        self._last_used = time.monotonic()

    @contextlib.contextmanager
    def in_use(self):
        """Пока идёт запрос, движок не выгружается по простою, даже если ответ генерируется долго."""
        self._busy += 1
        self.touch()
        try:
            yield
        finally:
            self._busy -= 1
            self.touch()

    def ensure_running(self, cancel: threading.Event | None = None, timeout: float = 300) -> int:
        """Запускает сервер, если нужно, и ждёт загрузки модели. Возвращает порт."""
        # Модель может грузиться в другом потоке (предзагрузка): ждём её, но не дольше, чем до отмены.
        while not self._lock.acquire(timeout=0.2):
            if cancel is not None and cancel.is_set():
                raise Aborted
        try:
            self.touch()
            if self.running and _health(self.port) == 200:
                return self.port
            self._stop_locked()
            for gpu in ([True, False] if self.use_gpu and not self._gpu_failed else [False]):
                try:
                    self._start_locked(gpu, cancel, timeout)
                    self.touch()
                    return self.port
                except EngineError:
                    if not gpu:
                        raise
                    log.warning("Движок не запустился с видеокартой, пробую на процессоре")
                    self._gpu_failed = True
            raise EngineError("Движок нейросети не запустился.")
        finally:
            self._lock.release()

    def _start_locked(self, gpu: bool, cancel: threading.Event | None, timeout: float) -> None:
        exe = server_exe()
        if not exe.exists():
            raise EngineError("Движок нейросети не установлен. Запустите установщик Gloss ещё раз.")
        self.port = _free_port()
        command = [str(exe), "-m", str(self.model_path), "--host", "127.0.0.1", "--port", str(self.port),
                   "-c", str(self.context), "-np", "1", "--no-webui"]
        if not gpu:
            command += ["--device", "none"]
        log_dir().mkdir(parents=True, exist_ok=True)
        # В журнал движка пишутся только служебные сообщения (загрузка, ошибки); тексты запросов
        # llama-server выводит лишь в подробном режиме (-v), который мы не включаем.
        log_file = open(log_dir() / "engine.log", "w", encoding="utf-8", errors="replace")
        started = time.monotonic()
        log.info("Запускаю движок: модель %s, %s, порт %d", self.model_path.name,
                 "видеокарта" if gpu else "процессор", self.port)
        try:
            self._process = subprocess.Popen(command, cwd=exe.parent, stdout=log_file, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        finally:
            log_file.close()  # дочерний процесс держит свою копию хэндла
        if not winapi.kill_with_parent(self._process.pid):
            log.warning("Не удалось привязать движок к Gloss: при сбое он может остаться запущенным")
        while True:
            if cancel is not None and cancel.is_set():
                self._stop_locked()
                raise Aborted
            if self._process.poll() is not None:
                code = self._process.returncode
                self._process = None
                raise EngineError(f"Движок нейросети завершился при запуске (код {code}). "
                                  f"Подробности в журнале {log_dir() / 'engine.log'}.")
            if _health(self.port) == 200:
                log.info("Движок готов за %.1f с", time.monotonic() - started)
                return
            if time.monotonic() - started > timeout:
                self._stop_locked()
                raise EngineError(f"Модель не загрузилась за {timeout:.0f} с.")
            time.sleep(0.25)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def close(self) -> None:
        """Останавливает сервер и фоновую проверку простоя (объект больше не используется)."""
        self._closed.set()
        self.stop()

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
            log.info("Движок остановлен")

    def _idle_watch(self) -> None:
        while not self._closed.wait(15):
            if (self.unload_after and self.running and not self._busy
                    and time.monotonic() - self._last_used > self.unload_after):
                if self._lock.acquire(blocking=False):
                    try:
                        log.info("Движок простаивал %d мин, выгружаю модель из памяти", self.unload_after // 60)
                        self._stop_locked()
                    finally:
                        self._lock.release()
