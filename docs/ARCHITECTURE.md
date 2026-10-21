# DisguiseBrowser — архитектура и обоснование решений

Статус: проектный документ; реализация v0.1 на Python следует ему (см. раздел 11
о принятых решениях и статусе). Все утверждения в разделе «Эмпирическая база»
проверены на этой машине 2026‑10‑09 (Ubuntu 24.04, Google Chrome 154.0.8037.97,
Mesa 25.2.8, Xvfb).

## 1. Цель и границы

**Цель.** Живой (headed) автоматизированный браузер для парсинга и автоматизации
действий на сайтах, который антибот‑системы (Cloudflare, DataDome, Akamai,
Kasada, reCAPTCHA v3, FingerprintJS, BrowserScan и др.) оценивают как обычный
браузер обычного пользователя. Функциональный аналог CloakBrowser.

**Границы (что НЕ делаем).**

- Не решаем CAPTCHA и не обходим платёжные/антифрод‑барьеры банков — задача в
  том, чтобы вызовы CAPTCHA не появлялись, а не в том, чтобы их решать.
- В фазах 1–3 не собираем собственный форк Chromium (см. раздел 2 и 10): для
  уровня «один обычный пользователь» он не нужен, что показывают независимые
  бенчмарки 2026 года. Форк начинается там, где нужны N различимых устройств
  или не‑Linux персоны — план в [`FORK_PLAN.md`](FORK_PLAN.md).
- Не встраиваем ротацию прокси как сервис — прокси приносит пользователь, мы
  обеспечиваем согласованность идентичности с ним.

## 2. Что такое CloakBrowser и почему мы идём другим путём

CloakBrowser (CloakHQ) — тонкая обёртка (Python + JS) вокруг **собственной
сборки Chromium** с 73–87 закрытыми C++‑патчами (canvas, WebGL, audio, шрифты,
GPU, screen, WebRTC, тайминги сети, сигналы автоматизации, поведение CDP‑ввода).
Бинарник (~200 МБ) скачивается при первом запуске. Поверх — API Playwright /
Puppeteer, `humanize=True` (кривые Безье, тайминг клавиатуры, скролл), прокси
HTTP/SOCKS5 с inline‑auth, `geoip=True` (таймзона/локаль по IP прокси),
`--fingerprint=seed` (детерминированная личность), персистентные профили,
Docker‑образ, CLI `cloaktest`, MCP‑сервер.

**Почему не повторяем их путь буквально.**

1. Два независимых бенчмарка 2026 года (Paterson, май 2026: 7 инструментов ×
   31 цель × 3 прогона; Ur Rahim, сентябрь 2026: 7 инструментов × 6 детекторов)
   показывают, что решающий слой — **не JS‑фингерпринт, а протокол управления**.
   `nodriver` (сырой CDP к системному Chrome, без Playwright) — единственный
   инструмент с 0 блокировок из 31 цели (28 OK); CloakBrowser — 26 OK / 2 BLK,
   столько же, сколько `curl_cffi` вообще без браузера. В headed‑режиме все
   «сырые CDP»‑инструменты дают показатели, **идентичные настоящему Chrome**
   (CreepJS 19/0/0, fingerprint‑scan 0, «You are human»).
2. Патченный бинарник — вечно отстающая версия (CloakBrowser для macOS застрял
   на Chromium 145 при актуальном 154), а версия сама по себе — сигнал.
3. Форк Chromium — отдельная инженерная индустрия (сборка ~6 ч на мощной машине,
   поддержка патчей на каждом релизе). Для нас это в лучшем случае фаза 4+.

**Наш путь:** настоящий системный Google Chrome + собственный «тихий» CDP‑клиент
+ все подмены **ниже уровня JavaScript** (флаги запуска, профиль/prefs, окружение
процесса, графический драйвер, шрифты, виртуальный рабочий стол) + когерентная
персона + человекоподобный ввод + измеримость.

## 3. Модель угроз: слои детекта

