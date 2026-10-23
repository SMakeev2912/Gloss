"""Работа с моделью: встроенный движок (llama.cpp) или Ollama, разбиение текста, отмена.

Используется http.client из стандартной библиотеки: так у нас есть прямой
доступ к сокету, и отмена (Esc) мгновенно закрывает соединение, после чего
движок прекращает генерацию. Прокси и переменные окружения не используются:
запросы идут только на локальный адрес.
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import urlsplit

import engine
import model_files
import quotes
from config import Config, ModelSettings, OllamaSettings
from modes import CONDENSING_MODES, Mode, build_messages, clean_output

log = logging.getLogger(__name__)


class Cancelled(Exception):
    """Пользователь отменил обработку."""


class LlmError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


class BackendUnavailable(LlmError):
    """Движок не запущен или не установлен."""


class ModelMissing(LlmError):
    """Модель не скачана."""


class _Refused(BackendUnavailable):
    """Соединение отклонено: на этом адресе никто не слушает."""


_CONNECT_TIMEOUT = 1.0
# Китайская, японская и корейская письменность, их знаки препинания и полноширинные символы («，»).
_CJK = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")
DOWNLOAD_HINT = "Скачайте её: меню значка → «Проверить обновления…» → «Скачать»."


@dataclass(frozen=True)
class Health:
    ok: bool
    status: str          # коротко, для меню трея
    message: str         # подробно, для уведомления
    can_download: bool = False  # проблема решается загрузкой модели в окне обновлений


# --- Разбиение на части --------------------------------------------------------

# От крупных разделителей к мелким: абзацы, строки, предложения, пробелы.
_SPLIT_LEVELS = (r"\n[ \t]*\n\s*", r"\n", r"(?<=[.!?…])[ \t]+", r"[ \t]+")


def _pieces(text: str, limit: int, level: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    if level >= len(_SPLIT_LEVELS):
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    parts = re.split(f"({_SPLIT_LEVELS[level]})", text)
    pieces: list[str] = []
    for i in range(0, len(parts), 2):
        piece = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if not piece:
            continue
        pieces.extend(_pieces(piece, limit, level + 1) if len(piece) > limit else [piece])
    return pieces


def split_text(text: str, limit: int) -> list[str]:
    """Режет текст на части не длиннее limit, по возможности по абзацам.

    Разделители остаются в частях, поэтому "".join(части) == text.
    """
    chunks: list[str] = []
    current = ""
    for piece in _pieces(text, limit, 0):
        if current and len(current) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


def _split_whitespace(chunk: str) -> tuple[str, str, str]:
    core = chunk.strip()
    if not core:
        return chunk, "", ""
    start = chunk.index(core)
    return chunk[:start], core, chunk[start + len(core):]


# --- HTTP с отменой ----------------------------------------------------------------

class _Http:
    """Запросы к локальному серверу; abort() из любого потока закрывает текущий сокет."""

    def __init__(self, label: str):
        self.label = label  # «Ollama» или «движок нейросети» – для сообщений об ошибках
        self._lock = threading.Lock()
        self._active: http.client.HTTPConnection | None = None

    def abort(self) -> None:
        with self._lock:
            conn = self._active
        sock = getattr(conn, "sock", None) if conn else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _connect(self, hosts: list[str], port: int, https: bool, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        for host in hosts:
            # Windows ждёт ~2 с, прежде чем сообщить об отказе в соединении. Локальный
            # сервер принимает соединение мгновенно, поэтому на connect хватает секунды.
            conn = cls(host, port, timeout=_CONNECT_TIMEOUT)
            try:
                conn.connect()
            except OSError:  # отказ, таймаут, неизвестный адрес, сеть недоступна: пробуем следующий
                conn.close()
                continue
            conn.sock.settimeout(timeout)
            return conn
        raise _Refused(f"{self.label.capitalize()} не отвечает. Текст не изменён.")

    def request(self, hosts: list[str], port: int, method: str, path: str, payload: dict | None,
                timeout: float, cancel: threading.Event | None = None, https: bool = False) -> tuple[int, dict]:
        def cancelled() -> bool:
            return cancel is not None and cancel.is_set()

        if cancelled():
            raise Cancelled
        conn = self._connect(hosts, port, https, timeout)
        with self._lock:
            self._active = conn
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
            if cancelled():  # отмена могла прийти до появления сокета
                raise Cancelled
            response = conn.getresponse()
            raw = response.read()
        except Cancelled:
            raise
        except TimeoutError:
            if cancelled():
                raise Cancelled from None
            raise LlmError(f"Модель не ответила за {timeout:g} с. Текст не изменён.") from None
        except (OSError, http.client.HTTPException) as exc:
            if cancelled():
                raise Cancelled from None
            log.warning("Сбой соединения (%s): %s", self.label, type(exc).__name__)
            raise BackendUnavailable(f"Соединение с {self.label} прервано ({type(exc).__name__}). "
                                     "Текст не изменён.") from None
        finally:
            with self._lock:
                self._active = None
            conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return response.status, data if isinstance(data, dict) else {}

    def stream(self, hosts: list[str], port: int, path: str, payload: dict, cancel: threading.Event,
               https: bool = False) -> Iterator[dict]:
        """POST с потоковым ответом (по JSON-объекту на строку)."""
        conn = self._connect(hosts, port, https, timeout=120)
        with self._lock:
            self._active = conn
        try:
            conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise LlmError(f"{self.label.capitalize()} ответила кодом {response.status}.")
            while line := response.readline():
                if cancel.is_set():
                    raise Cancelled
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
        except (Cancelled, LlmError):
            raise
        except (OSError, http.client.HTTPException) as exc:
            if cancel.is_set():
                raise Cancelled from None
            raise LlmError(f"Загрузка прервалась ({type(exc).__name__}). "
                           "Повторите, и она продолжится с того же места.") from None
        finally:
            with self._lock:
                self._active = None
            conn.close()


# --- Общая часть клиентов ------------------------------------------------------------

class LlmClient:
    """Общий интерфейс: check, preload, chat, abort, close и обработка текста частями."""

    backend = ""

    def __init__(self, settings: ModelSettings):
        self.settings = settings

    @property
    def model(self) -> str:
        return self.settings.name

    def check(self) -> Health:
        raise NotImplementedError

    def preload(self) -> None:
        raise NotImplementedError

    def chat(self, messages: list[dict], temperature: float, num_predict: int,
             cancel: threading.Event | None = None) -> str:
        raise NotImplementedError

    def abort(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Освобождает ресурсы (останавливает встроенный движок)."""

    def process(self, text: str, mode: Mode, common_rules: str, max_chars: int,
                cancel: threading.Event, on_progress: Callable[[int, int], None] | None = None,
                protect_quotes: bool = True) -> str:
        """Обрабатывает текст целиком (частями, если он длиннее max_chars)."""
        text = text.replace("\r\n", "\n")
        chunks = split_text(text, max_chars)
        temperature = mode.temperature if mode.temperature is not None else self.settings.temperature
        log.info("Обработка: режим=%s, символов=%d, частей=%d", mode.id, len(text), len(chunks))
        results: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            if cancel.is_set():
                raise Cancelled
            if on_progress and len(chunks) > 1:
                on_progress(index, len(chunks))
            lead, core, trail = _split_whitespace(chunk)
            if not any(ch.isalpha() for ch in core):
                results.append(chunk)  # нечего исправлять: числа, символы, пробелы
                continue
            started = time.monotonic()
            cleaned = self._edit(core, mode, common_rules, temperature, cancel, protect_quotes)
            log.info("Часть %d/%d: %d → %d символов за %.1f с",
                     index, len(chunks), len(core), len(cleaned), time.monotonic() - started)
            results.append(lead + cleaned + trail)
        return "".join(results)

    def _edit(self, core: str, mode: Mode, common_rules: str, temperature: float, cancel: threading.Event,
              protect_quotes: bool) -> str:
        """Одна часть текста. Фрагменты в кавычках модель видит только как метки [[1]] (см. quotes.py)."""
        sent, fragments = quotes.protect(core) if protect_quotes else (core, [])
        rules = f"{common_rules.strip()}\n{quotes.RULE}" if fragments else common_rules
        may_drop = mode.id in CONDENSING_MODES  # при сокращении предложение с меткой могли убрать целиком
        problem = ""
        for attempt in (1, 2):  # второй раз – с температурой 0: так модель отвечает строже
            answer = self.chat(build_messages(rules, mode, sent), temperature if attempt == 1 else 0.0,
                               num_predict=len(core) + 256, cancel=cancel)
            cleaned = clean_output(sent, answer)
            if not cleaned:
                raise LlmError("Модель вернула пустой ответ. Текст не изменён.")
            restored, missing, unknown = quotes.restore(cleaned, fragments) if fragments else (cleaned, [], [])
            if unknown or (missing and not may_drop):
                problem = ("Модель потеряла фрагмент в кавычках, поэтому текст не изменён. Попробуйте ещё раз "
                           "или выделите текст поменьше.")
                log.warning("Попытка %d: модель потеряла метки кавычек (пропало %d, лишних %d)",
                            attempt, len(missing), len(unknown))
            elif _CJK.search(restored) and not _CJK.search(core):
                # Небольшие модели иногда посреди фразы переходят на китайский.
                problem = "Модель начала отвечать на другом языке, поэтому текст не изменён. Попробуйте ещё раз."
                log.warning("Попытка %d: в ответе появились иероглифы, которых не было в тексте", attempt)
            else:
                return restored
        raise LlmError(problem)


