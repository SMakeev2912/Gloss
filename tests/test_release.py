import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import release  # noqa: E402


def test_wait_for_release_finds_run_by_commit_not_by_tag(monkeypatch):
    # В истории Actions может остаться старый запуск с тем же тегом (например, после сброса истории):
    # искать нужно запуск именно этого коммита.
    calls = []

    def fake_run(*args, capture=False):
        calls.append(args)
        return json.dumps([{"databaseId": 42}])

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    release.wait_for_release("v1.1.0", "af361d0", "https://github.com/owner/Gloss")
    listing = calls[0]
    assert "--commit" in listing and listing[listing.index("--commit") + 1] == "af361d0"
    assert "--branch" not in listing
