"""Записывает _build_info.py: репозиторий GitHub, откуда Gloss будет брать обновления.

Источник: переменная GITHUB_REPOSITORY (GitHub Actions) или `git remote get-url origin`.
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def detect_repo() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""


repo = detect_repo()
(ROOT / "_build_info.py").write_text(
    f'"""Создаётся при сборке (tools/build_info.py). Не редактируйте вручную."""\nUPDATE_REPO = {repo!r}\n',
    encoding="utf-8",
)
print(f"Обновления: {repo or 'не настроены (нет git remote на GitHub)'}")
