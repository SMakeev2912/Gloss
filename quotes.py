"""Защита текста в кавычках: названия полей, кнопок, форм и цитаты модель не меняет.

Перед отправкой каждый фрагмент в кавычках (вместе с кавычками) заменяется меткой «Q1», «Q2»…
Модель видит только метки, а после ответа на их место возвращается исходный текст символ в символ.
Поэтому изменить то, что в кавычках, модель не может, даже если нарушит инструкцию.

Метки именно в ёлочках: GigaChat принимает их за названия и сохраняет, а метки вида [[1]],
{{1}} или QUOTE_1 в длинном тексте считает мусором и выбрасывает (проверено на модели).

Кавычки: «ёлочки», „лапки“, “английские” и прямые "двойные". Одинарные не трогаем: тот же
символ стоит в апострофах (it's). Фрагмент не переходит через пустую строку: непарная кавычка
не должна «съесть» полтекста. Вложенные кавычки («…„…“…») защищаются вместе с внешними.
"""
from __future__ import annotations

import re

_PAIRS = {"«": "»", "„": "“", "“": "”", '"': '"'}
_TOKEN = "«Q{}»"
# Метка в ответе модели: модель иногда меняет кавычки вокруг неё, добавляет пробелы или убирает кавычки.
_TOKEN_RE = re.compile(r'[«"„“]\s*Q(\d+)\s*[»"“”]|\bQ(\d+)\b')
_BARE_TOKEN_RE = re.compile(r"\bQ\d+\b")

RULE = ("Also: tokens like «Q1» stand for fixed fragments of the original text (names of fields, buttons, forms, "
        "quotations). Copy every such token exactly as written, with its quotation marks, in its place in the "
        "sentence. Never change, translate, split or remove it.")


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
    if _BARE_TOKEN_RE.search(text):  # в самом тексте есть «Q3» и подобное: не путаем это с нашими метками
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
        number = int(match.group(1) or match.group(2))
        if not 1 <= number <= len(fragments):
            unknown.append(number)
            return match.group(0)
        seen.add(number)
        return fragments[number - 1]  # кавычки вокруг метки заменяются кавычками самого фрагмента

    restored = _TOKEN_RE.sub(put, text)
    missing = [n for n in range(1, len(fragments) + 1) if n not in seen]
    return restored, missing, unknown
