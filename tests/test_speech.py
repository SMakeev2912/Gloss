import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import speech  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
ENGINE_DIR = "sherpa-onnx-v1.13.8-win-x64-shared-MT-Release-no-tts"
MODEL_DIR = "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16"
NEEDED = ["BUILD", "model", "onnxruntime.dll", "sherpa-onnx-offline.exe"]
MODEL_NEEDED = ["LICENSE", "decoder.onnx", "encoder.int8.onnx", "joiner.onnx", "tokens.txt"]


def make_tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def engine_tar(tmp_path: Path, extra: dict[str, bytes] | None = None) -> Path:
    return make_tar(tmp_path / "engine.tar.bz2", {
        f"{ENGINE_DIR}/bin/sherpa-onnx-offline.exe": b"exe",
        f"{ENGINE_DIR}/bin/onnxruntime.dll": b"bin-dll",
        f"{ENGINE_DIR}/bin/sherpa-onnx-offline-websocket-server.exe": b"lishnee",
        f"{ENGINE_DIR}/lib/onnxruntime.dll": b"lib-dll",
        f"{ENGINE_DIR}/include/sherpa-onnx/c-api/c-api.h": b"h",
        **(extra or {}),
    })


def model_tar(tmp_path: Path) -> Path:
    return make_tar(tmp_path / "model.tar.bz2", {
        **{f"{MODEL_DIR}/{name}": name.encode() for name in MODEL_NEEDED},
        f"{MODEL_DIR}/README.md": b"readme",
        f"{MODEL_DIR}/test_wavs/example.wav": b"wav",
    })


def make_installed(folder: Path, build: str) -> None:
    (folder / "model").mkdir(parents=True, exist_ok=True)
    for name in ("sherpa-onnx-offline.exe", "onnxruntime.dll"):
        (folder / name).write_bytes(b"x")
    for name in MODEL_NEEDED:
        (folder / "model" / name).write_bytes(b"x")
    (folder / "BUILD").write_text(build, encoding="utf-8")


# --- Установка ---

def test_extract_keeps_only_needed_files(tmp_path):
    staging = tmp_path / "staging"
    speech._extract(engine_tar(tmp_path), model_tar(tmp_path), staging)
    assert sorted(p.name for p in staging.iterdir()) == NEEDED
    assert sorted(p.name for p in (staging / "model").iterdir()) == MODEL_NEEDED
    assert (staging / "onnxruntime.dll").read_bytes() == b"bin-dll"  # из bin/, а не из lib/
    assert (staging / "BUILD").read_text(encoding="utf-8") == speech.ASR_BUILD


def test_extract_rejects_path_traversal(tmp_path):
    staging = tmp_path / "out" / "staging"
    evil = engine_tar(tmp_path, {f"{ENGINE_DIR}/bin/../../../sherpa-onnx-offline.exe": b"evil"})
    speech._extract(evil, model_tar(tmp_path), staging)
    assert not (tmp_path / "sherpa-onnx-offline.exe").exists()
    assert not (tmp_path / "out" / "sherpa-onnx-offline.exe").exists()


def test_extract_without_exe_fails(tmp_path):
    broken = make_tar(tmp_path / "engine.tar.bz2", {f"{ENGINE_DIR}/bin/onnxruntime.dll": b"dll"})
    with pytest.raises(speech.UpdateError, match="sherpa-onnx-offline.exe"):
        speech._extract(broken, model_tar(tmp_path), tmp_path / "staging")


def test_is_installed_checks_build_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert not speech.is_installed()
    make_installed(speech.speech_dir(), build="старая")
    assert not speech.is_installed()
    make_installed(speech.speech_dir(), build=speech.ASR_BUILD)
    assert speech.is_installed()
    (speech.speech_dir() / "model" / "encoder.int8.onnx").unlink()
    assert not speech.is_installed()


def test_download_mb():
    assert 180 < speech.download_mb("x64") < 190
    assert 180 < speech.download_mb("arm64") < 190


def test_install_on_32bit_refuses(tmp_path, monkeypatch):
    import threading
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(speech.UpdateError, match="32-битную"):
        speech.install("x86", lambda fraction, detail: None, threading.Event())
