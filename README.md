# cdp_mcp_server

Интерактивный лаунчер **Chrome (CDP) + MCP-сервер [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)** с HTTP-эндпоинтом для подключения харнессов и MCP-клиентов (ZCode, Claude, curl и т.д.).

Поднимает и контролирует два процесса:

1. `google-chrome --remote-debugging-port=<cdp_port> --user-data-dir=...`
2. MCP-сервер chrome-devtools-mcp, подключённый к Chrome по CDP

и обслуживает клиентов по Streamable HTTP: `http://<ip>:<mcp_port>/mcp`.

## Почему builtin

MCP-клиенты часто ждут ответ на `initialize` не дольше ~1с. supergateway (и любой шлюз, запускающий новый процесс MCP на сессию) отвечает 1.3–1.5с — клиент успевает разорвать соединение. Сам chrome-devtools-mcp стартует ~1с (бандл puppeteer).

Режим `builtin` (по умолчанию) держит **один постоянно живущий** процесс MCP и собственный HTTP-мост на Python: `initialize` прогревается при старте и отдаётся из кэша за ~1мс, инструменты идут напрямую. Режим `supergateway` оставлен для sse/ws/токена/CORS (`set mcp_gateway supergateway`).

## Быстрый старт

Скачайте бинарник из релизов (Linux x86_64), либо запускайте исходник (нужен только Python 3.10+, без зависимостей):

```bash
./cdp_mcp_server                 # или: python3 chrome_cdp_mcp_start.py
mcp> start                       # Chrome + MCP, адрес напечатает в логе
mcp> url                         # http://172.x.x.x:8333/mcp — вставить в клиент
mcp> quit                        # остановить всё и выйти
```

Требования: установленный Google Chrome/Chromium, Node.js с npx (для chrome-devtools-mcp).

## Команды

```
show / set <параметр> <значение> / reset [параметр]   — конфигурация
start|stop|restart [all|chrome|mcp]                   — управление процессами
status / check / logs [chrome|mcp] [N] / url / help   — диагностика
save|load [файл]                                      — конфиг вручную
```

Изменения `set`/`reset` **автоматически сохраняются** в `mcp_launcher.json` рядом со скриптом/бинарником и подхватываются при следующем запуске.

## Конфигурация (основное)

| Параметр | По умолчанию | Описание |
|---|---|---|
| `cdp_port` | 9222 | порт CDP Chrome |
| `mcp_port` | 8931 | порт HTTP-эндпоинта MCP |
| `mcp_gateway` | builtin | `builtin` (мгновенный коннект) \| `supergateway` |
| `mcp_transport` | sse | транспорт supergateway: sse \| streamableHttp \| ws |
| `mcp_stateful` | on | supergateway+streamableHttp: `--stateful` |
| `mcp_package` | chrome-devtools-mcp@latest | пакет MCP; путь/команда — запуск напрямую |
| `headless` | off | Chrome без окна |
| `user_data_dir` | /tmp/chrome-mcp | профиль Chrome (обязателен для CDP в Chrome 136+) |
| `mcp_token` | | Bearer-токен (только supergateway) |

Полный список — `show` в REPL.

## Сборка бинарника

```bash
python3 -m PyInstaller --onefile --name cdp_mcp_server --distpath . \
  --workpath build --specpath build chrome_cdp_mcp_start.py
```