| Слой | Что проверяют | Чем закрываем |
|---|---|---|
| L0 Сеть | ASN/репутация IP, datacenter vs residential, DNS‑утечки, WebRTC‑IP | Прокси пользователя через локальный шлюз; политика WebRTC; таймзона/локаль по exit‑IP |
| L1 Транспорт | JA3/JA4 TLS, порядок HTTP/2 SETTINGS, Accept‑Language, Client Hints `sec-ch-ua*` | Настоящий Chrome ⇒ отпечаток настоящий. Никогда не ломать Client Hints (см. ADR‑6) |
| L2 Протокол управления | `Runtime.enable`‑утечка, `//# sourceURL`, стек вызовов из main world, `navigator.webdriver`, `__pwInitScripts`, `exposeFunction`, обход CSP, дефолтный viewport | «Тихий CDP» (ADR‑2, ADR‑3), запуск без автоматизационных флагов (ADR‑1) |
| L3 JS‑окружение | UA/platform, screen/availHeight/DPR, WebGL vendor/renderer, canvas/audio‑хэши, шрифты, plugins, permissions, `chrome.*`, Worker‑контекст | Headed‑режим, виртуальный рабочий стол, Mesa‑идентичность GPU (ADR‑5), шрифтовые пакеты, честные значения хоста |
| L4 Когерентность | UA ↔ Client Hints ↔ platform ↔ шрифты ↔ GPU ↔ таймзона ↔ язык ↔ гео IP ↔ TLS | Модель «Персона» — единый источник истины, валидация непротиворечивости до запуска |
| L5 Поведение | Траектории мыши, скорость/ускорение, тайминг клавиш, скролл, паузы, `screenX==pageX` у CDP‑событий | Humanizer (кинематические профили), затем OS‑уровневый ввод через XTest (ADR‑7) |
| L6 История | Пустой профиль, нет cookies/кэша/истории, incognito‑признаки, одинаковая личность с разных IP | Персистентные профили, «прогрев», привязка персоны к прокси |

Пошаговый реестр сигналов по этим слоям с текущим статусом и способом проверки
ведётся в [`SIGNALS.md`](SIGNALS.md); любое изменение стелс‑слоя обновляет его.

## 4. Принципы

- **P1. Настоящий браузер.** Только системный Google Chrome stable (при
  необходимости — Chromium/Chrome for Testing как fallback, но не по умолчанию).
- **P2. Тихий CDP.** Используем лишь те команды, побочные эффекты которых не
  наблюдаемы страницей. Чёрный список ведётся явно и проверяется тестами.
- **P3. Ниже JS.** Никаких `Object.defineProperty`/прокси‑хуков в контексте
  страницы в фазах 1–2. Всё, что нельзя подменить ниже JS, **не подменяем**, а
  делаем честным и правдоподобным (например, честный Linux‑десктоп вместо
  «Windows без шрифтов Windows»).
- **P4. Headed‑first.** Headless — только явный opt‑in с отдельным набором
  мер (UA через флаг, `--screen-info`, Worker‑UA). На серверах — виртуальный
  рабочий стол.
- **P5. Когерентная персона.** Все параметры порождаются из одной сущности и
  проверяются на непротиворечивость; персона привязана к профилю и прокси.
- **P6. Измеримость.** Любое изменение стелс‑слоя проходит через автоматический
  scorecard по публичным детекторам (аналог `cloaktest`), с фиксацией базовой
  линии «настоящий Chrome на этой же машине».

## 5. Эмпирическая база (спайк на этой машине)

Сырой CDP‑клиент (~150 строк, без зависимостей), системный Chrome 154,
Xvfb 1920×1080. Проверялись `bot.sannysoft.com`, `bot-detector.rebrowser.net`,
`deviceandbrowserinfo.com/are_you_a_bot`, `tls.peet.ws`.

| # | Конфигурация | `navigator.webdriver` | Инфобар | WebGL renderer | Вердикт |
|---|---|---|---|---|---|
| 1 | `--remote-debugging-pipe`, без доп. флагов | **true** | нет | нет контекста | dabi: bot (`hasWebdriverTrue`, `hasWebdriverInFrameTrue`); sannysoft: 3 fail |
| 2 | `--remote-debugging-port=0` | **true** | нет | нет контекста | — |
| 3 | pipe + `--disable-blink-features=AutomationControlled` | false | **да** («неподдерживаемый флаг», −56 px innerHeight) | — | — |
| 4 | #3 + `--test-type` | false | нет | — | — |
| 5 | pipe + CDP `Emulation.setAutomationOverride(false)` | **true** (метод умеет только включать) | нет | — | — |
| 6 | **`--remote-debugging-port=<конкретный порт>`**, без иных флагов | **false** | нет | — | — |
| 7 | Xvfb `+extension GLX`, `LIBGL_ALWAYS_SOFTWARE=1`, без `--ignore-gpu-blocklist` | — | — | нет контекста | — |
| 8 | #7 + `--ignore-gpu-blocklist` | — | — | `ANGLE (Mesa, llvmpipe (LLVM 20.1.2 256 bits), OpenGL 4.5)` | — |
| 9 | `--use-angle=swiftshader --enable-unsafe-swiftshader` | — | — | `ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)…))` — классический признак сервера | — |
| 10 | #8 + Mesa driconf `force_gl_vendor`/`force_gl_renderer` + `MESA_GL_VERSION_OVERRIDE=4.6` | — | — | **`ANGLE (AMD, AMD Radeon RX 6600 (radeonsi navi23 LLVM 20.1.2), OpenGL 4.6)`** | — |
| 11 | Реальный дисплей `:1` (AMD Vega) | false | нет | `ANGLE (AMD, AMD Radeon Vega 8 Graphics (radeonsi raven ACO), OpenGL 4.6)`; `screen.avail` 1854×1048 (панель ОС) | — |
| 12 | **Итог: #6 + #10 (Xvfb), из флагов только `--ignore-gpu-blocklist`** | false | нет | как #10 | **dabi: «You are human!» (0 сигналов); sannysoft: 0 fail.** rebrowser (проверено при pipe‑транспорте, от транспорта не зависит): `runtimeEnableLeak`/`sourceUrlLeak` — нет утечек |
| 13 | `Runtime.evaluate` в main world с `document.getElementsByClassName` | — | — | — | rebrowser `mainWorldExecution` 🔴 **обнаружено** |
| 14 | То же через `Page.createIsolatedWorld` (случайное имя мира), без `Runtime.enable` | — | — | — | **не обнаружено**; `window.dummyFn` из изолированного мира недоступен (ожидаемо) |

