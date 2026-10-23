"""Обновления: программы (GitHub Releases) и модели (реестр Ollama).

В интернет Gloss обращается только здесь: по команде пользователя («Проверить обновления»
в меню) и, если включено updates.auto_check, сам раз в сутки (один запрос к GitHub: какая
версия последняя и что в ней нового). Тексты пользователя сюда никогда не попадают.

Как применяется обновление exe: новый Gloss.exe скачивается во временную
папку и запускается с ключом --apply-update. Он ждёт выхода старого процесса,
копирует себя на место старого exe и запускает его с --after-update.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import changelog
import winapi as w
from config import APP_NAME, VERSION

log = logging.getLogger(__name__)

GITHUB_API = os.environ.get("GLOSS_GITHUB_API", "https://api.github.com")
OLLAMA_REGISTRY = os.environ.get("GLOSS_OLLAMA_REGISTRY", "https://registry.ollama.ai")
ASSET_NAME = f"{APP_NAME}.exe"
SUMS_NAME = "SHA256SUMS.txt"


class UpdateError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


class Aborted(Exception):
    pass


# --- Форматирование прогресса (общее с установщиком) ------------------------------

def format_size(value: float) -> str:
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} ГБ".replace(".", ",")
    return f"{value / 1024 ** 2:.0f} МБ"


def _eta(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, int(seconds))} с"
    return f"{int(seconds // 60)} мин {int(seconds % 60):02d} с"


class Rate:
    """Скорость загрузки и оставшееся время со сглаживанием."""

    def __init__(self, done: int = 0) -> None:
        self.reset(done)

    def reset(self, done: int) -> None:
        """Начать отсчёт скорости с уже имеющихся байт (при докачке они не в счёт)."""
        self.last_time = time.monotonic()
        self.last_done = done
        self.speed = 0.0

    def text(self, done: int, total: int) -> str:
        now = time.monotonic()
        if now - self.last_time >= 0.5:
            instant = max(0.0, (done - self.last_done) / (now - self.last_time))
            self.speed = instant if not self.speed else 0.7 * self.speed + 0.3 * instant
            self.last_time, self.last_done = now, done
        parts = [f"{format_size(done)} из {format_size(total)}" if total else format_size(done)]
        if self.speed > 0:
            parts.append(f"{format_size(self.speed)}/с")
            if total > done:
                parts.append(f"осталось {_eta((total - done) / self.speed)}")
        return " · ".join(parts)


class PullProgress:
    """Сводит события /api/pull (по слоям) в одну полосу прогресса."""

    def __init__(self, expected_bytes: int = 0):
        self.expected = expected_bytes
        self.layers: dict[str, tuple[int, int]] = {}
        self.rate = Rate()

    @property
    def total(self) -> int:
        return max(sum(t for t, _ in self.layers.values()), self.expected)

    def feed(self, event: dict) -> tuple[float | None, str]:
        status = str(event.get("status", ""))
        if event.get("total"):
            self.layers[event.get("digest", status)] = (int(event["total"]), int(event.get("completed") or 0))
        if status.startswith("pulling") and self.layers:
            done, total = sum(c for _, c in self.layers.values()), self.total
            return (min(done / total, 1.0) if total else None), self.rate.text(done, total)
        if status.startswith("verifying"):
            return None, "Проверяю целостность…"
        if status.startswith("writing") or status == "success":
            return None, "Завершаю…"
        return None, "Связываюсь с реестром моделей…"


# --- HTTP ---------------------------------------------------------------------------

def _open(url: str, accept: str = "*/*", timeout: float = 20):
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}", "Accept": accept})
    return urllib.request.urlopen(request, timeout=timeout)


def _get(url: str, accept: str = "*/*") -> bytes:
    try:
        with _open(url, accept) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Сервер ответил ошибкой {exc.code}.") from None
    except OSError as exc:
        raise UpdateError(f"Нет связи с сервером обновлений ({exc.__class__.__name__}). "
                          "Проверьте подключение к интернету.") from None


# --- Версии и релизы ----------------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", text)[:3]]
    return tuple(numbers + [0] * (3 - len(numbers)))  # type: ignore[return-value]


def is_newer(candidate: str, current: str = VERSION) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    notes: str
    asset_url: str
    asset_size: int
    sha256: str | None


def latest_release(repo: str) -> Release:
    raw = _get(f"{GITHUB_API}/repos/{repo}/releases/latest", "application/vnd.github+json")
    try:
        data = json.loads(raw)
    except ValueError:
        raise UpdateError("Сервер обновлений вернул непонятный ответ.") from None
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    asset = assets.get(ASSET_NAME)
    if not asset:
        raise UpdateError(f"В последнем выпуске нет файла {ASSET_NAME}.")
    sha256 = None
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        sha256 = digest.split(":", 1)[1].lower()
    elif SUMS_NAME in assets:
        sums = _get(assets[SUMS_NAME]["browser_download_url"]).decode("utf-8", "replace")
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == ASSET_NAME:
                sha256 = parts[0].lower()
    return Release(
        version=str(data.get("tag_name") or "").lstrip("vV"),
        page_url=str(data.get("html_url") or f"https://github.com/{repo}/releases"),
        notes=str(data.get("body") or "")[:2000],
        asset_url=str(asset.get("browser_download_url")),
        asset_size=int(asset.get("size") or 0),
        sha256=sha256,
    )


def collect_notes(releases: list, current: str = VERSION) -> list[tuple[str, list[str]]]:
    """Что нового во всех опубликованных версиях новее current: [(версия, пункты)], новые сверху."""
    found = []
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        version = str(item.get("tag_name") or "").lstrip("vV")
        if version and is_newer(version, current):
            found.append((version, changelog.points(str(item.get("body") or ""))))
    return sorted(found, key=lambda pair: parse_version(pair[0]), reverse=True)


def notes_since(repo: str, current: str = VERSION) -> list[tuple[str, list[str]]]:
    raw = _get(f"{GITHUB_API}/repos/{repo}/releases?per_page=30", "application/vnd.github+json")
    try:
        return collect_notes(json.loads(raw), current)
    except ValueError:
        raise UpdateError("Сервер обновлений вернул непонятный ответ.") from None


def update_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-update"


def download_release(release: Release, on_progress: Callable[[float | None, str], None],
                     cancel: threading.Event) -> Path:
    """Скачивает новый Gloss.exe во временную папку и проверяет SHA-256."""
    if not release.sha256:
        raise UpdateError("У выпуска нет контрольной суммы. Обновление небезопасно, отменяю.")
    folder = update_dir()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET_NAME
    digest = hashlib.sha256()
    rate = Rate()
    try:
        with _open(release.asset_url, timeout=30) as response, open(target, "wb") as out:
            total = int(response.headers.get("Content-Length") or release.asset_size or 0)
            done = 0
            while chunk := response.read(256 * 1024):
                if cancel.is_set():
                    raise Aborted
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                on_progress(done / total if total else None, rate.text(done, total))
    except Aborted:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError(f"Загрузка обновления прервалась ({exc.__class__.__name__}).") from None
    if digest.hexdigest() != release.sha256:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError("Контрольная сумма скачанного файла не совпала, обновление отменено.")
    log.info("Обновление %s скачано и проверено", release.version)
    return target


def start_apply(new_exe: Path) -> None:
    """Запускает новый exe, который заменит текущий после его выхода. Вызывающий должен завершиться."""
    subprocess.Popen([str(new_exe), "--apply-update", sys.executable, str(os.getpid())],
                     cwd=new_exe.parent, creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP


def apply_update(target: str, old_pid: int) -> None:
    """Выполняется новым exe из временной папки: ждёт старый процесс и подменяет файл."""
    w.wait_for_process(old_pid, timeout_ms=20000)
    for _ in range(40):  # exe может ещё быть занят антивирусом или завершающимся процессом
        try:
            shutil.copy2(sys.executable, target)
            break
        except PermissionError:
            time.sleep(0.25)
    else:
        log.error("Не удалось заменить %s", target)
        w.message_box("Не удалось установить обновление: файл программы занят.\n"
                      "Перезагрузите компьютер и попробуйте снова.", APP_NAME, 0x10)
        subprocess.Popen([target], cwd=str(Path(target).parent), creationflags=0x00000008)
        return
    log.info("Файл программы обновлён: %s", target)
    subprocess.Popen([target, "--after-update"], cwd=str(Path(target).parent), creationflags=0x00000008)


def cleanup_update_dir() -> None:
    """Удаляет временную копию после обновления (она может ещё завершаться – пробуем несколько раз)."""
    folder = update_dir()
    for _ in range(20):
        if not folder.exists():
            return
        shutil.rmtree(folder, ignore_errors=True)
        time.sleep(0.25)


# --- Модель ---------------------------------------------------------------------------

def _manifest_url(model: str) -> str | None:
    name, _, tag = model.partition(":")
    if name.count("/") > 1 or "." in name.split("/")[0] and "/" in name:
        return None  # модели с других реестров (hf.co/…) проверить не можем
    namespace, _, repo = name.rpartition("/")
    return f"{OLLAMA_REGISTRY}/v2/{namespace or 'library'}/{repo}/manifests/{tag or 'latest'}"


def remote_model_digest(model: str) -> str:
    url = _manifest_url(model)
    if url is None:
        raise UpdateError("Для этой модели проверка обновлений не поддерживается.")
    manifest = _get(url, "application/vnd.docker.distribution.manifest.v2+json")
    return hashlib.sha256(manifest).hexdigest()


def model_update_available(model: str, local_digest: str) -> bool:
    """True, если в реестре Ollama лежит другая версия модели, чем скачанная."""
    remote = remote_model_digest(model)
    log.info("Модель %s: локальная %s…, в реестре %s…", model, local_digest[:12], remote[:12])
    return remote != local_digest.lower()
