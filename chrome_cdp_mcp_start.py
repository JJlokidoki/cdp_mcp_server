#!/usr/bin/env python3
"""
mcp_launcher.py — интерактивный лаунчер Chrome (CDP) + MCP (supergateway -> chrome-devtools-mcp).

Запускает и контролирует два процесса:

  1) google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-mcp
  2) npx -y supergateway --stdio "npx chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222" --port 8931

Возможности: интерактивная конфигурация, start/stop/restart, логирование
(консоль + файл с ротацией), перехват вывода дочерних процессов, корректная
остановка всего дерева процессов (SIGTERM -> SIGKILL / taskkill на Windows)
при выходе, Ctrl+C, SIGTERM и SIGHUP.

Настройки, изменённые командами set/reset, автоматически сохраняются в
конфиг-файл (по умолчанию mcp_launcher.json рядом со скриптом) и
загружаются при следующем запуске.

chrome_lazy (по умолчанию on): Chrome не держится запущенным постоянно, а
стартует при первом вызове инструмента MCP и закрывается после
chrome_idle_timeout секунд простоя — чтобы не занимать RAM открытым Chrome.

Запуск:  python3 mcp_launcher.py [--config FILE] [--log-file FILE] [--autostart] [-s key=value ...]
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

IS_WIN = os.name == "nt"
LOCAL = "127.0.0.1"
__version__ = "1.1.0"

log = logging.getLogger("launcher")


class LauncherError(Exception):
    """Ожидаемая ошибка с понятным сообщением для пользователя."""


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # --- Chrome ---
    chrome_path: str = ""              # пусто = автопоиск
    headless: bool = False
    user_data_dir: str = str(Path(tempfile.gettempdir()) / "chrome-mcp")
    cdp_port: int = 9222
    window_size: str = ""              # "1280,800"
    no_sandbox: bool = False           # нужно в Docker / под root
    disable_gpu: bool = False
    start_url: str = ""
    chrome_extra_flags: str = ""       # произвольные флаги Chrome
    chrome_lazy: bool = True           # запускать Chrome при первом вызове
                                       # инструмента MCP и закрывать после простоя
    chrome_idle_timeout: int = 180     # сек без вызовов инструментов до закрытия
                                       # Chrome (0 = не закрывать автоматически)
    # --- MCP ---
    npx_path: str = ""                 # пусто = автопоиск
    mcp_package: str = "chrome-devtools-mcp@latest"
    mcp_port: int = 8931
    mcp_gateway: str = "builtin"       # builtin | supergateway
    mcp_transport: str = "sse"         # sse | streamableHttp | ws (для supergateway)
    mcp_stateful: bool = True          # streamableHttp: один процесс MCP на сессию,
                                       # а не новый npx на каждый POST (stateless
                                       # отвечает ~2с и клиенты рвут соединение)
    mcp_log_level: str = "info"        # debug логирует содержимое запросов
    mcp_cors: bool = False
    mcp_health_endpoint: str = ""      # например /healthz
    mcp_token: str = ""                # Bearer-токен (--oauth2Bearer)
    mcp_extra_flags: str = ""          # доп. флаги chrome-devtools-mcp
    gateway_extra_flags: str = ""      # доп. флаги supergateway
    # --- Общее ---
    start_timeout: int = 40
    stop_timeout: int = 8
    show_chrome_output: bool = False   # печатать вывод Chrome в консоль (в файл пишется всегда)
    show_mcp_output: bool = True


DESCR = {
    "chrome_path": "путь к Chrome (пусто = автопоиск)",
    "headless": "запуск Chrome без окна (--headless=new)",
    "user_data_dir": "профиль Chrome (--user-data-dir; обязателен для CDP в Chrome 136+)",
    "cdp_port": "порт CDP (--remote-debugging-port)",
    "window_size": "размер окна, формат 1280,800",
    "no_sandbox": "добавить --no-sandbox (Docker/root)",
    "disable_gpu": "добавить --disable-gpu",
    "start_url": "стартовая страница",
    "chrome_extra_flags": "произвольные флаги Chrome",
    "chrome_lazy": "Chrome по требованию: старт при первом tools/call, авто-закрытие после простоя",
    "chrome_idle_timeout": "сек без вызовов инструментов до авто-закрытия Chrome (0 = не закрывать)",
    "npx_path": "путь к npx (пусто = автопоиск)",
    "mcp_package": "npm-пакет MCP-сервера (берётся из кэша npx); путь/команда — запуск напрямую",
    "mcp_port": "порт, на котором слушает MCP (builtin-мост или supergateway)",
    "mcp_gateway": "builtin = встроенный HTTP-мост (один живой процесс MCP, мгновенный коннект); supergateway = через npx",
    "mcp_transport": "транспорт для клиентов (supergateway): sse | streamableHttp | ws",
    "mcp_stateful": "streamableHttp: --stateful (живой процесс MCP на сессию; off = stateless, новый npx на каждый POST)",
    "mcp_log_level": "уровень логов supergateway: debug | info | none",
    "mcp_cors": "включить CORS (--cors)",
    "mcp_health_endpoint": "health-эндпоинт, например /healthz",
    "mcp_token": "Bearer-токен для доступа к MCP (--oauth2Bearer)",
    "mcp_extra_flags": "доп. флаги для chrome-devtools-mcp",
    "gateway_extra_flags": "доп. флаги для supergateway",
    "start_timeout": "таймаут ожидания готовности, сек",
    "stop_timeout": "таймаут мягкой остановки до kill, сек",
    "show_chrome_output": "показывать вывод Chrome в консоли",
    "show_mcp_output": "показывать вывод MCP/supergateway в консоли",
}

CHOICES = {
    "mcp_gateway": ("builtin", "supergateway"),
    "mcp_transport": ("sse", "streamableHttp", "ws"),
    "mcp_log_level": ("debug", "info", "none"),
}

DEFAULTS = Config()


def parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "y", "on", "да"):
        return True
    if v in ("0", "false", "no", "n", "off", "нет"):
        return False
    raise LauncherError(f"Не удалось разобрать булево значение: {raw!r} (используйте on/off)")


def set_value(cfg: Config, key: str, raw: str) -> None:
    if key not in {f.name for f in fields(cfg)}:
        raise LauncherError(f"Неизвестный параметр: {key}. Список — команда 'show'.")
    cur = getattr(cfg, key)
    if isinstance(cur, bool):
        val = parse_bool(raw)
    elif isinstance(cur, int):
        try:
            val = int(raw)
        except ValueError:
            raise LauncherError(f"{key}: ожидается целое число, получено {raw!r}")
        if key.endswith("_port") and not 1 <= val <= 65535:
            raise LauncherError(f"{key}: порт должен быть в диапазоне 1..65535")
        if key.endswith("_timeout"):
            min_val = 0 if key == "chrome_idle_timeout" else 1  # 0 = отключить авто-закрытие
            if val < min_val:
                raise LauncherError(f"{key}: значение должно быть >= {min_val}")
    else:
        val = raw
    if key in CHOICES and val not in CHOICES[key]:
        raise LauncherError(f"{key}: допустимо {' | '.join(CHOICES[key])}")
    if key == "window_size" and val and not re.fullmatch(r"\d+,\d+", val):
        raise LauncherError("window_size: формат 1280,800")
    if key == "mcp_health_endpoint" and val and not val.startswith("/"):
        raise LauncherError("mcp_health_endpoint должен начинаться с '/'")
    if key == "user_data_dir" and val:
        val = str(Path(val).expanduser())
    for flag in ("chrome_extra_flags", "mcp_extra_flags", "gateway_extra_flags"):
        if key == flag and val:
            try:
                shlex.split(val)
            except ValueError as e:
                raise LauncherError(f"{key}: ошибка разбора флагов: {e}")

    setattr(cfg, key, val)
    if cfg.cdp_port == cfg.mcp_port:
        setattr(cfg, key, cur)
        raise LauncherError("cdp_port и mcp_port не должны совпадать")


def load_config(cfg: Config, path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LauncherError(f"Файл не найден: {path}")
    except (OSError, json.JSONDecodeError) as e:
        raise LauncherError(f"Не удалось прочитать конфиг {path}: {e}")
    if not isinstance(data, dict):
        raise LauncherError("Конфиг должен быть JSON-объектом")
    for k, v in data.items():
        try:
            set_value(cfg, k, str(v) if not isinstance(v, bool) else ("on" if v else "off"))
        except LauncherError as e:
            log.warning("Конфиг %s: пропущен параметр %s: %s", path, k, e)
    log.info("Конфиг загружен: %s", path)


def save_config(cfg: Config, path: Path) -> None:
    try:
        path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        raise LauncherError(f"Не удалось сохранить конфиг {path}: {e}")
    log.info("Конфиг сохранён: %s", path)


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # пакеты не отправляются
        return s.getsockname()[0]
    except OSError:
        return LOCAL
    finally:
        s.close()


def http_get_json(url: str, timeout: float = 1.5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def find_chrome(cfg: Config) -> str:
    if cfg.chrome_path:
        p = shutil.which(cfg.chrome_path) or (cfg.chrome_path if Path(cfg.chrome_path).exists() else None)
        if not p:
            raise LauncherError(f"Chrome не найден по пути: {cfg.chrome_path}")
        return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    extra = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in extra:
        if Path(p).exists():
            return p
    raise LauncherError("Chrome не найден. Укажите путь: set chrome_path /path/to/chrome")


def find_npx(cfg: Config) -> str:
    if cfg.npx_path:
        p = shutil.which(cfg.npx_path) or (cfg.npx_path if Path(cfg.npx_path).exists() else None)
        if not p:
            raise LauncherError(f"npx не найден по пути: {cfg.npx_path}")
        return p
    p = shutil.which("npx")
    if not p:
        raise LauncherError("npx не найден. Установите Node.js или укажите: set npx_path /path/to/npx")
    return p


def resolve_npx_cache_binary(spec: str) -> Optional[str]:
    """Ищет бинарник npm-пакета в кэше npx (~/.npm/_npx).

    Запуск бинарника напрямую вместо `npx -y <pkg>` экономит ~1с на старте
    дочернего MCP-процесса — критично для stateless/stateful streamableHttp,
    где клиент ждёт ответ на initialize дольше этого времени.
    Возвращает путь к самому свежему найденному бинарнику или None.
    """
    if IS_WIN or not spec or any(ch.isspace() for ch in spec):
        return None
    name = spec[1:].split("@", 1)[0] if spec.startswith("@") else spec.split("@", 1)[0]
    if not name or "/" in name or "@" in name:
        return None
    best_mtime, best = 0.0, ""
    for bin_path in (Path.home() / ".npm" / "_npx").glob(f"*/node_modules/.bin/{name}"):
        try:
            mtime = bin_path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime, best = mtime, str(bin_path)
    return best or None


def cmd_to_str(cmd: List[str]) -> str:
    return subprocess.list2cmdline(cmd) if IS_WIN else shlex.join(cmd)


# --------------------------------------------------------------------------- #
# Управляемый процесс
# --------------------------------------------------------------------------- #
class ManagedProcess:
    def __init__(self, name: str, buffer_size: int = 3000):
        self.name = name
        self.proc: Optional[subprocess.Popen] = None
        self.buffer: deque = deque(maxlen=buffer_size)
        self.expected = False  # должен работать (для детектора неожиданных падений)
        self.on_line = None    # опциональный колбэк на каждую строку stdout
        self._reader: Optional[threading.Thread] = None
        self.log = logging.getLogger(name)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def start(self, cmd: List[str], display_cmd: Optional[List[str]] = None,
              stdin_pipe: bool = False) -> None:
        log.info("Запуск %s: %s", self.name, cmd_to_str(display_cmd or cmd))
        kwargs: dict = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # своя группа процессов -> убиваем всё дерево
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self.proc = None
            raise LauncherError(f"Не удалось запустить {self.name}: {e}")
        self.expected = True
        self._reader = threading.Thread(target=self._read_output, args=(self.proc,), daemon=True,
                                        name=f"{self.name}-reader")
        self._reader.start()

    def _read_output(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    if self.on_line is not None:
                        try:
                            self.on_line(line)
                        except Exception:
                            log.exception("%s: ошибка обработчика вывода", self.name)
                    shown = line if len(line) <= 2000 else line[:2000] + f"... (+{len(line)-2000} симв.)"
                    self.buffer.append(shown)
                    self.log.info(shown, extra={"child": True})
        except (ValueError, OSError):
            pass  # поток закрыт при остановке

    def _signal(self, force: bool) -> None:
        p = self.proc
        if p is None:
            return
        try:
            if IS_WIN:
                args = ["taskkill", "/PID", str(p.pid), "/T"] + (["/F"] if force else [])
                subprocess.run(args, capture_output=True, timeout=10)
            else:
                os.killpg(p.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s: ошибка отправки сигнала: %s", self.name, e)

    def stop(self, timeout: int = 8) -> None:
        self.expected = False
        p = self.proc
        if p is None:
            return
        if p.poll() is None:
            log.info("Остановка %s (pid=%s)...", self.name, p.pid)
            self._signal(force=False)
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                log.warning("%s не завершился за %ss — принудительное завершение", self.name, timeout)
                self._signal(force=True)
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    log.error("%s: не удалось убить процесс pid=%s", self.name, p.pid)
        if not IS_WIN:  # добиваем возможных осиротевших потомков в той же группе
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self._reader:
            self._reader.join(timeout=2)
        for stream in (p.stdin, p.stdout):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass
        log.info("%s остановлен (код возврата: %s)", self.name, p.returncode)
        self.proc = None

    def tail(self, n: int) -> List[str]:
        return list(self.buffer)[-n:]


# --------------------------------------------------------------------------- #
# Встроенный HTTP-мост (builtin): один постоянный MCP-процесс на всех клиентов
# --------------------------------------------------------------------------- #
class BuiltinMcpBridge:
    """Streamable HTTP -> stdio для одного долгоживущего MCP-процесса.

    В отличие от supergateway, не запускает новый процесс MCP на каждое
    подключение: initialize прогревается один раз при старте и ответ клиенту
    отдаётся из кэша за миллисекунды. Это критично: запуск chrome-devtools-mcp
    занимает ~1с, а клиенты с коротким таймаутом успевают разорвать соединение.
    """

    WARMUP_PROTOCOL = "2025-11-25"

    def __init__(self, mp: ManagedProcess, launcher: Optional["Launcher"] = None):
        self.mp = mp
        self.launcher = launcher                # для ленивого запуска Chrome
        self.busy = 0                           # запросов в работе (idle-таймер не трогает)
        self.lock = threading.Lock()            # сериализует обмены с MCP-процессом
        self.cond = threading.Condition()
        self.pending: Dict[object, object] = {}  # id запроса -> 'sent' | ответ
        self.init_result: Optional[dict] = None  # ответ MCP-сервера на initialize
        self.sent_initialized = False
        self.httpd: Optional[ThreadingHTTPServer] = None
        mp.on_line = self._on_child_line

    # --- приём от MCP-процесса ---
    def _on_child_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict) or "id" not in msg or \
                ("result" not in msg and "error" not in msg):
            return  # уведомления сервера некому доставить (GET-поток не поддерживаем)
        with self.cond:
            if msg["id"] in self.pending:
                self.pending[msg["id"]] = msg
                self.cond.notify_all()

    # --- отправка в MCP-процесс ---
    def _send(self, msg: dict) -> None:
        p = self.mp.proc
        if not self.mp.running or p is None or p.stdin is None:
            raise LauncherError("MCP-процесс не запущен")
        try:
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
        except (OSError, ValueError) as e:
            raise LauncherError(f"Запись в MCP-процесс не удалась: {e}")

    def _wait_response(self, req_id: object) -> dict:
        while True:
            with self.cond:
                got = self.pending.get(req_id)
                if isinstance(got, dict):
                    del self.pending[req_id]
                    return got
                if not self.mp.running:
                    del self.pending[req_id]
                    return {"jsonrpc": "2.0", "id": req_id, "error": {
                        "code": -32603, "message": "MCP-процесс недоступен (упал)"}}
                self.cond.wait(0.5)

    # --- обработка сообщений клиента (под self.lock) ---
    def ensure_initialized(self) -> dict:
        if self.init_result is not None:
            return self.init_result
        req = {"jsonrpc": "2.0", "id": "__warmup__", "method": "initialize",
               "params": {"protocolVersion": self.WARMUP_PROTOCOL, "capabilities": {},
                          "clientInfo": {"name": "mcp-launcher", "version": "1.0"}}}
        with self.cond:
            self.pending[req["id"]] = "sent"
        self._send(req)
        resp = self._wait_response(req["id"])
        if "error" in resp:
            raise LauncherError(f"initialize MCP-сервера не удался: {resp['error'].get('message')}")
        self.init_result = resp
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.sent_initialized = True
        info = resp.get("result", {}).get("serverInfo", {})
        log.info("MCP-сервер инициализирован (прогрев): %s %s",
                 info.get("name", "?"), info.get("version", "?"))
        return resp

    @staticmethod
    def _error(req_id: object, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def handle_messages(self, body) -> Optional[object]:
        """Обрабатывает POST (объект или batch). Возвращает ответ, None = только уведомления."""
        single = not isinstance(body, list)
        messages = body if isinstance(body, list) else [body]
        out: List[dict] = []
        for m in messages:
            if not isinstance(m, dict) or "method" not in m:
                out.append(self._error(m.get("id") if isinstance(m, dict) else None,
                                       -32600, "Некорректное JSON-RPC сообщение"))
                continue
            if m["method"] == "initialize":
                cached = self.ensure_initialized()
                out.append({"jsonrpc": "2.0", "id": m.get("id"),
                            "result": cached.get("result", {})})
            elif "id" in m:  # запрос -> переслать и дождаться ответа
                if m["method"] == "tools/call" and self.launcher is not None:
                    # ленивый Chrome: первый вызов инструмента поднимает браузер
                    self.launcher.ensure_chrome_for_tools()
                with self.cond:
                    self.pending[m["id"]] = "sent"
                try:
                    self._send(m)
                except LauncherError as e:
                    with self.cond:
                        self.pending.pop(m["id"], None)
                    out.append(self._error(m["id"], -32603, str(e)))
                    continue
                log.info("HTTP → MCP: %s (id=%s)", m["method"], m["id"])
                out.append(self._wait_response(m["id"]))
            else:  # уведомление
                if m["method"] == "notifications/initialized" and self.sent_initialized:
                    continue  # сервер уже инициализирован при прогреве
                if m["method"] == "notifications/initialized":
                    self.sent_initialized = True
                try:
                    self._send(m)
                except LauncherError:
                    pass
        if single:
            return out[0] if out else None
        return out or None

    # --- HTTP-сервер ---
    def start_http(self, port: int) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, payload: Optional[object] = None) -> None:
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length)) if length else None
                except (ValueError, OSError):
                    self._reply(400, {"error": "Некорректный JSON"})
                    return
                if body is None:
                    self._reply(400, {"error": "Пустое тело запроса"})
                    return
                bridge.busy += 1
                try:
                    with bridge.lock:
                        try:
                            resp = bridge.handle_messages(body)
                        except LauncherError as e:
                            self._reply(500, {"error": str(e)})
                            return
                finally:
                    bridge.busy -= 1
                if resp is None:
                    self._reply(202)
                else:
                    self._reply(200, resp)

            def do_GET(self):  # noqa: N802
                self._reply(405, {"error": "GET не поддерживается, используйте POST"})

            def do_DELETE(self):  # noqa: N802
                self._reply(200)  # сессий нет — подтверждаем удаление

            def log_message(self, fmt: str, *args) -> None:
                log.debug("http: " + fmt, *args)

        self.httpd = ThreadingHTTPServer(("", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="mcp-http").start()

    def stop_http(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except OSError:
                pass
            self.httpd = None
        with self.cond:
            self.pending.clear()
            self.cond.notify_all()


# --------------------------------------------------------------------------- #
# Лаунчер
# --------------------------------------------------------------------------- #
class Launcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.chrome = ManagedProcess("chrome")
        self.mcp = ManagedProcess("mcp")
        self.bridge: Optional[BuiltinMcpBridge] = None
        self._last_tool_ts = 0.0            # последнее обращение инструмента к Chrome
        self.lock = threading.RLock()
        self._down = False
        self._stop_evt = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True, name="monitor")
        self._monitor.start()

    # --- сборка команд ---
    def chrome_cmd(self) -> List[str]:
        c = self.cfg
        cmd = [
            find_chrome(c),
            f"--remote-debugging-port={c.cdp_port}",
            f"--user-data-dir={c.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if c.headless:
            cmd.append("--headless=new")
        if c.no_sandbox:
            cmd.append("--no-sandbox")
        if c.disable_gpu:
            cmd.append("--disable-gpu")
        if c.window_size:
            cmd.append(f"--window-size={c.window_size}")
        cmd += shlex.split(c.chrome_extra_flags)
        if c.start_url:
            cmd.append(c.start_url)
        return cmd

    def resolve_mcp_exec(self) -> List[str]:
        """Команда запуска MCP-сервера: путь/команда, бинарник из кэша npx или npx -y.

        Обёртка npx добавляет ~1с к старту дочернего процесса — из-за этого
        клиенты с коротким таймаутом не дожидаются ответа на initialize.
        """
        c = self.cfg
        exe = c.mcp_package.split()[0] if c.mcp_package else ""
        if exe and (shutil.which(exe) or Path(exe).expanduser().exists()):
            return shlex.split(c.mcp_package)
        cached = resolve_npx_cache_binary(c.mcp_package)
        if cached:
            log.info("MCP: бинарник из кэша npx вместо 'npx -y %s': %s", c.mcp_package, cached)
            return [cached]
        return [find_npx(c), "-y", c.mcp_package]

    def builtin_child_cmd(self) -> List[str]:
        cmd = self.resolve_mcp_exec()
        cmd += ["--browserUrl", f"http://{LOCAL}:{self.cfg.cdp_port}"]
        cmd += shlex.split(self.cfg.mcp_extra_flags)
        return cmd

    def mcp_cmd(self) -> List[str]:
        c = self.cfg
        inner = " ".join(shlex.quote(x) for x in self.resolve_mcp_exec())
        inner += f" --browserUrl http://{LOCAL}:{c.cdp_port}"
        if c.mcp_extra_flags:
            inner += " " + c.mcp_extra_flags
        cmd = [find_npx(c), "-y", "supergateway", "--stdio", inner, "--port", str(c.mcp_port)]
        if c.mcp_transport != "sse":
            cmd += ["--outputTransport", c.mcp_transport]
        if c.mcp_transport == "streamableHttp" and c.mcp_stateful:
            cmd.append("--stateful")
        cmd += ["--logLevel", c.mcp_log_level]
        if c.mcp_cors:
            cmd.append("--cors")
        if c.mcp_health_endpoint:
            cmd += ["--healthEndpoint", c.mcp_health_endpoint]
        if c.mcp_token:
            cmd += ["--oauth2Bearer", c.mcp_token]
        cmd += shlex.split(c.gateway_extra_flags)
        return cmd

    def _masked(self, cmd: List[str]) -> List[str]:
        t = self.cfg.mcp_token
        return [a.replace(t, "***") for a in cmd] if t else cmd

    # --- Chrome ---
    def cdp_reachable(self) -> bool:
        try:
            http_get_json(f"http://{LOCAL}:{self.cfg.cdp_port}/json/version")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def start_chrome(self) -> None:
        with self.lock:
            c = self.cfg
            if self.chrome.running:
                log.info("Chrome уже запущен (pid=%s)", self.chrome.pid)
                return
            if port_in_use(LOCAL, c.cdp_port):
                raise LauncherError(f"Порт CDP {c.cdp_port} уже занят. Освободите его или 'set cdp_port N'.")
            try:
                Path(c.user_data_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise LauncherError(f"Не удалось создать user_data_dir {c.user_data_dir}: {e}")
            self.chrome.start(self.chrome_cmd())
            self._last_tool_ts = time.monotonic()  # отсчёт простоя ведём и от старта
            try:
                self._wait_cdp()
            except BaseException:
                self.chrome.stop(c.stop_timeout)
                raise

    def _wait_cdp(self) -> None:
        deadline = time.monotonic() + self.cfg.start_timeout
        while time.monotonic() < deadline:
            if not self.chrome.running:
                rc = self.chrome.proc.returncode if self.chrome.proc else "?"
                last = "\n".join(self.chrome.tail(5))
                raise LauncherError(
                    f"Chrome завершился сразу после старта (код {rc}). Возможно, профиль "
                    f"{self.cfg.user_data_dir} занят другим Chrome — смените user_data_dir.\n{last}")
            try:
                info = http_get_json(f"http://{LOCAL}:{self.cfg.cdp_port}/json/version")
                log.info("CDP готов: %s (порт %s)", info.get("Browser", "?"), self.cfg.cdp_port)
                return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.3)
        raise LauncherError(f"CDP не ответил за {self.cfg.start_timeout} сек")

    # --- MCP ---
    def start_mcp(self) -> None:
        with self.lock:
            c = self.cfg
            if self.mcp.running:
                log.info("MCP уже запущен (pid=%s)", self.mcp.pid)
                return
            if c.chrome_lazy and c.mcp_gateway != "builtin":
                log.warning("chrome_lazy работает только с builtin-шлюзом: в supergateway-режиме "
                            "Chrome нужно запускать вручную 'start chrome'")
            if not c.chrome_lazy and not self.chrome.running and not self.cdp_reachable():
                raise LauncherError("CDP недоступен. Сначала запустите Chrome: 'start chrome'.")
            if port_in_use(LOCAL, c.mcp_port):
                raise LauncherError(f"Порт MCP {c.mcp_port} уже занят. Освободите его или 'set mcp_port N'.")
            if c.mcp_gateway == "builtin":
                self._start_builtin_mcp()
            else:
                self._start_supergateway_mcp()

    def _start_builtin_mcp(self) -> None:
        c = self.cfg
        # builtin всегда обслуживает streamableHttp (/mcp) независимо от mcp_transport
        for key in ("mcp_token", "mcp_cors", "mcp_health_endpoint", "gateway_extra_flags"):
            if getattr(c, key):
                log.warning("builtin-режим не поддерживает %s (опция действует только в supergateway)", key)
        self.mcp.start(self.builtin_child_cmd(), stdin_pipe=True)
        try:
            self.bridge = BuiltinMcpBridge(self.mcp, launcher=self)
            self.bridge.start_http(c.mcp_port)
            with self.bridge.lock:
                self.bridge.ensure_initialized()  # прогрев: клиент получит ответ мгновенно
            log.info("MCP готов (builtin). Адрес для harness: %s", self.endpoint())
        except BaseException:
            if self.bridge:
                self.bridge.stop_http()
                self.bridge = None
            self.mcp.stop(c.stop_timeout)
            raise

    def _start_supergateway_mcp(self) -> None:
        c = self.cfg
        cmd = self.mcp_cmd()
        self.mcp.start(cmd, self._masked(cmd))
        try:
            self._wait_mcp()
        except BaseException:
            self.mcp.stop(c.stop_timeout)
            raise

    def _wait_mcp(self) -> None:
        deadline = time.monotonic() + self.cfg.start_timeout
        while time.monotonic() < deadline:
            if not self.mcp.running:
                rc = self.mcp.proc.returncode if self.mcp.proc else "?"
                last = "\n".join(self.mcp.tail(8))
                raise LauncherError(f"MCP завершился сразу после старта (код {rc}).\n{last}")
            if port_in_use(LOCAL, self.cfg.mcp_port):
                log.info("MCP готов. Адрес для harness: %s", self.endpoint())
                return
            time.sleep(0.4)
        raise LauncherError(
            f"MCP не начал слушать порт за {self.cfg.start_timeout} сек "
            f"(при первом запуске npx качает пакеты — увеличьте 'set start_timeout 90')")

    def endpoint(self) -> str:
        c = self.cfg
        ip = local_ip()
        if c.mcp_gateway != "builtin" and c.mcp_transport == "ws":
            return f"ws://{ip}:{c.mcp_port}"
        path = "/sse" if (c.mcp_gateway != "builtin" and c.mcp_transport == "sse") else "/mcp"
        return f"http://{ip}:{c.mcp_port}{path}"

    # --- Общее ---
    def ensure_chrome_for_tools(self) -> None:
        """Ленивый запуск Chrome: вызывается мостом перед tools/call."""
        with self.lock:
            self._last_tool_ts = time.monotonic()
            if self.chrome.running:
                return
            log.info("chrome_lazy: запускаю Chrome по запросу инструмента...")
            self.start_chrome()  # ждёт готовности CDP; RLock реентерабелен

    def start(self, target: str = "all") -> None:
        c = self.cfg
        if target in ("all", "chrome"):
            if target == "all" and c.chrome_lazy and c.mcp_gateway == "builtin":
                log.info("chrome_lazy: Chrome запустится при первом вызове инструмента "
                         "(авто-закрытие через %sс простоя)", c.chrome_idle_timeout or "выкл")
            else:
                self.start_chrome()
        if target in ("all", "mcp"):
            self.start_mcp()

    def stop(self, target: str = "all") -> None:
        with self.lock:
            if target in ("all", "mcp"):
                if self.bridge is not None:
                    self.bridge.stop_http()
                    self.bridge = None
                self._safe_stop(self.mcp)
            if target in ("all", "chrome"):
                self._safe_stop(self.chrome)

    def _safe_stop(self, mp: ManagedProcess) -> None:
        try:
            mp.stop(self.cfg.stop_timeout)
        except Exception:
            log.exception("Ошибка при остановке %s", mp.name)

    def restart(self, target: str = "all") -> None:
        self.stop(target)
        self.start(target)

    def shutdown(self) -> None:
        if self._down:
            return
        self._down = True
        self._stop_evt.set()
        log.info("Завершение работы: останавливаю MCP и Chrome...")
        self.stop("all")
        log.info("Всё остановлено.")

    def _monitor_loop(self) -> None:
        while not self._stop_evt.wait(1.0):
            for mp in (self.chrome, self.mcp):
                p = mp.proc
                if mp.expected and p is not None and p.poll() is not None:
                    mp.expected = False
                    log.error("%s неожиданно завершился (код %s). Последние строки вывода:\n%s",
                              mp.name, p.returncode, "\n".join(mp.tail(8)) or "(пусто)")
                    if mp is self.chrome and self.mcp.running:
                        log.warning("Chrome упал, а MCP ещё работает — перезапустите: 'restart'")
            # chrome_lazy: закрываем Chrome, если инструменты давно не обращались
            c = self.cfg
            if (c.chrome_lazy and c.chrome_idle_timeout > 0
                    and self.chrome.running and self._last_tool_ts > 0
                    and (self.bridge is None or self.bridge.busy == 0)
                    and time.monotonic() - self._last_tool_ts > c.chrome_idle_timeout):
                log.info("chrome_lazy: Chrome простаивал %sс — останавливаю для экономии RAM",
                         c.chrome_idle_timeout)
                self._safe_stop(self.chrome)

    def status(self) -> str:
        c = self.cfg

        def line(mp: ManagedProcess, port: int) -> str:
            if mp.running:
                return f"{mp.name:7} RUNNING pid={mp.pid} port={port}"
            return f"{mp.name:7} stopped"

        out = [line(self.chrome, c.cdp_port), line(self.mcp, c.mcp_port)]
        if self.mcp.running:
            out.append(f"endpoint: {self.endpoint()}")
        if (self.chrome.running and c.chrome_lazy and c.chrome_idle_timeout > 0
                and self._last_tool_ts > 0):
            left = max(0, int(c.chrome_idle_timeout - (time.monotonic() - self._last_tool_ts)))
            out.append(f"chrome_lazy: Chrome закроется через {left}с без вызовов инструментов")
        return "\n".join(out)

    def check(self) -> None:
        c = self.cfg
        try:
            info = http_get_json(f"http://{LOCAL}:{c.cdp_port}/json/version")
            log.info("CDP OK: %s | %s", info.get("Browser"), info.get("webSocketDebuggerUrl"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if c.chrome_lazy:
                log.info("CDP не отвечает (норма для chrome_lazy — Chrome запустится по требованию): %s", e)
            else:
                log.error("CDP недоступен на порту %s: %s", c.cdp_port, e)
        if port_in_use(LOCAL, c.mcp_port):
            log.info("MCP: порт %s слушает", c.mcp_port)
            if c.mcp_health_endpoint:
                try:
                    with urllib.request.urlopen(f"http://{LOCAL}:{c.mcp_port}{c.mcp_health_endpoint}", timeout=2) as r:
                        log.info("MCP health %s -> HTTP %s", c.mcp_health_endpoint, r.status)
                except (urllib.error.URLError, OSError) as e:
                    log.error("MCP health не отвечает: %s", e)
        else:
            log.error("MCP: порт %s не слушает", c.mcp_port)


# --------------------------------------------------------------------------- #
# Логирование
# --------------------------------------------------------------------------- #
class ConsoleFilter(logging.Filter):
    """Скрывает вывод дочерних процессов в консоли, если он отключён в конфиге."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "child", False):
            if record.name == "chrome":
                return self.cfg.show_chrome_output
            if record.name == "mcp":
                return self.cfg.show_mcp_output
        return True


