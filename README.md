# DisguiseBrowser

Живой (headed) браузер для парсинга и автоматизации, который антибот‑системы
видят как обычный Chrome обычного пользователя. Аналог CloakBrowser, но без
собственной сборки Chromium: работает ваш настоящий Google Chrome.

Python 3.12+, Linux. Статус: **v0.1 — рабочая первая версия** (см. «Что внутри»).

## Концепция простыми словами

Сайт распознаёт бота по нескольким слоям одновременно:

| Слой | Что смотрят | Как закрываем |
|---|---|---|
| **Сеть и TLS** | «рукопожатие» браузера с сервером (JA3/JA4, HTTP/2) — его невозможно подделать поверх | используем **настоящий системный Chrome**: отпечаток настоящий, версия всегда актуальная |
| **Протокол управления** | автоматизация управляет браузером через DevTools‑протокол (CDP); Playwright/Puppeteer шлют команды с побочными эффектами, видимыми странице (`Runtime.enable`, `sourceURL`, стек вызовов) | **свой «тихий» CDP‑клиент**: запрещённые команды перечислены явно и блокируются; JS выполняется в изолированном мире |
| **JS‑окружение** | `navigator.webdriver`, `HeadlessChrome`, экран 800×600, отсутствие WebGL на сервере, пустой профиль | запуск без автоматизационных флагов; headed‑режим; на серверах — виртуальный рабочий стол Xvfb; имя GPU подменяется **на уровне драйвера Mesa**, а не хуками в JS |
| **Согласованность** | язык ↔ таймзона ↔ гео IP ↔ ОС ↔ шрифты ↔ GPU | одна сущность **Persona** порождает все параметры и проверяет их на непротиворечивость |
| **Поведение** | телепортация курсора, мгновенный ввод | **Humanizer**: кривые Безье, профиль скорости minimum‑jerk, тремор, лог‑нормальные задержки клавиш, скролл «щелчками» колеса |
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

Персона и персистентный профиль:

```python
from disguisebrowser import GPU_PRESETS, Persona, launch

persona = Persona.from_host(
    languages=("de-DE", "de", "en-US", "en"),
    timezone="Europe/Berlin",
    gpu=GPU_PRESETS["intel-xe"],      # применяется на виртуальном дисплее
    proxy="socks5://10.0.0.1:1080",   # без логина/пароля в v0.1
    seed=42,                          # одинаковая геометрия/GPU у «возвращающегося» посетителя
)
browser = await launch(persona=persona, user_data_dir="./profiles/berlin", human="careful")
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

## Что внутри (v0.1)

- `cdp/` — WebSocket‑клиент CDP, сессии, события, **политика запрещённых команд**.
- `launcher.py` — поиск Chrome, минимальный рецепт флагов, Preferences профиля, запуск.
- `persona.py` — Persona, честное определение параметров хоста, валидация, пресеты GPU.
- `display.py` — Xvfb + GLX, оконный менеджер (если установлен), GPU‑идентичность через Mesa driconf.
- `page.py` — навигация и ожидания, изолированные миры, DOM‑запросы, `ElementHandle`,
  скриншоты, cookies, человекоподобные `click`/`type`/`scroll`.
- `input/` — кинематические модели и CDP‑ввод (мышь, клавиатура, колесо).
- `scorecard.py`, `cli.py` — детекторы и команды `doctor` / `test` / `open`.
- `tests/` — 48 юнит‑тестов и 20 интеграционных на реальном Chrome с офлайн‑фикстурами,
  воспроизводящими утечки (`Runtime.enable`, main‑world стек, Client Hints).

## Ограничения v0.1 и что дальше

- **Прокси с логином/паролем** пока не поддерживаются (Chrome не принимает креды
  в `--proxy-server`). Следующий шаг — локальный Proxy Gateway с авторизацией,
  SOCKS5 и определением таймзоны/локали по exit‑IP.
- **CDP‑ввод** оставляет известный артефакт `screenX == pageX` (crbug 40280325),
  по которому Cloudflare Turnstile распознаёт клики. План — OS‑уровневый ввод через
  XTest на виртуальном дисплее.
- **MCP‑сервер** для AI‑агентов — следующая итерация (тонкая обёртка над библиотекой).
- Headless‑режим (`headless=True`) — явный opt‑in со слабым стелсом.
- Canvas/audio‑хэши и лимиты WebGL на виртуальном дисплее остаются от llvmpipe.

Инструмент предназначен для законного использования с соблюдением условий
целевых сайтов; ответственность за применение лежит на пользователе.
