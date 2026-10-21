# DisguiseBrowser

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Linux](https://img.shields.io/badge/platform-Linux-111111?logo=linux&logoColor=white)](#установка)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Живой браузер для парсинга и автоматизации. Страница видит обычный десктопный сеанс: установленный в системе Chrome или [Camoufox](https://github.com/daijro/camoufox) — сборку Firefox, где отпечаток подменяется в C++, а не скриптом на странице.

Один и тот же API у библиотеки, CLI и MCP-сервера. Python 3.12+, Linux. Сейчас **v0.3**.

| | Chrome | Camoufox |
|---|---|---|
| Браузер | системный Google Chrome | Firefox от Camoufox |
| Когда он уместен | цель ждёт Chrome и его TLS | много долгоживущих личностей |
| Личность | персона с этой машины, на виртуальном дисплее — GPU через Mesa | именной профиль: устройство, экран, cookies, кэш |
| Ввод | XTest, события самого X-сервера | Playwright/Juggler и гуманизация Camoufox |
| Сеть | свой SOCKS5-шлюз, пароль не попадает в командную строку | тот же шлюз |

Модель угроз, что именно закрыто и чем это проверено — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) и [`docs/SIGNALS.md`](docs/SIGNALS.md).

## Установка

Chrome — тот, что уже стоит в системе. Camoufox ставится отдельной командой и качается один раз.

```bash
# сервер без монитора
sudo apt install xvfb libgl1-mesa-dri mesa-utils openbox fonts-noto-color-emoji

uv sync --extra camoufox --extra mcp
uv run python -m camoufox fetch
uv run disguise doctor
```

