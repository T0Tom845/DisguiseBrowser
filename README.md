# DisguiseBrowser

Живой (headed) браузер для парсинга и автоматизации, который антибот‑системы
видят как обычный Chrome обычного пользователя. Аналог CloakBrowser, но без
собственной сборки Chromium: работает ваш настоящий Google Chrome.

Python 3.12+, Linux. Статус: **v0.2** — библиотека, прокси с авторизацией и geo‑согласованием,
OS‑уровневый ввод, MCP‑сервер для AI‑агентов, несколько браузеров в одном процессе.

## Концепция простыми словами

Сайт распознаёт бота по нескольким слоям одновременно:

| Слой | Что смотрят | Как закрываем |
|---|---|---|
| **Сеть и TLS** | «рукопожатие» браузера с сервером (JA3/JA4, HTTP/2) — его невозможно подделать поверх | используем **настоящий системный Chrome**: отпечаток настоящий, версия всегда актуальная |
| **Протокол управления** | автоматизация управляет браузером через DevTools‑протокол (CDP); Playwright/Puppeteer шлют команды с побочными эффектами, видимыми странице (`Runtime.enable`, `sourceURL`, стек вызовов) | **свой «тихий» CDP‑клиент**: запрещённые команды перечислены явно и блокируются; JS выполняется в изолированном мире |
| **JS‑окружение** | `navigator.webdriver`, `HeadlessChrome`, экран 800×600 без рабочей области, отсутствие WebGL на сервере, пустой профиль | запуск без автоматизационных флагов; headed‑режим; на серверах — виртуальный рабочий стол Xvfb с EWMH‑рабочей областью («панель» → `screen.availHeight`); имя GPU подменяется **на уровне драйвера Mesa**, а не хуками в JS |
| **Согласованность** | язык ↔ таймзона ↔ гео IP ↔ ОС ↔ шрифты ↔ GPU | одна сущность **Persona** порождает все параметры и проверяет их; `geoip=True` берёт таймзону и языки по exit‑IP прокси |
| **Поведение** | телепортация курсора, мгновенный ввод, синтетические события (`pressure: 0` при нажатой кнопке) | **Humanizer** (Безье, minimum‑jerk, тремор, лог‑нормальные задержки) поверх **XTest** — событий самого X‑сервера, неотличимых от физической мыши и клавиатуры |
| **Измеримость** | — | `disguise test` прогоняет конфигурацию по публичным детекторам |

Подробное обоснование, модель угроз, эмпирика и дорожная карта —
в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Установка

```bash
# Google Chrome stable должен быть установлен. Для серверов без дисплея:
sudo apt install xvfb libgl1-mesa-dri mesa-utils openbox fonts-noto-color-emoji

uv sync            # разработка
# или: pip install -e .
```

Проверка окружения:

```bash
uv run disguise doctor
```

## Быстрый старт

```python
import asyncio
from disguisebrowser import launch

async def main() -> None:
    # display="auto": реальный дисплей, если есть $DISPLAY, иначе виртуальный (Xvfb)
    async with launch() as browser:
        page = await browser.new_page("https://example.com")
        print(await page.title())

        # Парсинг: селекторы CSS / XPath, текст, атрибуты, HTML
        for item in await page.query_all("p"):
            print(await item.text())

        # Автоматизация: человекоподобные клик, ввод, скролл
        await page.type("input[name=q]", "disguise browser")
        await page.click("button[type=submit]")
        await page.wait_for_selector("#results", state="visible")
        await page.screenshot("results.png")

asyncio.run(main())
```

Персона, прокси с авторизацией и персистентный профиль:

```python
from disguisebrowser import GPU_PRESETS, Persona, launch

persona = Persona.from_host(
    proxy="http://user:pass@residential-proxy:8080",   # или socks5://user:pass@host:port
    gpu=GPU_PRESETS["intel-xe"],      # применяется на виртуальном дисплее
    seed=42,                          # одинаковая геометрия/GPU у «возвращающегося» посетителя
)
# geoip=True: таймзона и языки берутся по exit-IP прокси (через тот же туннель)
browser = await launch(
    persona=persona, geoip=True, user_data_dir="./profiles/berlin", human="careful"
)
```

Chrome при этом видит только локальный SOCKS5‑шлюз без пароля (`disguisebrowser.proxy`):
креды не попадают в командную строку, DNS резолвится на стороне прокси, WebRTC
ограничен политикой профиля.

Несколько браузеров в одном процессе — просто несколько `launch()`: у каждого свой
Xvfb, профиль, прокси и персона:

```python
browsers = await asyncio.gather(*(launch(persona=Persona.from_host(seed=s)) for s in (1, 2, 3)))
```

### MCP‑сервер для AI‑агентов

