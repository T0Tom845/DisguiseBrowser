# DisguiseBrowser

Живой (headed) браузер для парсинга и автоматизации, который антибот‑системы
видят как обычный Chrome обычного пользователя. Аналог CloakBrowser, но без
собственной сборки Chromium: работает ваш настоящий Google Chrome.

Python 3.12+, Linux. Статус: **v0.3** — библиотека, прокси с авторизацией и geo‑согласованием,
OS‑уровневый ввод (включая ЙЦУКЕН), прогрев профилей, воркеры и OOPIF, подключение к уже
запущенному Chrome, MCP‑сервер, Docker‑образ.

## Концепция простыми словами

Сайт распознаёт бота по нескольким слоям одновременно:

| Слой | Что смотрят | Как закрываем |
|---|---|---|
| **Сеть и TLS** | «рукопожатие» браузера с сервером (JA3/JA4, HTTP/2) — его невозможно подделать поверх | используем **настоящий системный Chrome**: отпечаток настоящий, версия всегда актуальная |
| **Протокол управления** | автоматизация управляет браузером через DevTools‑протокол (CDP); Playwright/Puppeteer шлют команды с побочными эффектами, видимыми странице (`Runtime.enable`, `sourceURL`, стек вызовов) | **свой «тихий» CDP‑клиент**: запрещённые команды перечислены явно и блокируются; JS выполняется в изолированном мире |
| **JS‑окружение** | `navigator.webdriver`, `HeadlessChrome`, экран 800×600 без рабочей области, отсутствие WebGL на сервере, пустой профиль | запуск без автоматизационных флагов; headed‑режим; на серверах — виртуальный рабочий стол Xvfb с EWMH‑рабочей областью («панель» → `screen.availHeight`); имя GPU подменяется **на уровне драйвера Mesa**, а не хуками в JS |
| **Согласованность** | язык ↔ таймзона ↔ гео IP ↔ ОС ↔ шрифты ↔ GPU | одна сущность **Persona** порождает все параметры и проверяет их; `geoip=True` берёт таймзону и языки по exit‑IP прокси |
| **Поведение** | телепортация курсора, мгновенный ввод, синтетические события (`pressure: 0` при нажатой кнопке) | **Humanizer** (Безье, minimum‑jerk, тремор, лог‑нормальные задержки) поверх **XTest** — событий самого X‑сервера, неотличимых от физической мыши и клавиатуры |
| **Измеримость** | — | `disguise test` прогоняет конфигурацию по публичным детекторам; `disguise capture`/`diff` снимают полный отпечаток браузера и сравнивают с эталоном реального устройства |

Подробное обоснование, модель угроз, эмпирика и дорожная карта —
в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Что именно может выдать профиль,
что из этого закрыто и чем проверено — в [`docs/SIGNALS.md`](docs/SIGNALS.md).
План форка Chromium (энтропия устройств, не‑Linux персоны) — в
[`docs/FORK_PLAN.md`](docs/FORK_PLAN.md).

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

Прогрев постоянного профиля и подключение к Chrome, который уже слушает DevTools‑порт:

```python
from disguisebrowser import connect, launch

async with launch(user_data_dir="./profiles/berlin") as browser:
    await browser.warmup()          # несколько обычных сайтов → история, кэш, cookies
    page = await browser.new_page("https://example.com")

# Chrome не закрывается: connect() только отсоединяет CDP
browser = await connect(port=9222)
```

Русская персона на виртуальном дисплее получает раскладку `us,ru`. Кириллица уходит
реальными клавишами XTest (группа ЙЦУКЕН переключается через `XkbLockGroup`, без
аккорда Alt+Shift, который страница могла бы увидеть). На чужом дисплее и для символов
вне раскладки остаётся `Input.insertText`.

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
`go_back`, `browser_list`, `browser_close`, `warmup`, `browser_connect`.

Конфигурация клиента (stdio):

```json
{ "mcpServers": { "disguise": { "command": "uv", "args": ["run", "--directory", "/path/to/DisguiseBrowser", "disguise", "mcp"] } } }
```

## Движок Camoufox (Firefox) и постоянные профили

