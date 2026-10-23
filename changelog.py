"""«Что нового»: разбор CHANGELOG.md и описаний выпусков на GitHub.

CHANGELOG.md пишется для пользователей, по разделу на версию:

    ## Готовится          ← сюда пишутся пункты будущей версии
    - Новое…

    ## 1.1.0 · 24.10.2026
    - Что изменилось…

tools/release.py переносит пункты из «Готовится» под номер новой версии, GitHub Actions
берёт раздел версии в описание выпуска, а Gloss показывает эти пункты в окне «Обновления»
и в уведомлениях. CHANGELOG.md лежит и внутри exe: после обновления Gloss рассказывает,
что изменилось, не выходя в интернет.

Запуск из командной строки: python changelog.py notes 1.1.0 – печатает пункты версии
(для описания выпуска на GitHub).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

UNRELEASED = "Готовится"
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)
_BULLET = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")


def changelog_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "CHANGELOG.md"


def _plain(text: str) -> str:
    """Убирает разметку Markdown, которую обычный человек увидел бы как мусор."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # [текст](ссылка) → текст
    text = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", text)
    return text.strip()


def points(text: str) -> list[str]:
    """Пункты списка из текста раздела или описания выпуска. Без списка – непустые строки."""
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    bullets = [_plain(m.group(1)) for line in lines if (m := _BULLET.match(line))]
    if bullets:
        return bullets
    return [_plain(line) for line in lines if not line.lower().startswith("**full changelog")]


def sections(text: str) -> dict[str, str]:
    """{заголовок раздела без даты: его текст}. Заголовок «1.1.0 · 24.10.2026» даёт ключ «1.1.0»."""
    found = list(_HEADING.finditer(text))
    result = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        key = match.group(1).split("·")[0].strip().lstrip("vV")
        result[key] = text[match.end():end].strip()
    return result


def version_points(version: str, text: str | None = None) -> list[str]:
    if text is None:
        try:
            text = changelog_path().read_text(encoding="utf-8")
        except OSError:
            return []
    return points(sections(text).get(version.lstrip("vV"), ""))


def unreleased(text: str) -> list[str]:
    return points(sections(text).get(UNRELEASED, ""))


def release(text: str, version: str, date: str, items: list[str]) -> str:
    """Переносит пункты в новый раздел версии, раздел «Готовится» остаётся пустым."""
    block = f"## {version} · {date}\n\n" + "\n".join(f"- {item}" for item in items) + "\n"
    match = re.search(rf"^##\s+{UNRELEASED}\s*$", text, re.M)
    if not match:
        raise ValueError(f"В CHANGELOG.md нет раздела «## {UNRELEASED}».")
    following = _HEADING.search(text, match.end())
    end = following.start() if following else len(text)
    return text[:match.end()] + "\n\n" + block + ("\n" + text[end:] if following else "")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "notes":
        items = version_points(sys.argv[2], Path(__file__).resolve().with_name("CHANGELOG.md").read_text(encoding="utf-8"))
        print("\n".join(f"- {item}" for item in items) or "- Исправления и улучшения.")
    else:
        print("Использование: python changelog.py notes <версия>")