```bash
uv sync --extra mcp            # или pip install 'disguisebrowser[mcp]'
disguise mcp                   # stdio — для Claude Desktop / Cursor / Claude Code
disguise mcp --http --port 8000  # Streamable HTTP — для контейнеров
```

Инструменты: `browser_launch` → `session_id` (несколько сессий одновременно), `navigate`,
`snapshot` (accessibility‑дерево с `[ref=eN]`), `click` / `hover` / `type_text` /
`press_key` / `scroll` по ref или CSS‑селектору, `extract_text`, `get_html`, `screenshot`,
`evaluate`, `wait_for`, вкладки (`tabs` / `new_tab` / `select_tab` / `close_tab`),
`go_back`, `browser_list`, `browser_close`.

Конфигурация клиента (stdio):

```json
{ "mcpServers": { "disguise": { "command": "uv", "args": ["run", "--directory", "/path/to/DisguiseBrowser", "disguise", "mcp"] } } }
```

## CLI

```bash
disguise doctor                       # Chrome, Xvfb, Mesa, шрифты, персона хоста
disguise test --display virtual       # scorecard по детекторам (sannysoft, rebrowser, …)
disguise open https://example.com     # открыть браузер и наблюдать вживую
```

Результат `disguise test --display virtual` на машине без GPU (Chrome 154, Xvfb):

```
PASS  sannysoft             0 of 57 checks failed
PASS  rebrowser             6 green, 4 not triggered
PASS  deviceandbrowserinfo  You are human!
PASS  browserscan           verdict: Normal
PASS  tls                   JA4 t13d1517h2_… · HTTP/2 52d84b11…
```

## Что внутри (v0.2)

- `cdp/` — WebSocket‑клиент CDP, сессии, события, **политика запрещённых команд**.
- `launcher.py` — поиск Chrome, минимальный рецепт флагов, Preferences профиля, запуск.
- `persona.py` — Persona, честное определение параметров хоста, валидация, пресеты GPU,
  детерминированная геометрия и рабочая область по `seed`.
- `proxy.py` — **Proxy Gateway**: локальный SOCKS5 → upstream HTTP CONNECT / SOCKS5
  с авторизацией; geo‑lookup по exit‑IP через тот же туннель; таблица страна → языки.
- `display.py` — Xvfb + GLX, EWMH‑рабочая область, оконный менеджер (если установлен),
  GPU‑идентичность через Mesa driconf; параллельные дисплеи для нескольких браузеров.
- `page.py` — навигация и ожидания, изолированные миры, DOM‑запросы, `ElementHandle`,
  accessibility‑snapshot с `[ref=eN]`, скриншоты, cookies, история, человекоподобные
  `click`/`type`/`scroll`.
- `input/` — кинематические модели; бэкенды **XTest** (по умолчанию на X‑дисплее) и CDP.
- `snapshot.py`, `mcp_server.py` — accessibility‑дерево для агентов и MCP‑сервер с сессиями.
- `scorecard.py`, `cli.py` — детекторы и команды `doctor` / `test` / `open` / `mcp`.
- `tests/` — 63 юнит‑теста (в т.ч. фейковые upstream‑прокси и CDP‑сервер) и 27 интеграционных
  на реальном Chrome: офлайн‑фикстуры утечек, Chrome через авторизованный шлюз, geo‑выравнивание,
  XTest‑координаты и `pressure`, три браузера одновременно, MCP‑сценарий агента.

## Что измерено про ввод (Chrome 154, X11)

- Артефакт CDP `screenX == pageX` (crbug 40280325) в headed‑режиме **не воспроизводится** —
  координаты экрана корректны.
- Зато CDP‑события отдают `pressure: 0` при нажатой кнопке, тогда как физическая мышь и XTest —
  `0.5` (Pointer Events); и реальный «щелчок» колеса даёт `deltaY: 120`, а не 53.
  Поэтому XTest — бэкенд по умолчанию, а CDP‑колесо калибровано под 120 px.

## Ограничения и что дальше

- Headless‑режим (`headless=True`) — явный opt‑in со слабым стелсом.
- Canvas/audio‑хэши и лимиты WebGL на виртуальном дисплее остаются от llvmpipe.
- XTest печатает через раскладку US; символы вне ASCII вводятся через `Input.insertText`
  (как IME). Русская раскладка ЙЦУКЕН — следующий шаг.
- Оконный менеджер (`openbox`) и панель (`tint2`) рекомендуются для прода; без них библиотека
  публикует рабочую область сама и фокусирует вкладку через `Page.bringToFront`.
- Дальше: Docker‑образ с проверкой scorecard изнутри контейнера, прогрев профилей,
  воркер‑контексты (OOPIF/Service Worker), `connect()` к уже запущенному Chrome.

Инструмент предназначен для законного использования с соблюдением условий
целевых сайтов; ответственность за применение лежит на пользователе.