Второй движок рядом с Chrome: [Camoufox](https://github.com/daijro/camoufox) — сборка
Firefox, где отпечаток подменяется в C++, а не JS‑инъекциями. Подходит, когда нужны
**много долгоживущих личностей** (как «профили» в антидетект‑браузерах) и не нужен
именно Chrome. Весь верхний слой (MCP, scorecard, capture, warmup, гуманизация, прокси‑шлюз)
работает на обоих движках без ветвления: он говорит с браузером через
`protocols.BrowserLike` / `PageLike`.

```bash
uv sync --extra camoufox && uv run python -m camoufox fetch     # браузер качается один раз
disguise profile create shop-1 --os windows --proxy socks5://user:pass@host:1080 --geoip
disguise profile create work --os macos --kind preset --seed 7
disguise profile list
disguise open --engine camoufox --profile shop-1 https://example.com
disguise test --engine camoufox --profile shop-1 --display virtual
disguise profile set shop-1 --proxy ''        # сменить сеть; устройство не пересоздаётся
disguise profile delete work --yes
```

```python
from disguisebrowser import launch_camoufox

async with launch_camoufox(profile="shop-1", create=True, os="windows") as browser:
    page = await browser.new_page("https://example.com")
    await page.snapshot()   # то же accessibility‑дерево с [ref=eN], что и у Chrome
```

Что такое профиль: `profile.json` (устройство от fpgen или один из 113 готовых пресетов,
ОС, прокси, языки, таймзона, seed; права `0600`, прокси может содержать пароль) плюс каталог
Firefox `user-data/` с cookies, localStorage и кэшем. Устройство **закреплено**: одинаковые
UA, экран, ядра, шрифты, WebGL‑параметры и аудио‑seed при каждом запуске; `profile set`
меняет только сеть и заметки. Один профиль — один браузер одновременно (flock;
`delete` тоже отказывает, пока профиль занят). Хранилище лежит в
`$DISGUISE_HOME/profiles` или `~/.local/share/disguisebrowser/profiles`.

MCP: `browser_launch(engine="camoufox", profile="shop-1", create=true, os="windows")`,
а также `profile_list` и `profile_create`. Удаление профилей намеренно только в CLI.

Docker: `docker build -f Dockerfile.camoufox -t disguisebrowser-fox .`; профили держите
на томе `/data`.
Проверено в образе (Camoufox 156.0.1‑beta.36, Xvfb без GPU, профиль на томе): sannysoft
0 из 56 (1 проверка n/a), rebrowser 5 green, TLS — JA4 Firefox.

**Чего этот движок не делает** (проверено на сборке 156.0.1‑beta.36, Firefox 156):

- Это **не Chrome**: TLS/JA4 и HTTP/2 принадлежат Firefox. Это согласовано с его UA, но
  цели, ожидающие Chrome, не подойдут. Проверка «Chrome (New)» на sannysoft неприменима и
  в отчёте помечена как `n/a for this engine`, а не как провал.
- Версия Firefox в UA подменяется на версию реальной сборки (в профиле может быть 146,
  в сети уйдёт 156), поэтому после обновления Camoufox UA профиля сдвигается вместе с движком.
- Хэш `toDataURL()` для canvas и WebGL меняется от сессии к сессии (пиксели стабильны), а
  хэши пикселей на одной машине **одинаковы у разных профилей**: общий рендерер связывает
  профили одной машины. Разделять их надо отдельными хостами или прокси, а не профилями.
- `main_world=True` включает `evaluate(..., world="main")`; по умолчанию код страницы
  изолирован, и глобалы страницы из `evaluate` не видны.
- Ввод идёт через Playwright/Juggler (встроенная гуманизация Camoufox), а не через XTest.
  `--gpu`, `--keyboard`, `--seed` — опции Chrome; на Camoufox их задаёт профиль.
- Прокси без `--geoip` даёт неподавляемое `LeakWarning` от Camoufox: таймзона и язык при
  этом остаются хостовыми. Для чужого IP включайте `--geoip`.
- Первый `profile create` скачивает модель fpgen с GitHub (нужна сеть); образ Docker уже
  содержит её.

## CLI

```bash
disguise doctor                       # Chrome, Xvfb, Mesa, шрифты, персона хоста
disguise test --display virtual       # scorecard по детекторам (sannysoft, rebrowser, …)
disguise capture                      # снять эталонный отпечаток этого браузера в fingerprints/
disguise capture --serve              # отдать страницу-сборщик реальному браузеру (другая машина)
disguise diff fingerprints/a.json fingerprints/b.json   # сравнить две записи по категориям
disguise warmup --profile ./profiles/berlin   # история и кэш перед целевым сайтом
disguise open https://example.com     # открыть браузер и наблюдать вживую
disguise open --engine camoufox --profile shop-1   # Firefox с закреплённым профилем
disguise profile list|create|show|set|delete       # профили Camoufox (см. выше)
```

Docker (Chrome stable, Xvfb, Mesa, openbox, tint2, шрифты, пользователь без root):

```bash
docker build -t disguisebrowser .
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser doctor
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser test --display virtual
```

`seccomp=unconfined` нужен штатной песочнице Chrome (user namespaces); без него Chrome
падает на старте. Флаг `--no-sandbox` библиотека не ставит. Проверено: Chrome 155 в этом
образе проходит scorecard 5/5.

Результат `disguise test --display virtual` в контейнере без GPU (Chrome 155, Xvfb):

```
PASS  sannysoft             0 of 57 checks failed
PASS  rebrowser             6 green, 4 not triggered
PASS  deviceandbrowserinfo  You are human!
PASS  browserscan           verdict: Normal
PASS  tls                   JA4 t13d1517h2_… · HTTP/2 52d84b11…
```

## Что внутри (v0.3)

- `cdp/` — WebSocket‑клиент CDP, сессии, события, **политика запрещённых команд**.
- `launcher.py` — поиск Chrome, минимальный рецепт флагов, Preferences профиля, запуск.
- `persona.py` — Persona, честное определение параметров хоста, валидация, пресеты GPU,
  детерминированная геометрия и рабочая область по `seed`.
- `proxy.py` — **Proxy Gateway**: локальный SOCKS5 → upstream HTTP CONNECT / SOCKS5
  с авторизацией; geo‑lookup по exit‑IP через тот же туннель; таблица страна → языки.
- `display.py` — Xvfb + GLX, EWMH‑рабочая область, оконный менеджер и панель tint2 (если
  установлены), раскладка `setxkbmap`, GPU‑идентичность через Mesa driconf.
- `page.py` — навигация и ожидания, изолированные миры, DOM‑запросы, `ElementHandle`,
  accessibility‑snapshot с `[ref=eN]`, скриншоты, cookies, история, человекоподобные
  `click`/`type`/`scroll`.
- `input/` — кинематические модели; бэкенды **XTest** (по умолчанию на X‑дисплее, ЙЦУКЕН
  через `XkbLockGroup` на своём виртуальном дисплее) и CDP.
- `warmup.py` — прогрев профиля: несколько обычных сайтов, скролл, пауза; список стабилен при `seed`.
- `snapshot.py`, `mcp_server.py` — accessibility‑дерево для агентов и MCP‑сервер с сессиями.
- `scorecard.py`, `cli.py` — детекторы (с поправкой на движок) и команды `doctor` / `test` / `warmup` / `open` / `profile` / `mcp`.
- `protocols.py` — `BrowserLike` / `PageLike`: общий контракт Chrome‑ и Camoufox‑движка.
- `camoufox/` — движок Firefox: `profiles.py` (хранилище, блокировки), `browser.py` (`launch`, опции, сеть), `page.py` (тот же API страницы поверх Playwright).
- `tests/` — 188 тестов (137 юнит + 51 интеграционных на реальных Chrome и Camoufox): офлайн‑фикстуры утечек,
  воркер и cross‑site iframe, ЙЦУКЕН, прокси‑шлюз, geo‑выравнивание, XTest, несколько браузеров, MCP.
  Полный прогон внутри Docker‑образа:
  `docker run --rm --shm-size=1g --security-opt seccomp=unconfined -v "$PWD:/work:ro" -w /work --entrypoint bash disguisebrowser -c 'pip install --target /tmp/d pytest pytest-asyncio "mcp>=2.0" >/dev/null; PYTHONPATH=/work/src:/tmp/d:/work HOME=/tmp python -m pytest -q -p no:cacheprovider'`

## Что измерено про ввод (Chrome 154, X11)

- Артефакт CDP `screenX == pageX` (crbug 40280325) в headed‑режиме **не воспроизводится** —
  координаты экрана корректны.
- Зато CDP‑события отдают `pressure: 0` при нажатой кнопке, тогда как физическая мышь и XTest —
  `0.5` (Pointer Events); и реальный «щелчок» колеса даёт `deltaY: 120`, а не 53.
  Поэтому XTest — бэкенд по умолчанию, а CDP‑колесо калибровано под 120 px.

## Ограничения и что дальше

- Headless‑режим (`headless=True`) — явный opt‑in со слабым стелсом.
- Canvas/audio‑хэши и лимиты WebGL на виртуальном дисплее остаются от llvmpipe.
- ЙЦУКЕН включается на виртуальном дисплее, если язык персоны — `ru` (или `--keyboard us,ru`).
  На чужом X‑сервере группа не переключается, кириллица идёт через `Input.insertText`.
- Если установлены `openbox` и `tint2`, виртуальный дисплей поднимает оба: панель даёт
  `screen.availHeight < screen.height`. Без оконного менеджера рабочую область публикует
  сама библиотека.
- Дальше: прогон scorecard внутри собранного образа на машине с сетью до детекторов,
  наблюдаемость и мультипрофильный менеджер (фаза 3). Канвас/аудио‑хэши llvmpipe по‑прежнему
  не совпадают с заявленным GPU.

Инструмент предназначен для законного использования с соблюдением условий
целевых сайтов; ответственность за применение лежит на пользователе.