`pip install -e '.[camoufox,mcp]'` делает то же самое. Каталог `camoufox-dist/` в git не входит: собранный Firefox туда не помещается. Официальный бинарник, который качает `camoufox fetch`, запускается сразу. Свой патч шума canvas подключается отдельно, это [ниже](#своя-сборка-camoufox).

## Быстрый старт

Профиль Camoufox. Устройство закрепляется при создании и не пересобирается на следующем запуске.

```python
import asyncio
from disguisebrowser import launch_camoufox

async def main() -> None:
    async with launch_camoufox(profile="shop-1", create=True, os="windows") as browser:
        page = await browser.new_page("https://example.com")
        print(await page.title())
        for item in await page.query_all("p"):
            print(await item.text())
        await page.screenshot("page.png")

asyncio.run(main())
```

Тот же сценарий из терминала:

```bash
disguise profile create shop-1 --os windows --screen 1920x1080 \
    --proxy socks5://user:pass@host:1080 --geoip
disguise open --engine camoufox --profile shop-1 https://example.com
disguise test --engine camoufox --profile shop-1 --display virtual
```

Системный Chrome, без профиля Camoufox:

```python
from disguisebrowser import Persona, launch

persona = Persona.from_host(
    proxy="http://user:pass@residential-proxy:8080",
    seed=42,
)
browser = await launch(persona=persona, geoip=True, user_data_dir="./profiles/berlin")
```

`geoip=True` берёт таймзону и языки по exit-IP прокси. Chrome видит только локальный SOCKS5 без пароля. Несколько браузеров в одном процессе — несколько `launch()`: у каждого свой дисплей, профиль и прокси.

## Профили

Профиль — это `profile.json` (устройство, ОС, прокси, языки, таймзона, seed; файл с правами `0600`) и каталог Firefox с cookies, localStorage и кэшем. Хранилище: `$DISGUISE_HOME/profiles` или `~/.local/share/disguisebrowser/profiles`.

```bash
disguise profile list
disguise profile create work --os macos --kind preset --seed 7
disguise profile set shop-1 --proxy ''          # сеть меняется, устройство нет
disguise profile set shop-1 --screen 1920x1080  # шрифты и шум canvas пересчитаются со следующего запуска
disguise profile delete work --yes
```

Один профиль — один браузер одновременно. `delete` тоже отказывает, пока профиль занят.

`disguise manager` открывает одну страницу на `127.0.0.1:8787` со всеми профилями. У строки есть выключатель, адрес и кнопка «В браузер»: клики, прокрутка и клавиатура уходят в этот профиль, VNC для этого не нужен. Там же задаются разрешение, прокси и заметка. `Ctrl+C` в терминале закрывает менеджер и те окна, которые он сам открыл.

## MCP

```bash
disguise mcp                      # stdio, для Cursor / Claude
disguise mcp --http --port 8000   # Streamable HTTP
```

```json
{
  "mcpServers": {
    "disguise": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/DisguiseBrowser", "disguise", "mcp"]
    }
  }
}
```

Сессии независимы: `browser_launch` возвращает `session_id`. Дальше `navigate`, `snapshot` (дерево доступности с `[ref=eN]`), `click` / `type_text` / `scroll` по ref или CSS, `screenshot`, `evaluate`, вкладки, `browser_list`, `browser_close`. Для Camoufox: `browser_launch(engine="camoufox", profile="shop-1", create=true, os="windows")`, плюс `profile_list` и `profile_create`. Удаление профилей оставлено в CLI.

## Docker

Образ Chrome: системный Chrome, Xvfb, Mesa, openbox, tint2.

```bash
docker build -t disguisebrowser .
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser doctor
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser test --display virtual
```

`seccomp=unconfined` нужен песочнице Chrome. Флаг `--no-sandbox` библиотека не ставит.

Образ Camoufox по умолчанию сам скачивает официальную сборку:

```bash
docker build -f Dockerfile.camoufox -t disguisebrowser-fox .
docker run --rm disguisebrowser-fox doctor --engine camoufox
docker run --rm -v fox-data:/data disguisebrowser-fox profile create shop-1 --os windows
docker run --rm --shm-size=1g -v fox-data:/data disguisebrowser-fox \
    test --engine camoufox --profile shop-1 --display virtual
```

Профили, cookies и кэш лежат в `/data`. Этот том имеет смысл сохранять.

Своя сборка с патчем canvas подставляется архивом, без исходников Firefox в контексте сборки:

```bash
docker build -f Dockerfile.camoufox \
    --build-arg CAMOUFOX_DIST_URL=https://example.com/camoufox-dist.tar.zst \
    --build-arg CAMOUFOX_DIST_SHA256=<sha256 архива> \
    -t disguisebrowser-fox .
```

Архив — это содержимое каталога сборки, `camoufox-bin` лежит в корне:

```bash
tar -C camoufox-dist -I zstd -cf camoufox-dist.tar.zst .
sha256sum camoufox-dist.tar.zst
```

Такой архив уместно класть во вложение [GitHub Release](https://github.com/T0Tom845/DisguiseBrowser/releases): файл около 300 МБ, в git он не проходит лимит GitHub.

## Своя сборка Camoufox

Официальный Camoufox 156 не меняет пиксели canvas. Профили на одной машине получают одну и ту же картинку. Патч [`fork/camoufox/zz-canvas-readback.patch`](fork/camoufox/zz-canvas-readback.patch) при чтении слегка сдвигает редкие пиксели. Куда именно — зависит от сида профиля (`canvas:seed`), который запуск и так передаёт. Официальный бинарник этот ключ игнорирует.

Сборка один раз, в отдельном клоне [camoufox](https://github.com/daijro/camoufox) на теге `v156.0.1-beta.36`:

```bash
cp fork/camoufox/zz-canvas-readback.patch patches/
make dir
make build
```

Дальше каталог готового браузера либо пакуется в архив для Docker, либо указывается через `CAMOUFOX_EXECUTABLE_PATH`. Подробности и когда пересобирать — в [`fork/camoufox/README.md`](fork/camoufox/README.md). Исходники Firefox в этот репозиторий не входят. Патч и файлы, из которых он собран, лежат в `fork/camoufox/` и остаются под MPL-2.0.

Проверено на сборке 156.0.1-beta.36 в образе без GPU: sannysoft 0 из 56 (1 проверка неприменима к Firefox), rebrowser 5 green, TLS — JA4 Firefox. Замер «много профилей → много разных хэшей canvas» в репозитории ещё не оформлен отдельной командой.

TLS и HTTP/2 у Camoufox принадлежат Firefox и совпадают с его User-Agent. Цели, которым нужен именно Chrome, закрывает системный движок. Проверка «Chrome (New)» на sannysoft для Camoufox помечается как `n/a for this engine`. Прокси без `--geoip` оставляет таймзону и язык хоста, и Camoufox пишет `LeakWarning`.

## Что уже измерено на Chrome

`disguise test --display virtual` в контейнере без GPU, Chrome 155:

```
PASS  sannysoft             0 of 57 checks failed
PASS  rebrowser             6 green, 4 not triggered
PASS  deviceandbrowserinfo  You are human!
PASS  browserscan           verdict: Normal
PASS  tls                   JA4 t13d1517h2_… · HTTP/2 52d84b11…
```

Ввод на Chrome идёт через XTest: у событий CDP при нажатой кнопке `pressure` остаётся 0, у физической мыши и у XTest — 0.5. На виртуальном дисплее русская персона получает раскладку ЙЦУКЕН реальными клавишами, без аккорда, который страница могла бы увидеть.

Снять отпечаток и сравнить его с другой машиной:

```bash
disguise capture
disguise capture --serve
disguise diff fingerprints/a.json fingerprints/b.json
```

## Ограничения

- Headless (`headless=True`) — явный режим со слабым стелсом.
- На виртуальном дисплее без GPU canvas, audio и лимиты WebGL остаются от llvmpipe. Подмена имени видеокарты этому не помогает.
- ЙЦУКЕН переключается на своём виртуальном дисплее. На чужом X-сервере кириллица уходит через `Input.insertText`.
- Скриптовый ввод Camoufox (MCP, `click`, `type`) намеренно остаётся на Playwright/Juggler. Живой вид в `disguise manager` гуманизацию выключает. Буфер обмена этого вида пока не связан с буфером машины, где открыт менеджер.

Инструмент рассчитан на работу в рамках правил целевых сайтов. Как им пользуются — ответственность того, кто его запускает.

## Документация

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | зачем проект устроен так, а не как форк, который надо пересобирать на каждую правку |
| [`docs/SIGNALS.md`](docs/SIGNALS.md) | таблица сигналов: что закрыто, что нет, чем проверено |
| [`docs/FORK_PLAN.md`](docs/FORK_PLAN.md) | если когда-нибудь понадобится свой патч Chromium |
| [`fork/camoufox/README.md`](fork/camoufox/README.md) | патч canvas: что он меняет и как собрать браузер |

Код библиотеки живёт в `src/disguisebrowser/`. Chrome — `launcher.py`, `cdp/`, `persona.py`, `input/`. Camoufox — пакет `camoufox/`. Общий контракт страницы — `protocols.py`, поэтому MCP, scorecard и прогрев не ветвятся по движку.

## Лицензия

Код DisguiseBrowser — [MIT](LICENSE).

Camoufox и Firefox, патч в `fork/camoufox/` и любая собранная из них программа — Mozilla Public License 2.0. MIT на остальной репозиторий эти права не отменяет. Состав и атрибуции — в [NOTICE](NOTICE). Имена Firefox и Camoufox принадлежат их проектам. Эта сборка — не официальный Camoufox и не продукт Mozilla.
