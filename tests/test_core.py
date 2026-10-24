import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from hotkeys import HotkeyError, parse_hotkey  # noqa: E402
from llm import _split_whitespace, split_text  # noqa: E402
from modes import clean_output  # noqa: E402


# --- split_text ---

def test_short_text_is_single_chunk():
    assert split_text("Привет, мир", 100) == ["Привет, мир"]


@pytest.mark.parametrize("limit", [50, 120, 300, 1000])
def test_chunks_reassemble_exactly_and_respect_limit(limit):
    paragraph = "Это предложение номер один. А это второе! Третье? " * 5
    text = "\n\n".join(paragraph.strip() for _ in range(8)) + "\n- пункт\n- пункт\n\n" + "x" * 700
    chunks = split_text(text, limit)
    assert "".join(chunks) == text
    assert all(len(chunk) <= limit for chunk in chunks)


def test_chunks_prefer_paragraph_boundaries():
    a, b = "А" * 40, "Б" * 40
    chunks = split_text(f"{a}\n\n{b}", 60)
    assert chunks == [f"{a}\n\n", b]


def test_split_whitespace():
    assert _split_whitespace("\n  текст \n") == ("\n  ", "текст", " \n")
    assert _split_whitespace("   ") == ("   ", "", "")


# --- clean_output ---

@pytest.mark.parametrize("raw, expected", [
    ("Исправленный текст:\n\nПривет, мир.", "Привет, мир."),
    ("Here is the corrected text:\nHello, world.", "Hello, world."),
    ("«Привет, мир.»", "Привет, мир."),
    ('"Hello, world."', "Hello, world."),
    ("<text>\nПривет, мир.\n</text>", "Привет, мир."),
    ("<think>хм</think>\nПривет, мир.", "Привет, мир."),
    ("```\nПривет, мир.\n```", "Привет, мир."),
    ("  Привет, мир.  \n", "Привет, мир."),
])
def test_clean_output_strips_wrappers(raw, expected):
    assert clean_output("привет мир", raw) == expected


def test_clean_output_keeps_wrappers_present_in_original():
    original = '"Цитата" и ещё "цитата"'
    assert clean_output(original, original) == original
    assert clean_output("«Цитата.»", "«Цитата.»") == "«Цитата.»"
    assert clean_output("Вот что нужно:\nкупить хлеб", "Вот что нужно:\nкупить хлеб.") == "Вот что нужно:\nкупить хлеб."


# --- hotkeys ---

def test_parse_hotkey():
    hk = parse_hotkey(" Ctrl + ALT + g ")
    assert (hk.modifiers, hk.vk, hk.text) == (0x2 | 0x1, ord("G"), "Ctrl+Alt+G")
    assert parse_hotkey("f9").text == "F9"


@pytest.mark.parametrize("spec", ["g", "ctrl+esc", "ctrl+alt", "hyper+g", "ctrl+f99", ""])
def test_parse_hotkey_rejects(spec):
    with pytest.raises(HotkeyError):
        parse_hotkey(spec)


# --- config ---

def test_repo_config_matches_defaults():
    assert json.loads((ROOT / "config.json").read_text(encoding="utf-8")) == config.default_config()


def test_defaults_parse():
    cfg = config.parse(config.default_config(), Path("x.json"))
    assert [m.id for m in cfg.modes] == ["fix", "shorten", "business"]
    assert [m.hotkey.text for m in cfg.modes] == ["Ctrl+Alt+G", "Ctrl+Alt+H", "Ctrl+Alt+J"]
    assert cfg.updates.auto_check is True
    assert cfg.max_chunk_chars == 6000


def _with(**changes):
    raw = config.default_config()
    for dotted, value in changes.items():
        target = raw
        *path, key = dotted.split("__")
        for part in path:
            target = target[part]
        target[key] = value
    return raw


@pytest.mark.parametrize("raw, fragment", [
    (_with(ollama__url="http://example.com:11434"), "внешний адрес"),
    (_with(model__temperature=5), "model.temperature"),
    (_with(backend="cloud"), "backend"),
    (_with(model__name="gpt-9"), "model.name"),
    (_with(max_chunk_chars="много"), "max_chunk_chars"),
    (_with(modes=[]), "modes"),
])
def test_invalid_config(raw, fragment):
    with pytest.raises(config.ConfigError, match=fragment):
        config.parse(raw, Path("x.json"))