# --- Встроенный движок ---------------------------------------------------------------

class BuiltinClient(LlmClient):
    backend = "builtin"

    def __init__(self, settings: ModelSettings):
        super().__init__(settings)
        self._http = _Http("движком нейросети")
        self._server: engine.LlamaServer | None = None
        self._server_lock = threading.Lock()

    def check(self) -> Health:
        if not engine.is_installed():
            return Health(False, "Движок не установлен",
                          "Движок нейросети не установлен. Запустите установщик Gloss ещё раз.")
        if model_files.model_path(self.model) is None:
            if model_files.is_custom(self.model):
                return Health(False, "Файл модели не найден", f"Файл модели не найден: {self.model}.")
            return Health(False, "Модель не скачана", f"Модель {self.model} не скачана. {DOWNLOAD_HINT}", True)
        return Health(True, f"Готов · {self.model}", f"Модель {self.model} готова к работе.")

    def _server_for_model(self) -> engine.LlamaServer:
        path = model_files.model_path(self.model)
        if path is None:
            raise ModelMissing(f"Модель {self.model} не скачана. {DOWNLOAD_HINT}")
        if not engine.is_installed():
            raise BackendUnavailable("Движок нейросети не установлен. Запустите установщик Gloss ещё раз.")
        with self._server_lock:
            if self._server is None or self._server.model_path != path:
                if self._server is not None:
                    self._server.close()
                s = self.settings
                self._server = engine.LlamaServer(path, s.context, s.use_gpu, s.unload_after_min)
            return self._server

    def _running_server(self, cancel: threading.Event | None) -> tuple[engine.LlamaServer, int]:
        server = self._server_for_model()
        try:
            return server, server.ensure_running(cancel, timeout=max(self.settings.timeout_sec, 180))
        except engine.EngineError as exc:
            raise BackendUnavailable(str(exc)) from None
        except engine.Aborted:
            raise Cancelled from None

    def preload(self) -> None:
        started = time.monotonic()
        try:
            self._running_server(None)
            log.info("Предзагрузка модели: %.1f с", time.monotonic() - started)
        except LlmError as exc:
            log.warning("Предзагрузка модели не удалась: %s", exc)

    def chat(self, messages: list[dict], temperature: float, num_predict: int,
             cancel: threading.Event | None = None) -> str:
        payload = {"messages": messages, "temperature": temperature, "max_tokens": num_predict, "stream": False}
        for attempt in (1, 2):
            server, port = self._running_server(cancel)
            try:
                with server.in_use():
                    status, data = self._http.request(["127.0.0.1"], port, "POST", "/v1/chat/completions",
                                                      payload, timeout=self.settings.timeout_sec, cancel=cancel)
                break
            except _Refused:
                if attempt == 2:  # сервер упал между проверкой и запросом – перезапускаем один раз
                    raise
        if status != 200:
            error = data.get("error")
            text = error.get("message") if isinstance(error, dict) else error
            raise LlmError(f"Движок вернул ошибку {status}: {str(text or 'без описания')[:150]}. Текст не изменён.")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str):
            raise LlmError("Движок вернул ответ неожиданного формата. Текст не изменён.")
        return content

    def abort(self) -> None:
        self._http.abort()

    def close(self) -> None:
        with self._server_lock:
            if self._server is not None:
                self._server.close()
                self._server = None


