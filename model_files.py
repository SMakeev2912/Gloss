"""Файлы моделей встроенного движка: загрузка с Hugging Face, докачка, проверка, обновления.

Модели лежат в %LOCALAPPDATA%\\Gloss\\models. Рядом – models.json с SHA-256
каждого скачанного файла: по нему видно, что файл цел, и есть ли в
репозитории более новая версия.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from catalog import HF_URL, MODELS
from config import APP_NAME, VERSION, data_dir
from updater import Aborted, Rate, UpdateError

log = logging.getLogger(__name__)

Progress = Callable[[float | None, str], None]


def models_dir() -> Path:
    return data_dir() / "models"


def _manifest_path() -> Path:
    return models_dir() / "models.json"


def load_manifest() -> dict:
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(data: dict) -> None:
    models_dir().mkdir(parents=True, exist_ok=True)
    tmp = _manifest_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _manifest_path())


def is_custom(name: str) -> bool:
    """Своя модель: путь к файлу .gguf вместо названия из каталога."""
    return name.lower().endswith(".gguf")


def model_path(name: str) -> Path | None:
    """Файл, который передаётся движку (для разбитых моделей – первая часть), или None."""
    if is_custom(name):
        path = Path(os.path.expandvars(name))
        return path if path.is_file() else None
    spec = MODELS.get(name)
    entry = load_manifest().get(name)
    if not spec or not entry:
        return None
    files = [models_dir() / f for f in spec["files"]]
    return files[0] if all(f.is_file() for f in files) and set(entry.get("files", {})) >= set(spec["files"]) else None


def is_downloaded(name: str) -> bool:
    return model_path(name) is not None


def downloaded_models() -> list[str]:
    return [name for name in load_manifest() if name in MODELS and is_downloaded(name)]


def total_size() -> int:
    folder = models_dir()
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder.exists() else 0


# --- Hugging Face ------------------------------------------------------------------

def _open(url: str, headers: dict | None = None, timeout: float = 60):
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}", **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout)


def remote_files(name: str) -> dict[str, tuple[str, int]]:
    """{файл: (sha256, размер)} из репозитория модели на Hugging Face."""
    spec = MODELS[name]
    try:
        with _open(f"{HF_URL}/api/models/{spec['repo']}/tree/main", {"Accept": "application/json"}, 20) as response:
            tree = json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Нет связи с хранилищем моделей ({exc.__class__.__name__}). "
                          "Проверьте подключение к интернету.") from None
    found = {}
    for item in tree if isinstance(tree, list) else []:
        lfs = item.get("lfs") or {}
        if item.get("path") in spec["files"] and lfs.get("oid"):
            found[item["path"]] = (str(lfs["oid"]).lower(), int(item.get("size") or lfs.get("size") or 0))
    missing = set(spec["files"]) - set(found)
    if missing:
        raise UpdateError(f"В хранилище моделей не найдены файлы: {', '.join(sorted(missing))}.")
    return found


def update_available(name: str) -> bool:
    """True, если в репозитории файлы модели изменились по сравнению со скачанными."""
    local = load_manifest().get(name, {}).get("files", {})
    remote = remote_files(name)
    return any(local.get(file) != sha for file, (sha, _size) in remote.items())


def _sha256(path: Path, cancel: threading.Event) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            if cancel.is_set():
                raise Aborted
            digest.update(chunk)
    return digest.hexdigest()


def download(name: str, on_progress: Progress, cancel: threading.Event) -> None:
    """Скачивает (докачивает, обновляет) модель из каталога и проверяет SHA-256 каждого файла."""
    spec = MODELS[name]
    remote = remote_files(name)
    manifest = load_manifest()
    have = manifest.get(name, {}).get("files", {})
    folder = models_dir()
    folder.mkdir(parents=True, exist_ok=True)

    todo = [f for f in spec["files"] if not ((folder / f).is_file() and have.get(f) == remote[f][0])]
    total = sum(remote[f][1] for f in todo)
    done_before = 0
    rate = Rate()
    for file in todo:
        sha, size = remote[file]
        part = folder / (file + ".part")
        start = part.stat().st_size if part.exists() else 0
        if start > size:
            part.unlink()
            start = 0
        log.info("Модель %s: файл %s, докачка с %d из %d байт", name, file, start, size)
        if start < size:
            rate.reset(done_before + start)
            headers = {"Range": f"bytes={start}-"} if start else {}
            try:
                with _open(f"{HF_URL}/{spec['repo']}/resolve/main/{file}", headers) as response:
                    if start and response.status != 206:  # сервер не умеет докачку – начинаем заново
                        start = 0
                    with open(part, "ab" if start else "wb") as out:
                        written = start
                        while chunk := response.read(1 << 20):
                            if cancel.is_set():
                                raise Aborted
                            out.write(chunk)
                            written += len(chunk)
                            got = done_before + written
                            on_progress(min(got / total, 1.0) if total else None, rate.text(got, total))
            except Aborted:
                raise
            except urllib.error.HTTPError as exc:
                raise UpdateError(f"Хранилище моделей ответило ошибкой {exc.code}.") from None
            except OSError as exc:
                raise UpdateError(f"Загрузка модели прервалась ({exc.__class__.__name__}). "
                                  "Повторите, и она продолжится с того же места.") from None
        on_progress(None, "Проверяю целостность файла…")
        if _sha256(part, cancel) != sha:
            part.unlink(missing_ok=True)
            raise UpdateError("Файл модели повреждён при загрузке (контрольная сумма не совпала). Повторите загрузку.")
        os.replace(part, folder / file)
        have[file] = sha
        manifest[name] = {"files": have}
        _save_manifest(manifest)
        done_before += size
    log.info("Модель %s готова", name)


def delete_all() -> None:
    shutil.rmtree(models_dir(), ignore_errors=True)