def test_duplicate_hotkeys_rejected():
    raw = config.default_config()
    raw["modes"][1]["hotkey"] = "ctrl+alt+g"
    with pytest.raises(config.ConfigError, match="Ctrl\\+Alt\\+G"):
        config.parse(raw, Path("x.json"))


def test_custom_mode_and_partial_config():
    raw = {"modes": [{"id": "emoji", "name": "Эмодзи", "hotkey": "ctrl+alt+e", "prompt": ["Add emoji."]}]}
    cfg = config.parse(raw, Path("x.json"))
    assert cfg.model.name == config.DEFAULT_MODEL and cfg.backend == "builtin"
    assert cfg.modes[0].prompt == "Add emoji." and cfg.modes[0].temperature == 0.2


def test_remote_allowed_explicitly():
    raw = _with(ollama__url="http://192.168.1.5:11434", ollama__allow_remote=True)
    assert config.parse(raw, Path("x.json")).ollama.url == "http://192.168.1.5:11434"


def test_set_mode_enabled(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.load(path)  # создаст файл по умолчанию
    assert cfg.mode("fix").enabled
    config.set_mode_enabled(path, "fix", False)
    assert not config.load(path).mode("fix").enabled


# --- оформление текстов ---

def test_no_em_dashes():
    """В текстах проекта не используем длинное тире (U+2014), только среднее, и то пореже."""
    files = [*ROOT.glob("*.py"), *ROOT.glob("tools/*.py"), *ROOT.glob("tests/*.py"),
             ROOT / "README.md", ROOT / "build.bat", ROOT / ".github" / "workflows" / "release.yml"]
    offenders = [f"{path.name}:{number}" for path in files
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
                 if chr(0x2014) in line]
    assert not offenders, f"Длинные тире: {', '.join(offenders)}"


def test_quotes_are_replaced_and_restored_exactly():
    import quotes
    text = 'Нажмите «Сохранить» в форме "Заявка", цитата «он сказал „да“», пустые "" и 5" дюймов.'
    sent, fragments = quotes.protect(text)
    assert sent == 'Нажмите [[1]] в форме [[2]], цитата [[3]], пустые "" и 5" дюймов.'
    assert fragments == ["«Сохранить»", '"Заявка"', "«он сказал „да“»"]
    assert quotes.restore(sent, fragments) == (text, [], [])
    # Модель обрамила метку кавычками, добавила пробелы, потеряла одну и выдумала лишнюю.
    restored, missing, unknown = quotes.restore("Нажмите «[[1]]» в форме [[ 2 ]] и [[7]].", fragments)
    assert restored == "Нажмите «Сохранить» в форме \"Заявка\" и [[7]]."
    assert (missing, unknown) == ([3], [7])
    # Непарная кавычка не захватывает следующий абзац.
    assert quotes.protect("Начало « без пары\n\nВторой «абзац».")[1] == ["«абзац»"]


def test_pressed_keys_become_hotkeys_regardless_of_layout():
    import winapi as w
    from hotkeys import hotkey_from_keys
    ctrl_alt = w.MOD_CONTROL | w.MOD_ALT
    assert hotkey_from_keys(ctrl_alt, 0x4B).text == "Ctrl+Alt+K"   # «К» в русской раскладке – та же клавиша
    assert hotkey_from_keys(w.MOD_CONTROL | w.MOD_SHIFT, 0x31).text == "Ctrl+Shift+1"
    assert hotkey_from_keys(w.MOD_WIN, 0x70 + 4).text == "Win+F5"
    assert hotkey_from_keys(ctrl_alt, 0xBB).text == "Ctrl+Alt+Plus"
    for vk in (0x4B, 0x0D, 0xBC, 0x61):  # записанное в config.json разбирается обратно в то же сочетание
        hotkey = hotkey_from_keys(ctrl_alt, vk)
        assert parse_hotkey(hotkey.text.lower()) == hotkey
    with pytest.raises(HotkeyError):
        hotkey_from_keys(ctrl_alt, 0x1B)   # Esc занят под отмену
    with pytest.raises(HotkeyError):
        hotkey_from_keys(ctrl_alt, 0x5D)   # клавиша меню не годится


def test_bad_answers_are_retried_and_never_pasted():
    import threading
    import llm

    class FakeModel(llm.LlmClient):
        def __init__(self, answers):
            super().__init__(config.defaults(Path("x.json")).model)
            self.answers, self.calls = list(answers), 0

        def chat(self, messages, temperature, num_predict, cancel=None):
            self.calls += 1
            return self.answers.pop(0)

    cfg = config.defaults(Path("x.json"))
    fix = cfg.mode("fix")

    def run(answers, text="Нажмите «Сохранить» сейчас"):
        model = FakeModel(answers)
        return model, model.process(text, fix, cfg.common_rules, 6000, threading.Event())

    model, result = run(["Нажмите [[1]], это 不便。", "Нажмите [[1]] сейчас."])
    assert (result, model.calls) == ("Нажмите «Сохранить» сейчас.", 2)
    model, result = run(["Нажмите сейчас.", "Нажмите [[1]] сейчас!"])  # метка пропала в первый раз
    assert (result, model.calls) == ("Нажмите «Сохранить» сейчас!", 2)
    with pytest.raises(llm.LlmError, match="другом языке"):
        run(["Нажмите [[1]] 不便", "Нажмите [[1]] 不便"])
    with pytest.raises(llm.LlmError, match="кавычках"):
        run(["Нажмите сейчас", "Нажмите сейчас"])


# --- Диктовка в настройках ---

def test_dictation_defaults_when_section_missing(tmp_path):
    raw = config.default_config()
    del raw["dictation"]
    cfg = config.parse(raw, tmp_path / "config.json")
    assert cfg.dictation.mode.hotkey.text == "Ctrl+Alt+Space"
    assert cfg.dictation.mode.name == "Диктовка"
    assert cfg.dictation.max_record_sec == 300
    assert cfg.mode("dictation") is cfg.dictation.mode
    assert cfg.dictation.mode not in cfg.modes


def test_dictation_hotkey_conflicts_with_mode(tmp_path):
    raw = config.default_config()
    raw["dictation"]["hotkey"] = "ctrl+alt+g"
    with pytest.raises(config.ConfigError, match="Диктовка"):
        config.parse(raw, tmp_path / "config.json")


def test_disabled_dictation_does_not_conflict(tmp_path):
    raw = config.default_config()
    raw["dictation"].update(hotkey="ctrl+alt+g", enabled=False)
    config.parse(raw, tmp_path / "config.json")


def test_max_record_sec_bounds(tmp_path):
    raw = config.default_config()
    raw["dictation"]["max_record_sec"] = 5000
    with pytest.raises(config.ConfigError, match="max_record_sec"):
        config.parse(raw, tmp_path / "config.json")


def test_dictation_prompt_forbids_following_instructions():
    from modes import DEFAULT_DICTATION
    prompt = "\n".join(DEFAULT_DICTATION["prompt"])
    assert "never perform it" in prompt
    assert "Do not paraphrase" in prompt


def test_set_dictation_values_and_reset(tmp_path):
    path = tmp_path / "config.json"
    config._write_json(path, config.default_config())
    config.set_dictation_values(path, {"hotkey": "ctrl+alt+d"})
    assert config.load(path).dictation.mode.hotkey.text == "Ctrl+Alt+D"
    config.reset_hotkeys(path)
    assert config.load(path).dictation.mode.hotkey.text == "Ctrl+Alt+Space"


def test_set_dictation_values_without_section(tmp_path):
    path = tmp_path / "config.json"
    raw = config.default_config()
    del raw["dictation"]
    config._write_json(path, raw)
    config.set_dictation_values(path, {"enabled": False})
    cfg = config.load(path)
    assert not cfg.dictation.mode.enabled
    assert cfg.dictation.mode.hotkey.text == "Ctrl+Alt+Space"


def test_shipped_config_json_matches_defaults():
    shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert shipped["dictation"] == config.default_config()["dictation"]


def test_mode_setters_route_dictation(tmp_path):
    path = tmp_path / "config.json"
    config._write_json(path, config.default_config())
    config.set_mode_hotkey(path, "dictation", "ctrl+alt+d")
    config.set_mode_enabled(path, "dictation", False)
    cfg = config.load(path)
    assert cfg.dictation.mode.hotkey.text == "Ctrl+Alt+D" and not cfg.dictation.mode.enabled
    assert "dictation" not in [m.get("id") for m in config.read_raw(path)["modes"]]


def test_all_modes_ends_with_dictation(tmp_path):
    cfg = config.defaults(tmp_path / "config.json")
    assert [m.id for m in cfg.all_modes] == ["fix", "shorten", "business", "dictation"]