TLS во всех прогонах — настоящий Chrome 154: JA4 `t13d1517h2_8daaf6152771_cb7bf5808d99`,
HTTP/2 Akamai‑хэш `52d84b11737d980aef856699f885ca86`.

Объяснение #1–#6 (по исходникам Chromium, `content/child/runtime_features.cc`):
runtime‑фича `AutomationControlled` включается при `--enable-automation`,
`--headless`, `--remote-debugging-pipe` и `--remote-debugging-port=0`
(эфемерный порт — так запускает ChromeDriver). Явно заданный порт считается
«подключением отладчика» и флаг **не ставит**. Также с Chrome 136 remote
debugging игнорируется для профиля по умолчанию — нужен свой `--user-data-dir`
(у нас всегда свой).

## 6. Компоненты

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Публичный API: Browser / Context(Profile) / Page / Frame / Locator      │
│  + CLI: doctor · test (scorecard) · profile · run                        │
├──────────────┬──────────────┬──────────────┬─────────────────────────────┤
│ Persona      │ Humanizer    │ Proxy        │ Virtual Desktop             │
│ (идентичность│ (мышь/клавы/ │ Gateway      │ (Xvfb+GLX, WM, workarea,    │
│  + валидатор)│  скролл)     │ (локальный   │  Mesa‑идентичность GPU,     │
│              │              │  форвардер,  │  шрифты)                    │
│              │              │  auth, geo)  │                             │
├──────────────┴──────────────┴──────────────┴─────────────────────────────┤
│ Launcher (флаги, prefs, env TZ/LANG, профиль)                            │
├──────────────────────────────────────────────────────────────────────────┤
│ Quiet‑CDP Client: транспорт, сессии, auto‑attach, изолированные миры,    │
│ политика запрещённых команд, типизированный протокол                     │
└──────────────────────────────────────────────────────────────────────────┘
                 Google Chrome (системный, без модификаций)
```

### 6.1 Quiet‑CDP Client

- Собственный клиент (без Puppeteer/Playwright в контуре управления).
  Транспорт — WebSocket на `127.0.0.1:<порт>` (ADR‑1); обнаружение через
  `/json/version`.
- `Target.setAutoAttach` (`flatten`, `waitForDebuggerOnStart`) для страниц,
  iframe (OOPIF) и воркеров — чтобы применять настройки до первого скрипта.
- **Запрещено:** `Runtime.enable`, `Console.enable`, `Runtime.addBinding` в
  main world, `Page.addScriptToEvaluateOnNewDocument` в main world (фазы 1–2),
  `Page.setBypassCSP`, `Emulation.setDeviceMetricsOverride`/`setUserAgentOverride`
  без полного `userAgentMetadata`, добавление `//# sourceURL`.
- **Выполнение JS:** по умолчанию в изолированном мире (`Page.createIsolatedWorld`
  со случайным именем на каждый фрейм); `DOM.*` + `Runtime.callFunctionOn` по
  `objectId` для работы с узлами; main world — только явный opt‑in с
  предупреждением.
- Ожидания/события: `Page.enable`, `Network.enable` (не наблюдаемы страницей),
  `Page.lifecycleEvent`, собственные стратегии ожидания (load / networkidle /
  селектор / mutation‑тишина).

### 6.2 Launcher

