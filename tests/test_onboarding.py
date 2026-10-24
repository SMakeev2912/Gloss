import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from onboarding import dictation_problem  # noqa: E402


def dictation_mode(tmp_path, **changes):
    return replace(config.defaults(tmp_path / "config.json").dictation.mode, **changes)


def test_ready_has_no_problem(tmp_path):
    assert dictation_problem(dictation_mode(tmp_path), "ready", None) is None


def test_disabled_or_without_hotkey(tmp_path):
    assert "Настройки" in dictation_problem(dictation_mode(tmp_path, enabled=False), "ready", None)
    assert "Сочетания клавиш" in dictation_problem(dictation_mode(tmp_path, hotkey=None), "ready", None)


def test_speech_not_ready(tmp_path):
    assert "загружается" in dictation_problem(dictation_mode(tmp_path), "installing", None)
    assert "загрузит" in dictation_problem(dictation_mode(tmp_path), "missing", None)


def test_model_problem_still_lets_dictate(tmp_path):
    message = dictation_problem(dictation_mode(tmp_path), "ready", "Модель не скачана")
    assert "без улучшения" in message and "Модель не скачана" in message


def test_disabled_wins_over_other_problems(tmp_path):
    message = dictation_problem(dictation_mode(tmp_path, enabled=False), "installing", "Модель не скачана")
    assert "Настройки" in message
