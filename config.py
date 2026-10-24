"""Загрузка, валидация и сохранение config.json."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hotkeys import HotkeyError, parse_hotkey
from catalog import DEFAULT_MODEL, MODELS
from modes import DEFAULT_COMMON_RULES, DEFAULT_DICTATION, DEFAULT_MODES, Mode

log = logging.getLogger(__name__)

APP_NAME = "Gloss"
VERSION = "1.1.2"


def _update_repo() -> str | None:
    """Репозиторий GitHub для обновлений («владелец/имя»).

    build.bat записывает его в _build_info.py из `git remote`; для запуска
    из исходников можно задать переменную окружения GLOSS_UPDATE_REPO.
    """
    if os.environ.get("GLOSS_UPDATE_REPO"):
        return os.environ["GLOSS_UPDATE_REPO"]
    try:
        from _build_info import UPDATE_REPO  # type: ignore[import-not-found]
        return UPDATE_REPO or None
    except ImportError:
        return None


UPDATE_REPO = _update_repo()

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    pass


BACKENDS = ("builtin", "ollama")


@dataclass(frozen=True)
class ModelSettings:
    name: str               # модель из каталога (qwen2.5:3b-instruct) или путь к своему файлу .gguf
    temperature: float
    context: int
    timeout_sec: float
    preload_on_start: bool
    unload_after_min: float  # выгружать модель из памяти после простоя (0 – не выгружать)
    use_gpu: bool


@dataclass(frozen=True)
class OllamaSettings:
    url: str


@dataclass(frozen=True)
class CaptureSettings:
    copy_timeout_ms: int
    copy_retries: int
    modifier_release_timeout_ms: int
    restore_clipboard: bool
    restore_delay_ms: int


@dataclass(frozen=True)
class UiSettings:
    show_overlay: bool
    notify_unchanged: bool


@dataclass(frozen=True)
class UpdateSettings:
    auto_check: bool  # сам проверять новые версии: через минуту после запуска и раз в сутки


@dataclass(frozen=True)
class DictationSettings:
    mode: Mode              # id = "dictation": сочетание, промпт, включена ли
    max_record_sec: int     # дольше запись не идёт: останавливается и обрабатывается сама


@dataclass(frozen=True)
class Config:
    path: Path
    backend: str            # builtin – встроенный движок, ollama – установленная Ollama
    model: ModelSettings
    ollama: OllamaSettings
    capture: CaptureSettings
    ui: UiSettings
    updates: UpdateSettings
    max_chunk_chars: int
    protect_quotes: bool    # текст в кавычках модель не меняет (см. quotes.py)
    common_rules: str
    modes: tuple[Mode, ...]
    dictation: DictationSettings

    @property
    def all_modes(self) -> tuple[Mode, ...]:
        """Режимы для выделенного текста и диктовка последней: для меню и окна сочетаний."""
        return (*self.modes, self.dictation.mode)

    def mode(self, mode_id: str) -> Mode | None:
        if mode_id == self.dictation.mode.id:
            return self.dictation.mode
        return next((m for m in self.modes if m.id == mode_id), None)


def default_config() -> dict:
    return {
        "backend": "builtin",
        "model": {
            "name": DEFAULT_MODEL,
            "temperature": 0.2,
            "context": 8192,
            "timeout_sec": 120,
            "preload_on_start": True,
            "unload_after_min": 15,
            "use_gpu": True,
        },
        "ollama": {
            "url": "http://localhost:11434",
            "allow_remote": False,
        },
        "max_chunk_chars": 6000,
        "protect_quotes": True,
        "capture": {
            "copy_timeout_ms": 500,
            "copy_retries": 1,
            "modifier_release_timeout_ms": 1500,
            "restore_clipboard": True,
            "restore_delay_ms": 400,
        },
        "ui": {
            "show_overlay": True,
            "notify_unchanged": True,
        },
        "updates": {
            "auto_check": True,
        },
        "common_rules": list(DEFAULT_COMMON_RULES),
        "modes": copy.deepcopy(DEFAULT_MODES),
        "dictation": copy.deepcopy(DEFAULT_DICTATION),
    }


# --- Пути ---------------------------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    return Path(sys.executable).parent if is_frozen() else Path(__file__).resolve().parent


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME


def log_dir() -> Path:
    return data_dir() / "logs"


def load_state() -> dict:
    """Служебное состояние (пройден ли онбординг и т. п.) – не настройки пользователя."""
    try:
        state = json.loads((data_dir() / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**changes) -> None:
    _write_json(data_dir() / "state.json", {**load_state(), **changes})


def config_path() -> Path:
    """Где лежит config.json.

    GLOSS_CONFIG – явный путь; при запуске из исходников – рядом с main.py;
    у exe – рядом с exe, если файл там есть (портативный режим), иначе в %APPDATA%\\Gloss.
    """
    if os.environ.get("GLOSS_CONFIG"):
        return Path(os.environ["GLOSS_CONFIG"])
    local = app_dir() / "config.json"
    if not is_frozen() or local.exists():
        return local
    return installed_config_path()


# --- Чтение и запись ----------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_raw(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc.strerror or exc}") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Ошибка JSON в строке {exc.lineno}, столбце {exc.colno}: {exc.msg}") from None
    if not isinstance(raw, dict):
        raise ConfigError("Корень config.json должен быть объектом { … }")
    return raw


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        log.info("config.json не найден, создаю настройки по умолчанию: %s", path)
        _write_json(path, default_config())
    return parse(read_raw(path), path)


def installed_config_path() -> Path:
    """Где установленный Gloss.exe ищет настройки (см. config_path)."""
    return Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME / "config.json"


def set_model(path: Path, model: str, backend: str = "builtin") -> None:
    """Задаёт движок и модель, не трогая остальные настройки. Нечитаемый файл сохраняется в .bak."""
    try:
        raw = read_raw(path) if path.exists() else default_config()
    except ConfigError:
        os.replace(path, path.with_suffix(".json.bak"))
        log.warning("config.json повреждён – сохранён как config.json.bak, создан новый")
        raw = default_config()
    section = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    section["name"] = model
    raw["model"] = section
    raw["backend"] = backend
    _write_json(path, raw)


def set_auto_check(path: Path, enabled: bool) -> None:
    raw = read_raw(path)
    section = raw.get("updates") if isinstance(raw.get("updates"), dict) else {}
    section["auto_check"] = enabled
    raw["updates"] = section
    _write_json(path, raw)


def _set_mode_values(path: Path, values: dict[str, dict]) -> None:
    """{id режима: {ключ: значение}} – меняет только эти поля, остальное в config.json не трогает."""
    raw = read_raw(path)
    modes = raw.setdefault("modes", copy.deepcopy(DEFAULT_MODES))
    found = set()
    for item in modes:
        if isinstance(item, dict) and item.get("id") in values:
            item.update(values[item["id"]])
            found.add(item["id"])
    if values.keys() - found:
        raise ConfigError(f"Режим «{next(iter(values.keys() - found))}» не найден в config.json")
    _write_json(path, raw)


def set_mode_enabled(path: Path, mode_id: str, enabled: bool) -> None:
    if mode_id == "dictation":
        set_dictation_values(path, {"enabled": enabled})
    else:
        _set_mode_values(path, {mode_id: {"enabled": enabled}})


def set_mode_hotkey(path: Path, mode_id: str, hotkey: str) -> None:
    if mode_id == "dictation":
        set_dictation_values(path, {"hotkey": hotkey})
    else:
        _set_mode_values(path, {mode_id: {"hotkey": hotkey}})


def reset_hotkeys(path: Path) -> None:
    """Сочетания по умолчанию для стандартных режимов и диктовки (свои режимы не трогаем)."""
    present = {m.get("id") for m in read_raw(path).get("modes") or [] if isinstance(m, dict)}
    _set_mode_values(path, {m["id"]: {"hotkey": m["hotkey"]} for m in DEFAULT_MODES if m["id"] in present})
    set_dictation_values(path, {"hotkey": DEFAULT_DICTATION["hotkey"]})


def set_dictation_values(path: Path, values: dict) -> None:
    """Меняет только переданные ключи секции dictation; если секции нет, создаёт её со значениями по умолчанию."""
    raw = read_raw(path)
    section = raw.get("dictation")
    if not isinstance(section, dict):
        # Сочетание по умолчанию не записываем: если его занял свой режим, разбор оставит диктовку без него.
        section = {k: v for k, v in copy.deepcopy(DEFAULT_DICTATION).items() if k != "hotkey"}
    section.update(values)
    raw["dictation"] = section
    _write_json(path, raw)


# --- Валидация ----------------------------------------------------------------

class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def section(self, raw: dict, key: str, defaults: dict) -> dict:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            self.errors.append(f"«{key}» должен быть объектом {{ … }}")
            value = {}
        for unknown in value.keys() - defaults.keys():
            log.warning("config.json: неизвестный ключ «%s.%s» – игнорирую", key, unknown)
        return {**defaults, **value}

    def number(self, d: dict, key: str, where: str, lo: float, hi: float, integer: bool = False):
        value = d[key]
        ok_type = isinstance(value, int) if integer else isinstance(value, (int, float))
        if isinstance(value, bool) or not ok_type:
            self.errors.append(f"«{where}.{key}» должно быть {'целым ' if integer else ''}числом")
            return lo
        if not lo <= value <= hi:
            self.errors.append(f"«{where}.{key}» должно быть от {lo} до {hi}, сейчас {value}")
            return lo
        return value

    def boolean(self, d: dict, key: str, where: str) -> bool:
        value = d[key]
        if not isinstance(value, bool):
            self.errors.append(f"«{where}.{key}» должно быть true или false")
            return False
        return value

    def string(self, d: dict, key: str, where: str) -> str:
        value = d[key]
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"«{where}.{key}» должно быть непустой строкой")
            return ""
        return value.strip()

    def text(self, value, where: str) -> str:
        """Длинный текст: строка или список строк (так удобнее править JSON)."""
        if isinstance(value, list) and all(isinstance(line, str) for line in value):
            value = "\n".join(value)
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"«{where}» должно быть непустой строкой или списком строк")
            return ""
        return value.strip()


def _check_url(url: str, allow_remote: bool, v: _Validator) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        v.errors.append(f"«ollama.url» должен выглядеть как http://localhost:11434, сейчас «{url}»")
    elif not allow_remote and parts.hostname.lower() not in _LOCAL_HOSTS and not parts.hostname.startswith("127."):
        v.errors.append(
            f"«ollama.url» указывает на внешний адрес ({parts.hostname}). Gloss работает только локально; "
            "если это осознанно, задайте \"allow_remote\": true"
        )


def _parse_modes(raw_modes, default_temperature: float, v: _Validator) -> tuple[Mode, ...]:
    if not isinstance(raw_modes, list) or not raw_modes:
        v.errors.append("«modes» должен быть непустым списком режимов")
        return ()
    modes: list[Mode] = []
    seen_ids: set[str] = set()
    seen_hotkeys: dict[tuple[int, int], str] = {}
    for index, item in enumerate(raw_modes, start=1):
        where = f"modes[{index}]"
        if not isinstance(item, dict):
            v.errors.append(f"«{where}» должен быть объектом {{ … }}")
            continue
        mode_id = item.get("id")
        if not isinstance(mode_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", mode_id):
            v.errors.append(f"«{where}.id»: только латинские буквы, цифры, «_» или «-»")
            continue
        where = f"modes[{mode_id}]"
        if mode_id in seen_ids:
            v.errors.append(f"Режим с id «{mode_id}» встречается дважды")
            continue
        seen_ids.add(mode_id)

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            v.errors.append(f"«{where}.name» должно быть непустой строкой")
            name = mode_id
        prompt = v.text(item.get("prompt"), f"{where}.prompt")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            v.errors.append(f"«{where}.enabled» должно быть true или false")
            enabled = True
        temperature = item.get("temperature")
        if temperature is not None:
            temperature = v.number(item, "temperature", where, 0.0, 2.0)

        hotkey = None
        spec = item.get("hotkey")
        if spec not in (None, ""):
            if not isinstance(spec, str):
                v.errors.append(f"«{where}.hotkey» должно быть строкой вида \"ctrl+alt+g\"")
            else:
                try:
                    hotkey = parse_hotkey(spec)
                except HotkeyError as exc:
                    v.errors.append(f"«{where}.hotkey»: {exc}")
        if hotkey and enabled:
            key = (hotkey.modifiers, hotkey.vk)
            if key in seen_hotkeys:
                v.errors.append(f"{hotkey} назначена и режиму «{seen_hotkeys[key]}», и режиму «{mode_id}»")
            seen_hotkeys[key] = mode_id

        modes.append(Mode(
            id=mode_id,
            name=name.strip(),
            prompt=prompt,
            hotkey=hotkey,
            enabled=enabled,
            temperature=temperature if temperature is not None else default_temperature,
        ))
    return tuple(modes)


def _parse_dictation(d: dict, modes: tuple[Mode, ...], v: _Validator, explicit: bool) -> DictationSettings:
    """explicit – секция dictation есть в config.json (её писал пользователь или Gloss), а не взята по умолчанию."""
    name = v.string(d, "name", "dictation") or DEFAULT_DICTATION["name"]
    enabled = v.boolean(d, "enabled", "dictation")
    hotkey = None
    spec = d["hotkey"]
    if spec not in (None, ""):
        if not isinstance(spec, str):
            v.errors.append("«dictation.hotkey» должно быть строкой вида \"ctrl+alt+space\"")
        else:
            try:
                hotkey = parse_hotkey(spec)
            except HotkeyError as exc:
                v.errors.append(f"«dictation.hotkey»: {exc}")
    if hotkey and enabled:
        for mode in modes:
            if mode.enabled and mode.hotkey and (mode.hotkey.modifiers, mode.hotkey.vk) == (hotkey.modifiers, hotkey.vk):
                if not explicit:
                    # Секции ещё нет (обновление): сочетание по умолчанию уже занято своим режимом пользователя.
                    log.warning("%s занята режимом «%s», диктовка остаётся без сочетания", hotkey, mode.name)
                    hotkey = None
                    break
                v.errors.append(f"{hotkey} назначена и режиму «{mode.name}», и режиму «{name}»")
    mode = Mode(
        id="dictation",
        name=name,
        prompt=v.text(d["prompt"], "dictation.prompt"),
        hotkey=hotkey,
        enabled=enabled,
        temperature=v.number(d, "temperature", "dictation", 0.0, 2.0),
    )
    return DictationSettings(mode=mode, max_record_sec=v.number(d, "max_record_sec", "dictation", 10, 1800, integer=True))


def parse(raw: dict, path: Path) -> Config:
    defaults = default_config()
    v = _Validator()
    for unknown in raw.keys() - defaults.keys():
        log.warning("config.json: неизвестный ключ «%s» – игнорирую", unknown)

    backend = raw.get("backend", defaults["backend"])
    if backend not in BACKENDS:
        v.errors.append(f"«backend» должен быть \"builtin\" (встроенный движок) или \"ollama\", сейчас «{backend}»")
        backend = "builtin"

    m = v.section(raw, "model", defaults["model"])
    model = ModelSettings(
        name=v.string(m, "name", "model"),
        temperature=v.number(m, "temperature", "model", 0.0, 2.0),
        context=v.number(m, "context", "model", 512, 262144, integer=True),
        timeout_sec=v.number(m, "timeout_sec", "model", 5, 3600),
        preload_on_start=v.boolean(m, "preload_on_start", "model"),
        unload_after_min=v.number(m, "unload_after_min", "model", 0, 10080),
        use_gpu=v.boolean(m, "use_gpu", "model"),
    )
    if backend == "builtin" and model.name and model.name not in MODELS and not model.name.lower().endswith(".gguf"):
        v.errors.append(f"«model.name»: модели «{model.name}» нет в списке Gloss. Укажите одну из: "
                        f"{', '.join(MODELS)}, или путь к своему файлу .gguf")

    o = v.section(raw, "ollama", defaults["ollama"])
    url = v.string(o, "url", "ollama").rstrip("/")
    allow_remote = v.boolean(o, "allow_remote", "ollama")
    if url:
        _check_url(url, allow_remote, v)
    ollama = OllamaSettings(url=url)

    c = v.section(raw, "capture", defaults["capture"])
    capture = CaptureSettings(
        copy_timeout_ms=v.number(c, "copy_timeout_ms", "capture", 50, 10000, integer=True),
        copy_retries=v.number(c, "copy_retries", "capture", 0, 5, integer=True),
        modifier_release_timeout_ms=v.number(c, "modifier_release_timeout_ms", "capture", 0, 10000, integer=True),
        restore_clipboard=v.boolean(c, "restore_clipboard", "capture"),
        restore_delay_ms=v.number(c, "restore_delay_ms", "capture", 0, 10000, integer=True),
    )

    u = v.section(raw, "ui", defaults["ui"])
    ui = UiSettings(
        show_overlay=v.boolean(u, "show_overlay", "ui"),
        notify_unchanged=v.boolean(u, "notify_unchanged", "ui"),
    )

    up = v.section(raw, "updates", defaults["updates"])
    updates = UpdateSettings(auto_check=v.boolean(up, "auto_check", "updates"))

    top = {**defaults, **raw}
    max_chunk_chars = v.number(top, "max_chunk_chars", "config", 200, 100000, integer=True)
    protect_quotes = v.boolean(top, "protect_quotes", "config")
    common_rules = v.text(top["common_rules"], "common_rules")
    modes = _parse_modes(top["modes"], model.temperature, v)
    dictation = _parse_dictation(v.section(raw, "dictation", defaults["dictation"]), modes, v,
                                 explicit=isinstance(raw.get("dictation"), dict) and "hotkey" in raw["dictation"])

    if v.errors:
        raise ConfigError("\n".join(v.errors))
    return Config(
        path=path,
        backend=backend,
        model=model,
        ollama=ollama,
        capture=capture,
        ui=ui,
        updates=updates,
        max_chunk_chars=max_chunk_chars,
        protect_quotes=protect_quotes,
        common_rules=common_rules,
        modes=modes,
        dictation=dictation,
    )


def defaults(path: Path) -> Config:
    return parse(default_config(), path)


if __name__ == "__main__":
    # python config.py --write-default config.json
    if len(sys.argv) == 3 and sys.argv[1] == "--write-default":
        _write_json(Path(sys.argv[2]), default_config())
    else:
        print("Использование: python config.py --write-default <путь>")