Рецепт запуска (минимальный, проверенный): `--remote-debugging-port=<порт>`,
`--user-data-dir=<профиль>`, `--no-first-run`, `--no-default-browser-check`,
`--password-store=basic`, `--window-size/--window-position` из персоны,
`--lang=<локаль>`; окружение процесса: `TZ=<таймзона>`, `LANG/LC_ALL`;
для виртуального дисплея дополнительно `--ignore-gpu-blocklist` +
`LIBGL_ALWAYS_SOFTWARE=1` + `HOME`/drirc с GPU‑идентичностью. Prefs профиля:
отключить translate‑подсказки, восстановление сессии, политика WebRTC.
Никаких `--enable-automation`, `--headless`, `--disable-blink-features`,
`--test-type`, `--no-sandbox` по умолчанию.

### 6.3 Persona и Profile Store

Персона — декларативная сущность: ОС/платформа, версия Chrome (всегда
= установленной), языки, таймзона, экран (размер, work area, DPR), GPU
(vendor/renderer), набор шрифтов, политика WebRTC, прокси. Правила:
«все значения, которые нельзя подменить ниже JS, берутся с хоста честно»;
валидатор отвергает несогласованные комбинации (Windows‑UA на Linux‑хосте
без шрифтов Windows и т. п.). Профиль = персона + `user-data-dir` + история
использования; хранится на диске, переносим, привязан к прокси.

### 6.4 Proxy Gateway

Локальный форвардер (HTTP CONNECT / SOCKS5) на `127.0.0.1`, который держит
креды upstream‑прокси и отдаёт Chrome анонимный `--proxy-server`. Так мы не
используем `Fetch.enable` для авторизации (перехват влияет на тайминги Resource
Timing), поддерживаем SOCKS5 с auth (Chrome сам его не умеет), можем определять
exit‑IP и гео для таймзоны/локали персоны и блокировать утечки (DNS через
прокси, WebRTC‑политика `disable_non_proxied_udp`).

### 6.5 Virtual Desktop

Для серверов: Xvfb с `+extension GLX` и разрешением из персоны, лёгкий
EWMH‑оконный менеджер (чтобы `document.hasFocus()`, `visibilityState`,
`_NET_WORKAREA`/`screen.avail*` вели себя как на десктопе), Mesa llvmpipe с
driconf‑идентичностью GPU, шрифтовые пакеты (emoji, CJK, базовые). Один Xvfb
на экземпляр браузера. На десктопе — реальный дисплей без изменений.

### 6.6 Humanizer

Кинематическая модель ввода: траектории с кривизной и коррекцией в конце
(Bezier/minimum‑jerk), колоколообразный профиль скорости, лог‑нормальные
паузы, микродрожание, overshoot; клавиатура — пер‑символьные задержки с
зависимостью от биграмм, редкие опечатки с исправлением (opt‑in); скролл —
порциями, с инерцией. Фаза 1 — через `Input.dispatchMouseEvent/KeyEvent`
(события `isTrusted: true`); фаза 2 — OS‑уровневый ввод через XTest на
виртуальном дисплее (устраняет известный артефакт CDP `screenX == pageX`,
crbug 40280325, по которому Turnstile детектит CDP‑клики).

### 6.7 Scorecard (аналог `cloaktest`)

CLI‑команда прогоняет текущую конфигурацию по матрице детекторов и
сравнивает с базовой линией реального Chrome на той же машине:
`bot.sannysoft.com`, `bot-detector.rebrowser.net`,
`deviceandbrowserinfo.com/are_you_a_bot`, `browserscan.net/bot-detection`,
CreepJS, `fingerprint-scan.com`, `pixelscan.net`, `bot.incolumitas.com`,
`browserleaks.com` (canvas/webgl/webrtc/fonts), `tls.peet.ws/api/all`.
Результат — JSON + человекочитаемый отчёт; в CI — регрессионный порог.

## 7. Ключевые решения (ADR, кратко)