def setup_logging(cfg: Config, log_file: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(ConsoleFilter(cfg))
    root.addHandler(console)
    try:
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
        root.addHandler(fh)
    except OSError as e:
        print(f"Предупреждение: лог-файл {log_file} недоступен: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Интерактивная оболочка
# --------------------------------------------------------------------------- #
HELP = """\
Команды:
  show                      текущая конфигурация (* = отличается от значения по умолчанию)
  set <параметр> <значение> изменить параметр (действует со следующего старта;
                            сохраняется в конфиг-файл автоматически)
  reset [параметр]          вернуть значение по умолчанию (или все параметры)
  save [файл] / load [файл] сохранить / загрузить конфиг (JSON)
  start  [all|chrome|mcp]   запустить (по умолчанию all: Chrome, затем MCP)
  stop   [all|chrome|mcp]   остановить (all: сначала MCP, потом Chrome)
  restart [all|chrome|mcp]  перезапуск
  status                    состояние процессов и адрес MCP
  check                     проверить CDP и MCP (HTTP/порт)
  logs [chrome|mcp] [N]     последние N строк вывода процесса (по умолчанию mcp, 30)
  url                       адрес MCP для harness
  help                      эта справка
  quit | exit               остановить всё и выйти
Примеры:
  set headless on
  set user_data_dir /tmp/chrome-profile2
  set mcp_log_level debug      # подробное логирование MCP-запросов
  set mcp_gateway supergateway # вместо встроенного HTTP-моста (нужен sse/ws/токен)
  set chrome_idle_timeout 60   # закрыть Chrome после 60с без вызовов инструментов
  set chrome_lazy off          # Chrome по 'start'/'stop' без авто-управления
"""


def show_config(cfg: Config) -> None:
    width = max(len(f.name) for f in fields(cfg))
    for f in fields(cfg):
        val = getattr(cfg, f.name)
        mark = "*" if val != getattr(DEFAULTS, f.name) else " "
        shown = ("***" if (f.name == "mcp_token" and val) else val)
        if isinstance(shown, bool):
            shown = "on" if shown else "off"
        print(f"{mark} {f.name:<{width}} = {shown!s:<28} # {DESCR[f.name]}")


def check_target(arg: Optional[str]) -> str:
    t = (arg or "all").lower()
    if t not in ("all", "chrome", "mcp"):
        raise LauncherError("Цель должна быть: all | chrome | mcp")
    return t


def autosave(cfg: Config, cfg_path: Path) -> None:
    """Сохраняет конфиг после интерактивных изменений, чтобы он подхватился при следующем запуске."""
    try:
        save_config(cfg, cfg_path)
    except LauncherError as e:
        log.error("Автосохранение конфига не удалось: %s", e)


def handle(line: str, launcher: Launcher, cfg_path: Path) -> bool:
    """Возвращает False для выхода."""
    parts = line.strip().split(None, 2)
    if not parts:
        return True
    cmd = parts[0].lower()
    arg1 = parts[1] if len(parts) > 1 else None

    if cmd in ("quit", "exit", "q"):
        return False
    if cmd in ("help", "?", "h"):
        print(HELP)
    elif cmd == "show":
        show_config(launcher.cfg)
    elif cmd == "set":
        if len(parts) < 3:
            raise LauncherError("Использование: set <параметр> <значение>")
        value = parts[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        set_value(launcher.cfg, parts[1], value)
        shown = "***" if parts[1] == "mcp_token" else getattr(launcher.cfg, parts[1])
        log.info("%s = %s", parts[1], shown)
        autosave(launcher.cfg, cfg_path)
        if launcher.chrome.running or launcher.mcp.running:
            log.info("Процессы уже запущены — изменение применится после 'restart'.")
    elif cmd == "reset":
        if arg1:
            if arg1 not in {f.name for f in fields(Config)}:
                raise LauncherError(f"Неизвестный параметр: {arg1}")
            setattr(launcher.cfg, arg1, getattr(DEFAULTS, arg1))
            log.info("%s сброшен", arg1)
        else:
            for f in fields(Config):
                setattr(launcher.cfg, f.name, getattr(DEFAULTS, f.name))
            log.info("Все параметры сброшены")
        autosave(launcher.cfg, cfg_path)
    elif cmd == "save":
        save_config(launcher.cfg, Path(arg1) if arg1 else cfg_path)
    elif cmd == "load":
        load_config(launcher.cfg, Path(arg1) if arg1 else cfg_path)
    elif cmd == "start":
        launcher.start(check_target(arg1))
    elif cmd == "stop":
        launcher.stop(check_target(arg1))
    elif cmd == "restart":
        launcher.restart(check_target(arg1))
    elif cmd == "status":
        print(launcher.status())
    elif cmd == "check":
        launcher.check()
    elif cmd == "url":
        print(launcher.endpoint())
    elif cmd == "logs":
        which, n = "mcp", 30
        for a in (parts[1:] if len(parts) > 1 else []):
            for token in a.split():
                if token in ("chrome", "mcp"):
                    which = token
                elif token.isdigit():
                    n = int(token)
        mp = launcher.chrome if which == "chrome" else launcher.mcp
        lines = mp.tail(n)
        print("\n".join(lines) if lines else f"(вывод {which} пуст)")
    else:
        raise LauncherError(f"Неизвестная команда: {cmd}. Введите 'help'.")
    return True


def repl(launcher: Launcher, cfg_path: Path) -> None:
    try:
        import readline  # noqa: F401  (история и редактирование строки)
    except ImportError:
        pass
    print("MCP launcher. Введите 'help' для списка команд, 'start' для запуска.")
    while True:
        try:
            line = input("mcp> ")
        except EOFError:
            print()
            return
        try:
            if not handle(line, launcher, cfg_path):
                return
        except LauncherError as e:
            log.error("%s", e)
        except Exception:
            log.exception("Непредвиденная ошибка при выполнении команды")


def _raise_exit(signum, _frame) -> None:
    log.info("Получен сигнал %s — завершаюсь", signal.Signals(signum).name)
    raise SystemExit(0)


def main() -> int:
    # Конфиг и лог по умолчанию лежат рядом со скриптом (или бинарником), а не
    # в cwd — чтобы сохранённые настройки подхватывались из любой директории.
    # В скомпилированном виде (PyInstaller onefile) __file__ указывает во
    # временный каталог распаковки, поэтому берём путь к исполняемому файлу.
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Интерактивный лаунчер Chrome (CDP) + MCP (supergateway)")
    ap.add_argument("--config", default=str(base / "mcp_launcher.json"), help="файл конфигурации (JSON)")
    ap.add_argument("--log-file", default=str(base / "mcp_launcher.log"), help="файл лога")
    ap.add_argument("--autostart", action="store_true", help="сразу запустить Chrome и MCP")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("-s", "--set", action="append", default=[], metavar="KEY=VALUE",
                    help="переопределить параметр (можно несколько раз; не сохраняется в конфиг)")
    args = ap.parse_args()

    cfg = Config()
    cfg_path = Path(args.config)
    setup_logging(cfg, Path(args.log_file))

    launcher: Optional[Launcher] = None
    try:
        if cfg_path.exists():
            try:
                load_config(cfg, cfg_path)
            except LauncherError as e:
                log.error("%s", e)
        for item in args.set:
            if "=" not in item:
                log.error("Неверный формат -s %r, ожидается KEY=VALUE", item)
                continue
            k, v = item.split("=", 1)
            try:
                set_value(cfg, k.strip(), v.strip())
            except LauncherError as e:
                log.error("%s", e)

        launcher = Launcher(cfg)
        atexit.register(launcher.shutdown)
        signal.signal(signal.SIGTERM, _raise_exit)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _raise_exit)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _raise_exit)

        if args.autostart:
            try:
                launcher.start("all")
            except LauncherError as e:
                log.error("Автозапуск не удался: %s", e)
        repl(launcher, cfg_path)
    except KeyboardInterrupt:
        print()
        log.info("Прервано пользователем (Ctrl+C)")
    except SystemExit:
        pass
    except Exception:
        log.exception("Критическая ошибка")
        return 1
    finally:
        if launcher:
            launcher.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
