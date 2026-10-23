"""Защита текста в кавычках: названия полей, кнопок, форм и цитаты модель не меняет.

Перед отправкой каждый фрагмент в кавычках (вместе с кавычками) заменяется меткой [[1]], [[2]]…
Модель видит только метки, а после ответа на их место возвращается исходный текст символ в символ.
Поэтому изменить то, что в кавычках, модель не может, даже если нарушит инструкцию.

Кавычки: «ёлочки», „лапки“, “английские” и прямые "двойные". Одинарные не трогаем: тот же
символ стоит в апострофах (it's). Фрагмент не переходит через пустую строку: непарная кавычка
не должна «съесть» полтекста. Вложенные кавычки («…„…“…») защищаются вместе с внешними.
"""
from __future__ import annotations

import re

_PAIRS = {"«": "»", "„": "“", "“": "”", '"': '"'}
_TOKEN = "[[{}]]"
# Метка в ответе модели; модель иногда добавляет пробелы или обрамляет метку кавычками.
_TOKEN_RE = re.compile(r'([«"„“]?)\[\[\s*(\d+)\s*\]\]([»"“”]?)')

RULE = ("Also: tokens like [[1]] stand for fixed fragments of the original text (names of fields, buttons, forms, "
        "quotations). Copy every such token exactly as written, in its place in the sentence. Never change, "
        "translate, split or remove it and never put quotation marks around it.")


def _closing(text: str, start: int) -> int:
    """Индекс закрывающей кавычки для открывающей в позиции start или -1."""
    opening = text[start]
    closing = _PAIRS[opening]
    depth = 0
    for index in range(start + 1, len(text)):
        char = text[index]
        if text.startswith("\n\n", index) or text.startswith("\r\n\r\n", index):
            return -1
        if char == closing and depth == 0:
            return index
        if opening != closing and char == opening:
            depth += 1
        elif opening != closing and char == closing:
            depth -= 1
    return -1


def protect(text: str) -> tuple[str, list[str]]:
    """Заменяет фрагменты в кавычках метками. Возвращает (текст с метками, фрагменты по порядку)."""
    if "[[" in text:  # в самом тексте уже есть похожие метки: не путаем их с нашими
        return text, []
    fragments: list[str] = []
    parts: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in _PAIRS:
            end = _closing(text, index)
            if end == index + 1:  # пустые кавычки "" пропускаем целиком, чтобы не спутать их со следующими
                parts.append(text[index:end + 1])
                index = end + 1
                continue
            if end > index:
                fragments.append(text[index:end + 1])
                parts.append(_TOKEN.format(len(fragments)))
                index = end + 1
                continue
        parts.append(char)
        index += 1
    return "".join(parts), fragments


def restore(text: str, fragments: list[str]) -> tuple[str, list[int], list[int]]:
    """Возвращает фрагменты на место меток.

    Результат: (текст, номера пропавших меток, номера лишних меток, которых не было в исходном тексте).
    """
    seen: set[int] = set()
    unknown: list[int] = []

    def put(match: re.Match) -> str:
        number = int(match.group(2))
        if not 1 <= number <= len(fragments):
            unknown.append(number)
            return match.group(0)
        seen.add(number)
        fragment = fragments[number - 1]
        before, after = match.group(1), match.group(3)
        if before and after:
            return fragment  # модель обрамила метку кавычками, а они уже есть во фрагменте
        return before + fragment + after

    restored = _TOKEN_RE.sub(put, text)
    missing = [n for n in range(1, len(fragments) + 1) if n not in seen]
    return restored, missing, unknown