# --- Ollama ---------------------------------------------------------------------------

class OllamaClient(LlmClient):
    backend = "ollama"

    def __init__(self, settings: ModelSettings, ollama: OllamaSettings):
        super().__init__(settings)
        self.ollama = ollama
        parts = urlsplit(ollama.url)
        self._https = parts.scheme == "https"
        # «localhost» в Windows сначала резолвится в ::1, а Ollama по умолчанию
        # слушает только 127.0.0.1: каждый запрос терял бы ~2 с на отказ по IPv6.
        hostname = parts.hostname or "localhost"
        self._hosts = ["127.0.0.1", "::1"] if hostname.lower() == "localhost" else [hostname]
        self._port = parts.port or (443 if self._https else 11434)
        self._base = parts.path.rstrip("/")
        self._http = _Http("Ollama")

    @property
    def _keep_alive(self) -> str | int:
        minutes = self.settings.unload_after_min
        return -1 if minutes <= 0 else f"{max(1, round(minutes))}m"  # -1: Ollama держит модель всегда

    def _request(self, method: str, path: str, payload: dict | None, timeout: float,
                 cancel: threading.Event | None = None) -> tuple[int, dict]:
        return self._http.request(self._hosts, self._port, method, self._base + path, payload, timeout,
                                  cancel, https=self._https)

    def check(self) -> Health:
        model = self.model
        try:
            status, data = self._request("GET", "/api/tags", None, timeout=3)
        except LlmError:
            return Health(False, "Ollama недоступна", f"Ollama не отвечает по адресу {self.ollama.url}. "
                                                      "Запустите Ollama и выберите «Настройки → Проверить модель».")
        if status != 200:
            return Health(False, "Ollama недоступна", f"Ollama ответила кодом {status}.")
        names = set()
        for item in data.get("models") or []:
            if isinstance(item, dict):
                names.update(n for n in (item.get("name"), item.get("model")) if isinstance(n, str))
        if model not in names and not (":" not in model and f"{model}:latest" in names):
            return Health(False, "Модель не скачана", f"Модель {model} не скачана в Ollama. {DOWNLOAD_HINT}", True)
        return Health(True, f"Готов · {model}", f"Ollama работает, модель {model} доступна.")

    def local_models(self) -> dict[str, str]:
        """{имя модели: digest манифеста} для скачанных моделей."""
        status, data = self._request("GET", "/api/tags", None, timeout=5)
        if status != 200:
            raise LlmError(f"Ollama ответила кодом {status}.")
        return {item["name"]: str(item.get("digest") or "") for item in data.get("models") or []
                if isinstance(item, dict) and isinstance(item.get("name"), str)}

    def delete_model(self, model: str) -> bool:
        status, _ = self._request("DELETE", "/api/delete", {"model": model}, timeout=30)
        return status == 200

    def pull(self, model: str, on_event: Callable[[dict], None], cancel: threading.Event) -> None:
        """Скачивает (докачивает, обновляет) модель, передавая события прогресса Ollama."""
        for event in self._http.stream(self._hosts, self._port, self._base + "/api/pull",
                                       {"model": model, "stream": True}, cancel, https=self._https):
            if event.get("error"):
                raise LlmError(f"Ошибка загрузки модели: {event['error']}")
            on_event(event)
            if event.get("status") == "success":
                return
        raise LlmError("Загрузка модели оборвалась. Повторите, и она продолжится с того же места.")

    def preload(self) -> None:
        started = time.monotonic()
        try:
            status, _ = self._request("POST", "/api/chat", {"model": self.model, "messages": [],
                                                            "keep_alive": self._keep_alive},
                                      timeout=self.settings.timeout_sec)
            log.info("Предзагрузка модели: код %s за %.1f с", status, time.monotonic() - started)
        except LlmError as exc:
            log.warning("Предзагрузка модели не удалась: %s", exc)

    def chat(self, messages: list[dict], temperature: float, num_predict: int,
             cancel: threading.Event | None = None) -> str:
        s = self.settings
        payload = {"model": s.name, "messages": messages, "stream": False, "keep_alive": self._keep_alive,
                   "options": {"temperature": temperature, "num_ctx": s.context, "num_predict": num_predict}}
        status, data = self._request("POST", "/api/chat", payload, timeout=s.timeout_sec, cancel=cancel)
        error = str(data.get("error") or "")
        if status == 404 or "not found" in error.lower():
            raise ModelMissing(f"Модель {s.name} не скачана в Ollama. {DOWNLOAD_HINT}")
        if status != 200:
            raise LlmError(f"Ollama вернула ошибку {status}: {error[:150] or 'без описания'}. Текст не изменён.")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LlmError("Ollama вернула ответ неожиданного формата. Текст не изменён.")
        return content

    def abort(self) -> None:
        self._http.abort()


def make_client(cfg: Config) -> LlmClient:
    if cfg.backend == "ollama":
        return OllamaClient(cfg.model, cfg.ollama)
    return BuiltinClient(cfg.model)
