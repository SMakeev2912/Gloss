"""Распознавание речи: sherpa-onnx (sherpa-onnx-offline.exe) и модель GigaAM v3 от Сбера.

Сборка и модель закреплены (имя архива + SHA-256): установщик и Gloss скачивают
ровно их с официальной страницы выпусков sherpa-onnx на GitHub. Модель GigaAM v3
обучена на русской речи и сама ставит знаки препинания и заглавные буквы.

Распознавание работает на процессоре, локально: на каждую диктовку Gloss запускает
sherpa-onnx-offline.exe (модель загружается около секунды) и передаёт ему запись,
нарезанную на куски до 20 с.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable

from config import APP_NAME, VERSION, data_dir
from updater import Aborted, Rate, UpdateError

log = logging.getLogger(__name__)

ASR_BUILD = "sherpa-1.13.8+giga-am-v3-punct-2025-12-16"
_RELEASES_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# (тег выпуска, имя архива, SHA-256, размер в байтах)
_ENGINE = {
    "x64": ("v1.13.8", "sherpa-onnx-v1.13.8-win-x64-shared-MT-Release-no-tts.tar.bz2",
            "4b0a94f7b5c606b1b64a19a831c2127559e4b3d34e195465ebc7be73d9ed4783", 23271851),
    "arm64": ("v1.13.8", "sherpa-onnx-v1.13.8-win-arm64-shared-MT-Release-no-tts.tar.bz2",
              "29a864324e658bef2a8b83bd3e12adae8b415a5a232d83902030e8c6efa37dbc", 21872501),
}
_MODEL = ("asr-models", "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16.tar.bz2",
          "f9620a0099019c6afcee26525ef9ed3297fa50dd5691c1902af0c948fc1a470b", 170197019)
EXE = "sherpa-onnx-offline.exe"
_ENGINE_FILES = (EXE, "onnxruntime.dll")  # из bin/ архива движка, остальное не нужно
MODEL_FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt", "LICENSE")

Progress = Callable[[float | None, str], None]


def speech_dir() -> Path:
    return data_dir() / "speech"


def _download_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-speech"


def is_installed() -> bool:
    folder = speech_dir()
    try:
        build = (folder / "BUILD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (build == ASR_BUILD and all((folder / name).is_file() for name in _ENGINE_FILES)
            and all((folder / "model" / name).is_file() for name in MODEL_FILES))


def download_mb(arch: str = "x64") -> float:
    engine_size = _ENGINE.get(arch, _ENGINE["x64"])[3]
    return (engine_size + _MODEL[3]) / (1 << 20)


def install(arch: str, on_progress: Progress, cancel: threading.Event) -> None:
    """Скачивает (докачивает) sherpa-onnx и GigaAM, проверяет SHA-256 и раскладывает в speech_dir()."""
    if arch not in _ENGINE:
        raise UpdateError("Распознавание речи не поддерживает 32-битную Windows.")
    archives = [_ENGINE[arch], _MODEL]
    folder = _download_dir()
    folder.mkdir(parents=True, exist_ok=True)
    total = sum(size for *_, size in archives)
    rate = Rate()
    done_before = 0
    paths = []
    for tag, name, sha, size in archives:
        paths.append(_fetch(f"{_RELEASES_URL}/{tag}/{name}", folder / name, sha, size,
                            lambda got: on_progress(min((done_before + got) / total, 1.0),
                                                    rate.text(done_before + got, total)), cancel))
        done_before += size

    on_progress(None, "Распаковываю…")
    staging = speech_dir().with_name("speech.new")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        _extract(paths[0], paths[1], staging)
        # Прежнюю версию отодвигаем, а не удаляем: так же, как движок (см. engine.install).
        old = speech_dir().with_name("speech.old")
        shutil.rmtree(old, ignore_errors=True)
        if speech_dir().exists():
            try:
                os.replace(speech_dir(), old)
            except PermissionError:
                raise UpdateError("Не удалось заменить распознавание речи: его файлы заняты. "
                                  "Закройте Gloss и повторите.") from None
        os.replace(staging, speech_dir())
        shutil.rmtree(old, ignore_errors=True)
    except (OSError, tarfile.TarError) as exc:
        log.warning("Распознавание речи не распаковано: %s", exc)
        raise UpdateError(f"Не удалось распаковать распознавание речи ({exc.__class__.__name__}). "
                          "Проверьте свободное место на диске.") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)
    log.info("Распознавание речи %s установлено в %s", ASR_BUILD, speech_dir())


def remove() -> None:
    shutil.rmtree(speech_dir(), ignore_errors=True)


def _sha256(path: Path, cancel: threading.Event) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            if cancel.is_set():
                raise Aborted
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, target: Path, sha: str, size: int, on_bytes: Callable[[int], None],
           cancel: threading.Event) -> Path:
    """Скачивает файл с докачкой через .part (как model_files.download) и сверяет SHA-256."""
    if target.is_file() and target.stat().st_size == size and _sha256(target, cancel) == sha:
        on_bytes(size)
        return target  # скачан в прошлый раз, но распаковка не дошла до конца
    part = target.with_name(target.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    if start > size:
        part.unlink()
        start = 0
    log.info("Скачиваю %s, докачка с %d из %d байт", target.name, start, size)
    if start < size:
        headers = {"User-Agent": f"{APP_NAME}/{VERSION}", **({"Range": f"bytes={start}-"} if start else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                if start and response.status != 206:  # сервер не умеет докачку – начинаем заново
                    start = 0
                with open(part, "ab" if start else "wb") as out:
                    written = start
                    while chunk := response.read(1 << 20):
                        if cancel.is_set():
                            raise Aborted
                        out.write(chunk)
                        written += len(chunk)
                        on_bytes(written)
        except Aborted:
            raise
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"Сервер загрузки ответил ошибкой {exc.code}.") from None
        except OSError as exc:
            raise UpdateError(f"Загрузка распознавания речи прервалась ({exc.__class__.__name__}). "
                              "Повторите, и она продолжится с того же места.") from None
    if _sha256(part, cancel) != sha:
        part.unlink(missing_ok=True)
        raise UpdateError("Контрольная сумма распознавания речи не совпала, установка остановлена ради безопасности.")
    os.replace(part, target)
    return target


def _extract(engine_archive: Path, model_archive: Path, staging: Path) -> None:
    """Достаёт из архивов только нужные файлы. Пишем всегда в staging/<имя файла>,
    поэтому «../» в именах членов архива не может вывести за пределы папки."""
    (staging / "model").mkdir(parents=True, exist_ok=True)
    _copy_members(engine_archive, staging, _ENGINE_FILES, parent="bin")
    _copy_members(model_archive, staging / "model", MODEL_FILES, parent=None)
    missing = [name for name in _ENGINE_FILES if not (staging / name).is_file()]
    missing += [name for name in MODEL_FILES if not (staging / "model" / name).is_file()]
    if missing:
        raise UpdateError(f"В архиве распознавания речи нет файлов: {', '.join(missing)}.")
    (staging / "BUILD").write_text(ASR_BUILD, encoding="utf-8")


def _copy_members(archive: Path, target: Path, names: tuple[str, ...], parent: str | None) -> None:
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.name not in names or ".." in path.parts:
                continue
            if parent is not None and path.parent.name != parent:
                continue
            with tar.extractfile(member) as src, open(target / path.name, "wb") as dst:
                shutil.copyfileobj(src, dst)
