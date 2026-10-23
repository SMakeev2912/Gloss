"""Модель Gloss: установщик и встроенный движок работают с одной моделью, DEFAULT_MODEL.

size_gb – объём загрузки, ram_gb – рекомендуемая оперативная память,
vram_gb – видеопамять для работы на видеокарте, gpu_speed / cpu_speed – время
на абзац, cpu_ok – терпимо ли без видеокарты.
"""
from __future__ import annotations

HF_URL = "https://huggingface.co"

MODELS = {
    "gigachat3.1:10b-a1.8b": {
        "title": "GigaChat 3.1 Lightning",
        "about": "Модель Сбера, обученная в первую очередь на русском: грамотно расставляет запятые, "
                 "исправляет опечатки и держит деловой стиль. Видеокарта не обязательна.",
        "size_gb": 6.47, "ram_gb": 16, "vram_gb": 8,
        "gpu_speed": "≈1–3 с", "cpu_speed": "≈2–5 с", "cpu_ok": True,
        "repo": "ai-sage/GigaChat3.1-10B-A1.8B-GGUF",
        "files": ["GigaChat3.1-10B-A1.8B-q4_K_M.gguf"],
    },
}
DEFAULT_MODEL = "gigachat3.1:10b-a1.8b"