- **ADR‑1. Транспорт — TCP‑порт с явным номером на 127.0.0.1, не pipe.**
  Pipe и порт 0 включают `AutomationControlled` ⇒ `navigator.webdriver=true`;
  лечение флагом `--disable-blink-features` даёт инфобар, который гасится лишь
  `--test-type` (лишние, потенциально заметные флаги). Явный порт даёт чистый
  запуск без единого автоматизационного флага (#6). Риск открытого порта
  минимизируем: bind только на loopback, порт из эфемерного диапазона,
  проверка занятости; страницы не могут его опросить из‑за Local Network
  Access‑ограничений Chrome.
- **ADR‑2. Никакого `Runtime.enable`.** Это главная утечка CDP, которой
  пользуются Cloudflare/DataDome. `Runtime.evaluate`, `createIsolatedWorld`,
  `DOM.*`, `callFunctionOn` работают без него (#12, #14).
- **ADR‑3. JS только в изолированном мире.** Main‑world вызовы видны через
  стек (#13). Изолированный мир делит DOM, но не глобалы страницы — для
  парсинга этого достаточно.
- **ADR‑4. Headed‑first, виртуальный рабочий стол на серверах.** Headless в
  Chrome 15x оставляет UA `HeadlessChrome` (в т. ч. в Service Worker), экран
  800×600 без work area, DPR 1; патчить это по кусочкам — гонка.
- **ADR‑5. GPU‑идентичность на уровне драйвера (Mesa driconf), не JS.**
  `force_gl_vendor`/`force_gl_renderer` меняют то, что ANGLE отдаёт в
  `WEBGL_debug_renderer_info`, без хуков (#10). Ограничение: параметры/лимиты
  WebGL и хэши рендера остаются llvmpipe‑овскими (см. риски).
- **ADR‑6. Client Hints неприкосновенны.** Любой UA‑override без полного
  `userAgentMetadata` стирает `sec-ch-ua*` — серверная проверка без JS. В
  headed‑режиме UA не трогаем вовсе.
- **ADR‑7. Ввод: XTest по умолчанию на X‑дисплее, CDP как fallback.** Исходная
  мотивация (`screenX == pageX`) на Chrome 154 не подтвердилась, но XTest
  остаётся строго более подлинным: нет CDP‑трафика `Input.*`, `pressure` и
  тайминги — как у физических устройств (см. раздел 11). См. 6.6.
- **ADR‑8. Честный Linux‑десктоп по умолчанию.** Спуфинг Windows без шрифтов
  Windows — противоречие, которое ловят FingerprintJS/CreepJS. Персона
  «Windows» допускается только при наличии шрифтов и остаётся opt‑in.
- **ADR‑9. Форк Chromium — не в ближайших фазах.** Возможный шаг для canvas/
  audio‑шума и WebGL‑лимитов, если scorecard покажет необходимость.

## 8. Дорожная карта

- **Фаза 0 — каркас (после выбора стека).** Репозиторий, сборка, линтеры,
  тесты, CI. Quiet‑CDP клиент: транспорт, сессии, auto‑attach, типизация
  протокола, политика запрещённых команд (с тестом на утечки по rebrowser).
- **Фаза 1 — MVP парсинга + MCP.** Launcher + Persona (честная, с хоста) +
  Page API (навигация, ожидания, селекторы CSS/XPath/text, извлечение,
  скриншоты, cookies/storage, сетевой перехват на чтение) + изолированные
  миры + Humanizer v1 (CDP) + Proxy Gateway (HTTP/SOCKS5, auth, geo→TZ/locale)
  + Virtual Desktop (Xvfb+GLX, WM, Mesa‑идентичность) + Scorecard CLI +
  минимальный MCP‑сервер поверх библиотеки.
  Критерий выхода: матрица детекторов ≥ базовой линии реального Chrome.
- **Фаза 2 — зрелость стелса.** Сделано: XTest‑ввод и ЙЦУКЕН, прогрев профилей,
  политика WebRTC в профиле, воркер‑контексты (OOPIF, SW), headless‑opt‑in со
  `--screen-info`, Docker‑образ, MCP (вкладки, warmup, connect). Остаётся:
  шрифтовые пресеты шире Noto, загрузки файлов и чтение сетевых ответов в MCP.
- **Фаза 3 — интеграции.** Сделано: мультипрофильный менеджер (`disguise manager`,
  локальное окно со списком и выключателями). Остаётся: HTTP/WS‑демон для других
  языков (при необходимости), наблюдаемость.
- **Фаза 4 — опционально.** Патчи Chromium / сборочный конвейер, если
  scorecard и реальные цели это потребуют.

## 9. Верификация

- Юнит‑тесты клиента (парсинг сообщений, сессии, политика команд).
- Интеграционные тесты против локальных HTML‑фикстур, воспроизводящих
  детекторы (webdriver, Runtime.enable‑утечка, стек main world, Client Hints,
  `screenX==pageX`) — быстро и офлайн.
- Scorecard против публичных детекторов — вручную/по расписанию (они живые и
  меняются; в CI только smoke).
- Базовая линия: тот же Chrome, запущенный вручную на реальном дисплее.

## 10. Риски и ограничения

- **Канвас/аудио‑хэши и WebGL‑лимиты** на llvmpipe не совпадают с заявленным
  GPU. Для Linux‑персоны это правдоподобно (VM/llvmpipe встречаются), но
  продвинутые профилировщики могут кластеризовать. Митигация: честная
  персона на реальном GPU там, где он есть; в перспективе — виртуальный GPU
  (virgl) или патч.
- **Chrome на Linux редок (~2–3 % десктопов)** — сам по себе слабый сигнал,
  зато полностью когерентный. Windows‑персона требует шрифтов Windows (лицензия).
- **Арms race:** любая публичная техника со временем детектится. Поэтому P6
  (измеримость) и минимальный, легко меняемый стелс‑слой важнее «толщины» патчей.
- **XTest печатает ту раскладку, которая загружена в X.** На виртуальном дисплее
  персона с языком `ru` получает `setxkbmap -layout us,ru`; группа переключается
  через `XkbLockGroup` (без аккорда, видимого странице). На чужом дисплее группа
  не трогается, символы вне активной группы идут через `Input.insertText`.
- **Открытый DevTools‑порт** на loopback — поверхность атаки для других
  локальных процессов; на многопользовательских хостах нужен изолированный
  пользователь/контейнер.
- **Лицензии референсов:** nodriver/zendriver — AGPL (только изучать идеи,
  не копировать код); Puppeteer — Apache‑2.0; rod — MIT; протокол CDP — BSD.
- **Юридическое:** инструмент должен применяться с соблюдением ToS целевых
  сайтов и законодательства; это ответственность пользователя.

## 11. Решения владельца проекта и статус реализации

**Принято (2026‑10‑09):**

- *Стек:* **Python 3.12, asyncio, uv**; зависимость времени выполнения — только
  `websockets`. Пакет `disguisebrowser`, CLI `disguise`.
- *Форма потребления:* в первую очередь **библиотека** (скрипты парсинга и
  автоматизации на языке реализации) и **MCP‑сервер** для AI‑агентов. Демон с
  HTTP/WS‑API — не приоритет (возможен позже как тонкая обёртка над библиотекой).
  Следствие: MCP‑сервер входит в фазу 1 (минимальный набор инструментов:
  navigate / snapshot (accessibility‑дерево) / click / type / extract /
  screenshot), а не в фазу 3.
- *Среда эксплуатации:* **и локальный десктоп, и серверы/Docker без GPU**.
  Следствие: Virtual Desktop (Xvfb + GLX + WM + Mesa‑идентичность) — в фазе 1;
  на десктопе используется реальный дисплей.

**Статус v0.1 (реализовано).** Quiet‑CDP клиент с политикой запрещённых
команд; Launcher по рецепту ADR‑1; Persona (честные значения хоста, валидация,
пресеты GPU, детерминированная геометрия по `seed`); Virtual Desktop (Xvfb +
GLX, WM при наличии, Mesa‑идентичность через `DRIRC_CONFIGDIR`); Page API
(навигация по lifecycle‑событиям, изолированные миры со случайным именем,
CSS/XPath, `ElementHandle`, ожидания через MutationObserver внутри изолированного
мира, скриншоты, cookies); Humanizer v1 + CDP‑ввод; scorecard (`disguise test`)
и `disguise doctor`; защита от зомби‑процессов (`PR_SET_PDEATHSIG`). Тесты: 48
юнит + 20 интеграционных с офлайн‑фикстурами детекторов. Живой scorecard на
Xvfb без GPU: 5/5 PASS (sannysoft 0/57, rebrowser без красных, dabi «human»,
browserscan «Normal», TLS = Chrome).

**Статус v0.2 (реализовано поверх v0.1).**

- *Proxy Gateway* (`proxy.py`): локальный SOCKS5‑сервер без auth для Chrome →
  upstream HTTP(S) CONNECT или SOCKS5 с учётными данными. Chrome видит только
  SOCKS‑шлюз: не шлёт `Proxy-*`‑заголовков, резолвит DNS на стороне прокси, креды
  не попадают в командную строку. Geo‑lookup exit‑IP идёт через тот же туннель
  (TLS поверх туннеля через `StreamWriter.start_tls`); `launch(geoip=True)`
  переписывает таймзону и языки персоны по стране exit‑IP.
- *XTest‑ввод* (`input/xtest.py`): события синтезируются X‑сервером, Chrome
  получает их как от физических устройств; бэкенд по умолчанию на X‑дисплее,
  CDP остаётся fallback. Маппинг CSS‑координат в экранные — через
  `window.screenX/Y` и разницу outer/inner размеров окна; перед вводом вкладка
  активируется (`Page.bringToFront`).
- *EWMH‑рабочая область* (`display.publish_work_area`): без оконного менеджера
  публикуем `_NET_SUPPORTED` + `_NET_WORKAREA` + `_NET_CURRENT_DESKTOP` на root —
  Chrome честно отдаёт `screen.availHeight < screen.height` и `availTop`.
  `_NET_ACTIVE_WINDOW` намеренно не объявляем, чтобы Chrome фокусировал окно сам.
- *Несколько браузеров в одном процессе*: номера дисплеев резервируются
  процесс‑локально, старт Xvfb повторяется при межпроцессной гонке; проверено
  тремя параллельными `launch()`.
- *MCP‑сервер* (`mcp_server.py`, extra `mcp`): сессии `browser_launch → session_id`,
  accessibility‑snapshot с `[ref=eN]` (`snapshot.py`, через
  `Accessibility.getFullAXTree` + `DOM.resolveNode`), инструменты навигации,
  ввода, извлечения и вкладок; stdio и Streamable HTTP.

**Измерено про ввод (Chrome 154, X11, Xvfb).** Артефакт `screenX == pageX`
CDP‑событий (crbug 40280325) в headed‑режиме не воспроизводится. Различия,
которые остались: CDP `pointerdown.pressure == 0` против `0.5` у XTest/физической
мыши; реальный щелчок колеса даёт `wheel.deltaY = 120` (CDP‑бэкенд калиброван на
это значение). Это и является обоснованием XTest по умолчанию (уточнение ADR‑7).

**Статус v0.3 (реализовано поверх v0.2).**

- *ЙЦУКЕН:* `Persona.keyboard_layout()` → `us,ru`, когда язык `ru`. Виртуальный
  дисплей вызывает `setxkbmap`; XTest блокирует группу через `XkbLockGroup` и
  сверяет keysym уровня 0. Если группы нет, символ уходит в `Input.insertText`.
- *Прогрев:* `Browser.warmup()` / `disguise warmup` открывает несколько устойчивых
  публичных сайтов, скроллит и ждёт. При `seed` набор сайтов детерминирован.
- *Воркеры и OOPIF:* auto‑attach включается и на сессии вкладки (только он
  отдаёт выделенные воркеры и cross‑site iframe), с `waitForDebuggerOnStart`.
  iframe получает `Page.enable` и резюмируется; изолированный мир создаётся
  лениво при первом `Browser.evaluate_target()` — до коммита документа
  `Page.createIsolatedWorld` не отвечает, а коммит невозможен на паузе.
  Воркеры резюмируются без `Runtime.enable`. `Page.frames()` дополняет дерево
  родителя OOPIF‑целями, которых в нём нет.
- *Калибровка указателя:* XTest переводит CSS‑координаты в экранные по
  реальному `mousemove` (слушатель в изолированном мире, принимается только
  событие с совпадающими `screenX/Y`), а не по `outerHeight − innerHeight`:
  нижняя рамка openbox давала промах в 4 px.
- *`connect(port=…)`* присоединяется к уже запущенному Chrome на loopback и при
  `close()` только рвёт CDP, не останавливая процесс.
- *Панель:* при наличии `tint2` и оконного менеджера виртуальный дисплей поднимает
  нижнюю панель, чтобы WM опубликовал `_NET_WORKAREA` короче экрана.
- *Headless:* к opt‑in добавлен `--screen-info` из персоны.
- *Docker:* `Dockerfile` (Ubuntu 24.04, Chrome stable, Xvfb, Mesa, openbox, tint2,
  шрифты, непривилегированный пользователь). Весь набор тестов (99, включая
  интеграционные на реальном Chrome) и живой scorecard прогнаны изнутри образа:
  Chrome 155, 5/5 PASS. Контейнеру нужен `--security-opt seccomp=unconfined`
  для штатной песочницы Chrome; `--no-sandbox` не используется.

**Статус v0.4 (ветка `camoufox`, второй движок).**

- *Решение.* Camoufox не заменяет Chrome‑ядро, а подключается вторым движком: цель —
  множество долгоживущих личностей (профили, как у CloakBrowser), для которых не нужен
  именно Chrome. Quiet‑CDP остаётся единственным путём для Chrome; у Camoufox свой
  транспорт (Playwright 1.62 по Juggler), и «тишина» там обеспечивается самой сборкой.
- *Общий контракт.* `protocols.py` задаёт `BrowserLike` / `PageLike` / `ElementLike`
  (структурные `Protocol`). MCP‑сервер, scorecard, warmup и capture типизированы по ним
  и не знают о движке; `LaunchHandle[T]` обобщён по типу браузера. `_prepare_proxy`
  (шлюз, geo‑выравнивание, креды вне командной строки) переиспользован без копирования.
- *Профиль* (`camoufox/profiles.py`): `profile.json` (схема 1, `0600`, атомарная запись)
  + `user-data/` Firefox. Идентичность — словарь fpgen или готовый пресет; Camoufox
  хеширует закреплённый словарь в соль всех зависимых величин (шрифты, голоса, GPU,
  медиаустройства, аудио), поэтому при первом запуске словарь прогоняется через
  JSON‑круг, чтобы структура совпадала с последующими. `update` не пересоздаёт устройство.
  Один профиль — один браузер: `flock`, который держится всё время жизни браузера и
  освобождается при ошибке старта.
- *Запуск* (`camoufox/browser.py`): `launch()` собирает `_Request` и проходит шаги
  `resolve_profile → open_directory → prepare_network → resolve_display → build_options`
  (последний — чистая функция). Эфемерный запуск использует временный каталог и
  удаляет его при закрытии. Таймзона, локаль, геолокация и WebRTC выравниваются по
  exit‑IP; явные значения профиля важнее geo‑умолчаний.
- *Scorecard:* у `Detector` появились `waive` по движкам: заведомо неприменимые
  проверки (sannysoft «Chrome (New)» на Firefox) выводятся как `n/a for this engine`,
  а не как провал и не скрываются.
- *Измерено на сборке 156.0.1‑beta.36:* закреплённый профиль воспроизводит все
  детерминированные поля между запусками (меняется только PNG‑хэш `toDataURL`);
  localStorage и cookies (с `expires`) переживают перезапуск; UA получает версию
  движка, а не версию из профиля; хэши пикселей canvas одинаковы у разных профилей
  на одной машине; TLS/JA4 принадлежит Firefox. Из этого следует граница применимости,
  описанная в README: профили не изолируют машину.
- *Docker:* `Dockerfile.camoufox` (без Chrome). Образ скачивает официальный Camoufox,
  либо архив своей сборки, если задан `CAMOUFOX_DIST_URL`. GeoIP и модель fpgen
  скачиваются при сборке; профили на томе `/data`. Каталог `camoufox-dist/` в git не хранится.

**Дальше (фаза 3):** HTTP/WS‑демон при необходимости, наблюдаемость. Мультипрофильный
менеджер уже есть: `disguise manager`. Патчи Chromium — только если scorecard покажет, что хэши llvmpipe
мешают на реальных целях.

Сравнение стеков, на основании которого принято решение (библиотека + MCP,
десктоп + сервер):

| Критерий | Python 3.12 (asyncio, uv) | TypeScript (Node 22) | Go 1.27 |
|---|---|---|---|
| Библиотека для скриптов парсинга | лучший выбор: язык парсинга «по умолчанию», asyncio + sync‑фасад | хорошо; Playwright‑подобный API привычен JS‑разработчикам | многословно для скриптов |
| MCP‑сервер | официальный SDK `mcp` (FastMCP), зрелый | эталонная экосистема MCP (`@modelcontextprotocol/sdk`), поставка через `npx` | официальный Go SDK моложе остальных |
| Типизация CDP | генерируем из protocol JSON (есть `pycdp`, MIT) | официальный пакет `devtools-protocol` — без усилий | генерируем; референс — `rod/lib/proto` (MIT) |
| XTest‑ввод (фаза 2, Xvfb) | `python-xlib` — чистый Python, XTest поддержан | зрелой pure‑JS реализации нет; через `xdotool` или внешний помощник | `jezek/xgb` + xtest — чистый Go |
| Proxy Gateway, оркестрация Xvfb/WM | достаточно | достаточно | лучше всего (горутины, один бинарник) |
| HTTP‑fallback с TLS Chrome | `curl_cffi` — сильнейший вариант | слабее | `utls` — хорошо |
| Поставка пользователям | `uv tool install` / pip, нужен Python | npm / `npx -y` (норма для MCP), нужен Node | один статический бинарник |
| Референсы с пермиссивной лицензией | `pycdp`; идеи nodriver/zendriver (AGPL — только изучать) | Puppeteer (Apache‑2.0), chrome‑remote‑interface (MIT) | rod, chromedp (MIT) |

Выбран **Python**: основной сценарий — скрипты парсинга на Python; `python-xlib`
покрывает XTest для фазы 2; `curl_cffi` даёт HTTP‑fallback с TLS Chrome;
официальный MCP SDK для Python зрелый.

## Источники

- CloakHQ/CloakBrowser — README, issue #105 (патчи закрыты) — github.com/CloakHQ/CloakBrowser
- I. Paterson, «Anti‑Detect Browser Benchmark 2026: 7 Tools, 651 Verdicts» (май 2026)
- I. Ur Rahim, «I Tested 7 Stealth Browsers Against 6 Detection Pages» (сентябрь 2026)
- rebrowser/rebrowser-patches — описание утечки `Runtime.enable`; bot-detector.rebrowser.net
- Chrome for Developers, «Changes to remote debugging switches to improve security» (Chrome 136)
- Chromium `content/child/runtime_features.cc` — условия включения `AutomationControlled`
- crbug 40280325 — `Input.dispatchMouseEvent` выставляет `screenX/Y = x/y`; CDP‑Patches (Vinyzu)
- Mesa driconf: опции `force_gl_vendor`, `force_gl_renderer`; `MESA_GL_VERSION_OVERRIDE`
