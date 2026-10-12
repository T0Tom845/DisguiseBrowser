# План форка Chromium

Статус: проектный документ, реализация не начата. Активирует ADR‑9 из
[`ARCHITECTURE.md`](ARCHITECTURE.md): форк нужен, потому что три класса сигналов
из [`SIGNALS.md`](SIGNALS.md) (L3 «одинаковые хэши у всех сессий», «имя GPU
противоречит параметрам», «платформа Linux») принципиально не закрываются
ниже JavaScript без изменения самого браузера.

Пути к файлам Chromium указаны как ориентиры на M15x; перед реализацией каждый
сверяется по чекауту нужного тега.

## 1. Что меняется в проекте, а что нет

Форк — это **другой бинарник** в `launcher.find_chrome()` плюс один новый канал
конфигурации. Всё остальное остаётся и нужно ровно так же:

| Слой | С форком |
|---|---|
| L0 сеть, Proxy Gateway | без изменений |
| L1 транспорт (TLS/H2) | без изменений: форк собран из того же кода, JA4 тот же |
| L2 протокол управления (quiet‑CDP, изолированные миры) | без изменений; форк может **дополнительно** снять ограничения (раздел 5.8) |
| L3 JS‑окружение и железо | **закрывается форком** — предмет этого документа |
| L4 когерентность (Persona, валидатор) | расширяется: Persona порождает `FingerprintConfig` (раздел 4) |
| L5 поведение (Humanizer, XTest) | без изменений |
| L6 история, профили, warmup | без изменений; профиль дополнительно хранит `FingerprintConfig` |
| Virtual Desktop (Xvfb, WM, Mesa) | без изменений; GPU‑идентичность переезжает из driconf в конфиг форка |

## 2. Цели и не‑цели форка

**Цели (в порядке приоритета).**

- **G1. Паритет с Google Chrome.** Собранный нами Chromium со стороны страницы и
  сервера неотличим от Google Chrome stable той же версии на том же Linux.
  Без этого всё остальное бессмысленно: Chromium выдаёт себя брендами Client
  Hints, отсутствием H.264/AAC и Widevine.
- **G2. N различимых и стабильных устройств с одного хоста.** Каждый профиль
  получает собственные, воспроизводимые между сессиями хэши canvas/WebGL/audio,
  своё число ядер и объём памяти, свой набор параметров WebGL, согласованный
  с заявленным GPU.
- **G3. Не‑Linux персоны, в первую очередь Windows 11.** UA, Client Hints,
  `navigator.platform`, шрифты и их метрики, голоса, кодеки, WebGPU, скроллбары
  — всё согласовано с Windows‑референсом.
- **G4 (опционально). Удобство автоматизации.** Освободиться от ограничений,
  вокруг которых построен текущий рецепт запуска: `navigator.webdriver` при
  pipe‑транспорте, артефакты CDP‑ввода, «нестелсовый» headless.

**Не‑цели.**

- Не делаем UI‑продукт; форк не имеет своих настроек, страниц, брендинга.
- Не трогаем контур управления: Puppeteer/Playwright‑совместимость, собственные
  CDP‑домены для скриптинга — нет.
- Не патчим то, что уже решается флагами, prefs, окружением или fontconfig
  (DPR через `--force-device-scale-factor`, таймзона через `TZ`, шрифты через
  `FONTCONFIG_FILE`, лимит кучи V8 через `--js-flags`). Каждый патч должен
  обосновать, почему его нельзя сделать снаружи.
- Не решаем CAPTCHA, не обходим поведенческий анализ.
- macOS‑персона — после Windows; шрифты Apple нераспространяемы, Metal‑строки
  WebGL и overlay‑скроллбары — отдельная работа.

## 3. Принципы патчей

- **F1. Детерминизм по профилю.** Все подменяемые значения и весь шум —
  функция `(seed профиля, входные данные)`. Одно и то же содержимое canvas
  даёт одинаковый результат при повторном чтении, в iframe, в воркере, в
  OffscreenCanvas и после перезапуска. Детекторы проверяют именно
  нестабильность («canvas lies», «fingerprint instability»), а не сам шум.
- **F2. Шум — только там, где у реальных устройств есть энтропия** (пиксели
  canvas/WebGL‑рендера, сэмплы аудио). Там, где у устройства фиксированное
  значение (лимиты WebGL, число ядер, версия платформы), — фиксированное
  значение из референса, никакого рандома.
- **F3. Значения — из базы реальных устройств, а не генератор.** Набор
  UA + CH + GPU‑строки + параметры WebGL + расширения + кодеки + голоса + шрифты
  снимается с настоящих машин одним инструментом (раздел 6.1) и применяется
  целиком. Нельзя сочетать «RTX 3060» с лимитами Intel.
- **F4. Только C++ ниже биндингов.** Никаких инъекций JS, V8‑расширений,
  изменений прототипов. `Function.prototype.toString`, дескрипторы свойств,
  порядок ключей — нативные по построению.
- **F5. Патч = один файл diff + одна фикстура.** Каждый патч живёт отдельным
  файлом в серии, у него есть офлайн‑HTML‑фикстура в этом репозитории,
  доказывающая эффект и детерминизм. Патч без фикстуры не принимается.
- **F6. Честная версия, не гонка за stable.** Собираем конкретный тег Chrome и
  сообщаем ровно его: UA, `fullVersionList` и поведение этой версии, никогда
  «новее, чем собрано». Отставание на 2–3 мажора (примерно квартал) — нормальная
  популяция реальных пользователей и не считается проблемой; дальше — повод
  обновиться, когда удобно, а не по календарю (см. раздел 7).
- **F7. Конфигурация невидима странице.** Канал конфигурации — командная
  строка/файл, читается в browser‑процессе, пробрасывается в renderer/GPU
  через существующие механизмы. Ни `chrome://flags`, ни JS‑API, ни новых
  CDP‑команд, доступных из страницы.
- **F8. Сначала измерение, потом патч.** Любая строка плана ниже со статусом
  «проверить» закрывается снятием референса (раздел 6.1), а не предположением.

## 4. Модель конфигурации: `FingerprintConfig`

Python‑сторона (`Persona` + база устройств) порождает JSON и передаёт путь
флагом `--disguise-config=<файл>`. Browser‑процесс читает, валидирует,
сериализует и пробрасывает в дочерние процессы через командную строку
(allowlist в `RenderProcessHostImpl::PropagateBrowserCommandLineToRenderer`
и аналог для GPU‑процесса). В renderer — синглтон, инициализируемый до
создания первого контекста Blink. Без флага форк ведёт себя как обычный
Chromium с патчами паритета (G1) — это и есть режим «честный Linux».

```jsonc
{
  "schema": 1,
  "seed": "u64",                      // корень всего шума и выбора
  "device_profile_id": "win11-nvidia-rtx3060-dw",   // ссылка на запись базы устройств
  "platform": {                       // раздел 5.1
    "os": "windows", "ua_platform": "Windows NT 10.0; Win64; x64",
    "ch_platform": "Windows", "ch_platform_version": "15.0.0",
    "ch_architecture": "x86", "ch_bitness": "64", "ch_wow64": false,
    "navigator_platform": "Win32"
  },
  "hardware": { "hardware_concurrency": 12, "device_memory": 8 },  // 5.2
  "webgl": {                          // 5.3
    "vendor": "Google Inc. (NVIDIA)",
    "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3623)",
    "params": { "MAX_TEXTURE_SIZE": 16384, "...": "..." },
    "precision": { "VERTEX_SHADER.HIGH_FLOAT": [127, 127, 23], "...": "..." },
    "extensions_webgl1": ["..."], "extensions_webgl2": ["..."],
    "readback_noise": { "amplitude": 1, "density": 0.002 }
  },
  "webgpu": { "info": { "vendor": "nvidia", "architecture": "ampere", "device": "", "description": "" },
              "limits": { "...": "..." }, "features": ["..."] },
  "canvas":  { "readback_noise": { "amplitude": 1, "density": 0.002 } },        // 5.4
  "audio":   { "noise": { "amplitude": 1e-7 }, "sample_rate": 48000 },           // 5.5
  "media":   { "hevc_decode": true, "av1_decode": true, "h264_hw": true },        // 5.7
  "speech_voices": [ { "name": "Microsoft David - English (United States)", "lang": "en-US", "local": true, "default": true }, "..." ],
  "ui": { "scrollbar_width": 17, "overlay_scrollbars": false }                   // 5.6
}
```

Правила валидации конфигурации (Python, до запуска): все поля одной записи базы
устройств применяются вместе; `os=windows` требует установленного пакета
шрифтов Windows‑персоны (раздел 5.6) и `sample_rate=48000`; `hardware_concurrency`
∈ множества, встречающегося у этой модели GPU в референсах; `device_memory`
∈ {4, 8} (API сам округляет); лимиты WebGL не превышают возможностей
нашего реального GL‑стека там, где это ломает рендер (см. 5.3).

## 5. Каталог патчей

Для каждой области: что проверяют детекторы, что меняем, где, какие ловушки
согласованности, чем доказываем. «Вне форка» — означает, что область
обязательна для цели, но делается снаружи (fontconfig, prefs, флаги, образ).

### 5.1. Идентичность платформы: UA, Client Hints, `navigator.platform`

| Что видно | Где в Chromium (ориентир) | Что делаем |
|---|---|---|
| Заголовок `User-Agent`, `navigator.userAgent`/`appVersion` | `content/common/user_agent.cc` (`GetUnifiedPlatform`, `BuildUserAgentFromProduct`, `GetReducedUserAgent`) | платформенная часть из конфига; версия — настоящая версия сборки |
| `Sec-CH-UA`, `Sec-CH-UA-Platform`, `-Mobile` (шлются всегда) и high‑entropy (`platformVersion`, `architecture`, `bitness`, `wow64`, `fullVersionList`, `formFactors`, `model`) | `components/embedder_support/user_agent_utils.cc` (`GetUserAgentMetadata`, `GetPlatformForUAMetadata`, `GetPlatformVersionForUAMetadata`, `GenerateBrandVersionList`) | все поля из конфига; **бренд `Google Chrome`** в списке даже без `is_chrome_branded` (часть G1) |
| `navigator.userAgentData` | `third_party/blink/renderer/core/frame/navigator_ua_data.cc` | берёт метаданные из той же функции — проверить, что нет второго источника |
| `navigator.platform` | `third_party/blink/renderer/core/frame/navigator_id.cc` (compile‑time `#if`, на Linux — `uname`) | из конфига (`Win32` / `Linux x86_64` / `MacIntel`) |
| UA в воркерах, Service Worker, `fetch` из воркера | тот же `GetUserAgent()` через `ContentRendererClient` | проверить фикстурой, что воркеры и SW видят то же |
| `Sec-CH-UA-Platform-Version` для Windows 11 | `13.0.0`+ означает Win 11 | из референса |

Ловушки: `platformVersion` Windows ↔ Fluent‑скроллбары ↔ `navigator.userAgentData.platform`
↔ шрифты (Segoe UI Variable появился в Win 11) — всё из одной записи базы.
Поля `architecture/bitness` должны совпадать с реальной сборкой Chrome для
этой платформы (`x86`/`64`, не `arm`).

Проверка: фикстура сравнивает заголовки (через `Network.requestWillBeSentExtraInfo`
на локальном сервере фикстур) и JS‑значения с записью базы; главный фрейм,
iframe, dedicated/shared/service worker.

### 5.2. Железо: ядра, память

| Что видно | Где | Что делаем |
|---|---|---|
| `navigator.hardwareConcurrency` (окно и воркеры) | `third_party/blink/renderer/core/frame/navigator_concurrent_hardware.cc` → `base::SysInfo::NumberOfProcessors()` | возвращаем значение конфига **только в Blink‑API**; `SysInfo` не трогаем, чтобы не менять планировщики |
| `navigator.deviceMemory` и заголовок `Device-Memory` | `third_party/blink/public/common/device_memory/approximated_device_memory.cc`; заголовок считается в `content/browser/client_hints/` тем же классом | одна точка подмены покрывает оба |
| `performance.memory.jsHeapSizeLimit` ↔ `deviceMemory` | лимит кучи V8 от физической памяти | **вне форка**: `--js-flags=--max-old-space-size=…` из конфига; проверить референсом типичное значение (на 64‑бит обычно ≈4 ГБ) |

Ловушка: истинное число ядер измеримо таймингом параллельных воркеров
(`SharedArrayBuffer`/`Atomics`, или просто N воркеров × тяжёлый цикл).
Поэтому заявленное число ядер должно быть **≤ квоты cgroup** контейнера, а
лучше равно ей; персоны с 16+ ядрами — только на хостах, где это есть.

### 5.3. WebGL и WebGPU

| Что видно | Где | Что делаем |
|---|---|---|
| `UNMASKED_VENDOR/RENDERER_WEBGL` | `third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc::getParameter` (ветка `WebGLDebugRendererInfo`) | строки из конфига; Mesa driconf больше не нужен |
| `getParameter(MAX_*)`, `ALIASED_*_RANGE`, `MAX_TEXTURE_MAX_ANISOTROPY_EXT`, WebGL2‑лимиты | там же + `webgl2_rendering_context_base.cc` | табличная подмена по конфигу |
| `getShaderPrecisionFormat` | там же | таблица из референса (D3D11 даёт `[127,127,23]`‑подобные, llvmpipe — другие) |
| `getSupportedExtensions` / `getExtension` | там же + `extensions_util` | **скрываем** расширения, которых нет у целевого GPU (безопасно); **добавляем** только те, что реально поддерживает наш GL‑стек; остальное — несовпадение фиксируется и либо принимается как остаточный риск, либо персона отвергается валидатором |
| `getContextAttributes`, `drawingBufferColorSpace`, `MAX_DRAW_BUFFERS` | там же | из референса |
| Хэш рендера (`readPixels`, `toDataURL` с WebGL‑канваса, `drawImage(webglCanvas)`→2D) | `readPixels` в том же файле; остальные пути — через 5.4 | детерминированный шум при чтении (общий механизм с canvas 2D) |
| `navigator.gpu.requestAdapter()` → `adapter.info` (`vendor`, `architecture`, `device`, `description`), `limits`, `features`, `isFallbackAdapter` | `third_party/blink/renderer/modules/webgpu/gpu_adapter.cc`, `gpu_supported_limits.cc`; бэкенд Dawn | **проверить**, что `navigator.gpu` вообще есть в Linux Chrome 154 и в каком виде; на Windows есть с M113 — для Windows‑персоны адаптер обязан существовать. Бэкенд — Dawn/Vulkan поверх lavapipe или SwiftShader; `isFallbackAdapter=false`, `info`/`limits`/`features` из референса |

Ловушки:
- Лимиты выше реальных (например, `MAX_TEXTURE_SIZE=32768` у NVIDIA при 16384 у
  llvmpipe) ломают реальную аллокацию — детекторы этого почти не делают, но
  WebGL‑приложения на целевом сайте могут. Правило: заявляем только лимиты, не
  превышающие реальные, иначе персона отвергается. Отсюда предпочтение
  персонам с **интегрированной графикой** (Intel Iris Xe/UHD, AMD Radeon
  Graphics): их лимиты и производительность ближе к llvmpipe (см. риски).
- Производительность: время компиляции шейдера и кадра на llvmpipe на порядок
  хуже RTX. Это измеримо (`performance.now()` вокруг `drawArrays`+`finish`).
  Та же митигация — не заявлять дискретные топовые GPU.
- Vendor/device ID в hex внутри строки renderer должны соответствовать модели —
  только из референса.

Проверка: фикстура снимает полный дамп WebGL1/2 (все `getParameter`, все
форматы точности, расширения, атрибуты) и сравнивает с записью базы
побайтно; второй дамп из воркера через OffscreenCanvas.

### 5.4. Canvas 2D: детерминированный шум чтения

Дизайн шума:
- Вход: пиксели буфера на момент чтения. Выход: те же пиксели с изменением
  младшего бита нескольких каналов в небольшой доле пикселей (порядок 0,1–0,5 %).
- Позиции и знак изменения — PRNG, засеянный `hash(seed профиля, hash(содержимого))`.
  Поэтому одно содержимое → один результат (стабильность), разное содержимое
  → независимый шум, разные профили → разные хэши. Один профиль на разных
  сайтах даёт одинаковый хэш, как настоящее устройство.
- Шум не применяется к пустому/однотонному буферу (у реального устройства
  белый canvas читается точно; детектор CreepJS проверяет именно это).
- Текстовые метрики (`measureText`, `getBoundingClientRect` текста) **не**
  шумятся: они должны быть детерминированной функцией шрифтов (5.6).

Пути чтения, каждый из которых обязан проходить через один и тот же
`PerturbPixels()` (список поддерживается как часть теста; появление нового
пути в Chromium = регрессия):

| Путь | Где (ориентир) |
|---|---|
| `canvas.toDataURL()`, `toBlob()` | `third_party/blink/renderer/core/html/canvas/html_canvas_element.cc` (`ToDataURLInternal`, `toBlob`) |
| `ctx.getImageData()` | `modules/canvas/canvas2d/base_rendering_context_2d.cc` |
| `OffscreenCanvas.convertToBlob()`, `transferToImageBitmap()` | `core/offscreencanvas/offscreen_canvas.cc` |
| `createImageBitmap(canvas)` → чтение | `core/imagebitmap/image_bitmap.cc` |
| `ctx2.drawImage(canvas1)` → чтение из `canvas2` | покрывается чтением из `canvas2`, но проверить, что шум не накапливается «дважды» заметно |
| `gl.texImage2D(canvas)` → `readPixels` | через 5.3 |
| `canvas.captureStream()` → `MediaRecorder`/`<video>` → canvas | `CanvasCaptureHandler`; низкий приоритет, зафиксировать как известный непокрытый путь до измерения |

Референс реализации: Brave «farbling» (`brave/chromium_src/third_party/blink/renderer/...`,
MPL‑2.0 — файловый copyleft, портировать можно с сохранением заголовков).
Отличие нашего дизайна: сид по профилю, а не по (сессия, origin).

Проверка: фикстура рисует эталонную сцену FingerprintJS/BrowserLeaks, читает
её всеми путями в окне, iframe, воркере, сравнивает: (а) все пути дают один
хэш; (б) повторное чтение — тот же хэш; (в) после перезапуска того же профиля
— тот же; (г) другой seed — другой; (д) доля изменённых пикселей и амплитуда
в заданных границах; (е) белый canvas читается без изменений.

### 5.5. Аудио

| Что видно | Где | Что делаем |
|---|---|---|
| Хэш `OfflineAudioContext` (осциллятор → компрессор → `getChannelData`) | `third_party/blink/renderer/modules/webaudio/audio_buffer.cc` (`getChannelData`, `copyFromChannel`) | детерминированный шум по тому же принципу, что 5.4: функция `(seed, содержимое)`, амплитуда ≈1e‑7 |
| `AnalyserNode.getFloat/ByteFrequencyData`, `*TimeDomainData` | `modules/webaudio/analyser_node.cc`, `realtime_analyser.cc` | то же |
| `AudioContext.sampleRate`, `baseLatency`, `outputLatency`, `destination.maxChannelCount` | берутся от аудиоустройства | **вне форка**: null‑sink PipeWire в образе с `clock.rate` из конфига (Windows — 48000); `maxChannelCount=2` |

Ловушка: детекторы прогоняют один и тот же граф дважды и в воркере
(`OfflineAudioContext` в воркере недоступен — только окно/iframe) — результаты
обязаны совпадать бит в бит между вызовами и отличаться между профилями.

### 5.6. Шрифты, метрики текста, элементы UI — в основном вне форка

Это самый сильный классификатор ОС у CreepJS/FingerprintJS и самая слабая
часть любой «Windows с Linux» истории. Форк здесь почти не нужен; нужен
**пакет шрифтов персоны** и правильный fontconfig на процесс.

| Что видно | Что делаем |
|---|---|
| Список шрифтов через измерение ширин, `document.fonts.check`, `queryLocalFonts()` | свой `FONTCONFIG_FILE` на процесс: только шрифты персоны; Linux‑шрифты (DejaVu, Liberation под их именами, Noto Sans*) скрыты от матчинга под своими именами |
| Шрифты Windows: Arial, Times New Roman, Courier New, Calibri, Cambria, Segoe UI, Tahoma, Verdana, Georgia, Consolas, Segoe UI Emoji… | два варианта пакета: (1) **оригиналы**, которые пользователь приносит со своей лицензии — предпочтительно; (2) метрически совместимые свободные клоны через fontconfig‑алиасы: Liberation (Arial/Times/Courier), Carlito (Calibri), Caladea (Cambria), Selawik (Segoe UI, MIT), Gelasio (Georgia), шрифты Wine (Tahoma, LGPL). Клоны дают правильные **ширины**, формы глифов другие — но хэш рендера и так маскируется шумом 5.4 |
| Emoji: ширина/высота и форма Segoe UI Emoji | клона нет. Остаточный риск, фиксируем явно; измерить референсом, насколько CreepJS/др. по нему классифицируют |
| Доли пикселя в `measureText`/`getBoundingClientRect` (DirectWrite с subpixel positioning vs FreeType с хинтингом) | **проверить измерением**: fontconfig `hintstyle=hintnone`, `antialias`, `rgba`, и subpixel positioning у Skia/Linux должны дать ширины, равные линейным advance‑ам шрифта, как у DirectWrite. Если расхождение остаётся — патч `gfx::FontRenderParams`/Skia на Linux (единственное место, где форк может понадобиться в этом разделе) |
| Шрифты по умолчанию (`standard`, `serif`, `sansserif`, `fixed`), `system-ui` | prefs `webkit.webprefs.fonts.*` (Times New Roman/Arial/Consolas) + fontconfig‑алиас `system-ui`→Segoe UI |
| Ширина скроллбара (`innerWidth − clientWidth`), Fluent‑скроллбары Win11 | **форк**: `ui/native_theme/native_theme_base.cc` / `blink/renderer/core/scroll/scrollbar_theme_aura.cc` — толщина из конфига (Windows классич. 17 px, Linux 15 px); включение Fluent‑темы по конфигу — **проверить референсом**, что именно видно странице |
| `outerHeight − innerHeight`, `outerWidth − innerWidth` (высота хрома окна) | **вне форка**: измерить на референсе Windows при DPR 1/1.25/1.5 и подогнать `browser.custom_chrome_frame` + размер окна; `_NET_WORKAREA` с высотой панели Windows (40/48 px снизу) |

Проверка: фикстура «метрики текста» — набор строк × набор шрифтов →
`measureText().width`, `actualBoundingBox*`, ширина `<span>`; сравнение с
референсом Windows побайтно (не с допуском).

### 5.7. Медиа: кодеки, Widevine, устройства, голоса

| Что видно | Где | Что делаем |
|---|---|---|
| `canPlayType`/`MediaSource.isTypeSupported` для H.264/AAC/MP3 | сборка | **GN:** `proprietary_codecs=true`, `ffmpeg_branding="Chrome"` — иначе отличие от Chrome мгновенное (G1) |
| `requestMediaKeySystemAccess('com.widevine.alpha')` | сборка + CDM | **проверить**: `enable_widevine=true` и загрузка CDM через component updater в небрендированной сборке; если недоступно — Widevine остаётся известным расхождением v1, если цели не стриминговые |
| `MediaCapabilities.decodingInfo` (HEVC, AV1, `powerEfficient`, `smooth`) для GPU персоны | `media/base/supported_types.cc`, `media/mojo/.../media_capabilities` | таблица по конфигу: Windows с современным GPU декодирует HEVC аппаратно → `supported=true`; Linux‑референс — нет |
| `RTCRtpSender.getCapabilities('video')` (аппаратный H.264 в WebRTC) | `third_party/webrtc` + `media/video/gpu_video_accelerator_factories` | низкий приоритет; снять референс, решить по факту |
| `enumerateDevices()` — число устройств по `kind` | аудиостек | **вне форка**: PipeWire null‑sink + null‑source в образе; `videoinput` — по персоне (ноутбук ↔ есть камера) через `--use-fake-device-for-media-stream` или virtual v4l2 — **проверить**, что fake‑устройства не видны иначе как по счётчику |
| `speechSynthesis.getVoices()` | `content/browser/speech/tts_platform_impl_linux.cc`, `tts_controller_impl.cc`; сетевые голоса «Google …» — компонентное расширение `chrome/browser/resources/network_speech_synthesis` | **форк**: платформенные голоса из конфига (Windows: Microsoft David/Zira/Mark и т. п., `localService=true`); **проверить**, появляются ли сетевые голоса Google в нашей сборке без API‑ключей, и воспроизвести список референса |

### 5.8. Поверхность автоматизации (G4, опционально)

| Сигнал | Где | Что даёт |
|---|---|---|
| `navigator.webdriver` при `--remote-debugging-pipe` / порте 0 | `content/child/runtime_features.cc` (`AutomationControlled`) | pipe‑транспорт вместо TCP‑порта: снимает риск открытого порта на loopback; упрощает `connect` |
| CDP‑ввод: `pressure=0`, `screenX==pageX` (crbug 40280325) | `content/browser/devtools/protocol/input_handler.cc` | CDP‑ввод неотличим от физического → XTest перестаёт быть обязательным (Wayland, контейнеры без X) |
| Headless: `Headless` в UA, 800×600, нет work area, `AutomationControlled` | `headless/`, `content/child/runtime_features.cc`, `--screen-info` | «стелс‑headless» = Docker без Xvfb/WM/tint2. Отдельно оценить, сколько остаточных отличий у headless (фокус, `window.open`, `outerHeight==innerHeight`) — может оказаться дороже, чем Xvfb |
| `Runtime.enable`‑утечка | V8 inspector | **не делаем**: сложно, quiet‑CDP уже решает |

### 5.9. Паритет с Google Chrome: контрольный список (G1)

Снимается диффом «наша сборка vs системный Google Chrome на том же Linux»
инструментом 6.1. Известные заранее расхождения небрендированной сборки:

- бренды в `Sec-CH-UA`/`userAgentData.brands` (`Chromium` вместо `Google Chrome`);
- проприетарные кодеки; Widevine;
- сетевые голоса TTS (проверить);
- `chrome://` страницы и API‑ключи Google — странице не видны, игнорируем;
- PGO/оптимизации: `is_official_build=true` + профили PGO, чтобы тайминги JS не
  кластеризовали «медленную сборку» (низкий приоритет, но бесплатно).

Гейт фазы F1: дифф пуст по всем страничным наблюдаемым.

## 6. Инструменты и тесты

### 6.1. Снятие референса и дифф (`disguise capture` / `disguise diff`)

Главный инструмент всего плана; делается **до** первого патча.

- `capture`: страница‑сборщик (одна HTML + JS, без зависимостей), открывается
  в любом браузере — на реальной Windows‑машине, на Mac, в системном Chrome на
  Linux, в нашем форке. Собирает JSON: UA/CH (через echo‑эндпоинт),
  `Object.getOwnPropertyNames` всех прототипов `Navigator`, `Window`, `Screen`,
  `Document`, `HTMLCanvasElement`, …; полный дамп WebGL1/2 и WebGPU; кодеки
  (матрица `isTypeSupported`/`decodingInfo`); голоса; устройства (счётчики);
  шрифты (измерение списка из ~500 имён + `queryLocalFonts` при согласии);
  метрики текста; скроллбар/хром окна при нескольких DPR; `Intl`; эталонные
  хэши canvas/WebGL/audio (отдельно, помечены как «шумовой набор»); CSS
  media‑features. Один прогон = одна запись базы устройств.
- `diff`: сравнивает две записи. Поля разделены на «детерминированные» (должны
  совпасть побайтно) и «шумовые» (должны отличаться между профилями и
  совпадать между сессиями). Выход — список расхождений = бэклог патчей.
- Хранение: `fingerprints/<os>/<gpu>/<chrome-major>.json` в репозитории;
  персональные данные (имена устройств, серийники) не собираются.

### 6.2. Уровни проверки

| Уровень | Что | Когда |
|---|---|---|
| T1 Офлайн‑фикстуры патчей | по одной на патч (раздел 5), через существующий Page API; детерминизм, покрытие путей, границы шума | каждый коммит |
| T2 Дифф с референсом | `capture` на форке с конфигом X vs запись базы X: детерминированный набор — пустой дифф | каждый коммит |
| T3 Матрица уникальности/стабильности | N профилей × M сессий: хэш стабилен внутри профиля, различен между | ночной |
| T4 Публичные детекторы | CreepJS (trust, число «lies» = 0), browserleaks canvas/webgl/fonts, pixelscan «consistent», fingerprint.com demo (visitorId стабилен в профиле, различен между), browserscan, iphey, sannysoft, rebrowser | ночной/вручную |
| T5 Реальные цели | A/B против настоящего Chrome с тем же прокси | перед релизом форка |
| T6 Регрессия на ребейзе | T1–T4 на том теге, на который решили обновиться, до публикации сборки | при обновлении |

Отдельный список «лжи» CreepJS, который валидатор конфигурации обязан
исключать статически: `platform` ↔ `userAgentData.platform` ↔ UA; renderer ↔
лимиты; `hardwareConcurrency` ↔ тайминг воркеров; шрифты ↔ ОС; emoji ↔ ОС;
`Intl` ↔ `TZ` ↔ `Date`; `deviceMemory` ↔ `jsHeapSizeLimit`; canvas/audio
стабильность; `screen` ↔ `matchMedia`; голоса ↔ ОС.

## 7. Сборка и поставка

- **Исходники:** `depot_tools`, чекаут тега Chrome stable (например
  `154.0.8037.97`). Патчи — серия файлов `fork/patches/NNN-<область>.patch` +
  `series`, применяемая поверх тега (модель ungoogled‑chromium), а не
  долгоживущая ветка: ребейз = «применить серию на новый тег, починить
  упавшие». Рабочая ветка для разработки есть, но источником истины являются
  экспортированные патчи.
- **GN args (базовые):** `is_official_build=true`, `is_debug=false`,
  `symbol_level=0`, `blink_symbol_level=0`, `is_component_build=false`,
  `proprietary_codecs=true`, `ffmpeg_branding="Chrome"`, `enable_widevine=true`,
  `enable_nacl=false`, `use_thin_lto=true`, `chrome_pgo_phase=2` (с
  `checkout_pgo_profiles=True`), `target_os="linux"`, `target_cpu="x64"`.
  `is_chrome_branded=true` недоступен; бренд даём патчем (5.1).
- **Ресурсы.** Чекаут ~40 ГБ, out‑каталог 30–60 ГБ. Чистая сборка 1–2 ч на 64
  ядрах, 6–10 ч на 16 потоках ноутбука. После первой сборки инкрементальная
  пересборка патча — минуты, если `out/` сохраняется.
- **Сборочная машина — эта (i7‑1260P, 16 потоков, 592 ГБ NVMe, 15 ГБ RAM).**
  Узкое место — память: ThinLTO‑линковка `chrome` в официальной конфигурации
  требует 20–30 ГБ и упадёт по OOM. Поэтому для F0–F2 собираем **без**
  `is_official_build`/ThinLTO/PGO (`is_official_build=false`, `use_thin_lto=false`,
  `symbol_level=0`, `blink_symbol_level=0`, `v8_symbol_level=0`,
  `is_component_build=false`, `enable_nacl=false`), ограничиваем линковку
  (`concurrent_links=1`) и число компиляций (`autoninja -j 12`, чтобы не
  вытеснять всё из памяти). Нужен swap‑файл 32 ГБ на NVMe вместо нынешнего
  раздела 7,5 ГБ (`fallocate -l 32G /swapfile && mkswap && swapon` — требует
  root) и закрытые тяжёлые приложения на время линковки. Отличие
  неофициальной сборки от Chrome по производительности JS измеримо только
  микробенчмарками; для F3/релиза вернуться к официальной конфигурации на
  машине с 32+ ГБ RAM или добавить ещё swap и принять медленную линковку.
- **Референс Google Chrome на Linux** для гейта G1 нужен на той же машине:
  поставить `google-chrome-stable` из `.deb` (snap‑Chromium — не Chrome: бренд
  `Chromium`, токен `Ubuntu` в UA, своя песочница snap).
- **Артефакт:** tar.xz ~150–200 МБ, имя включает версию Chromium и версию
  серии патчей; SHA256; публикация в releases. Python‑сторона:
  `disguise fetch-browser` (кэш в `~/.cache/disguise`, проверка хэша),
  `find_chrome()` предпочитает форк, если он скачан, иначе системный Chrome
  (режим «честный Linux» продолжает работать без форка). Docker‑образ
  использует тот же артефакт.
- **Каденс:** по необходимости, не по календарю. Ориентир — не отставать больше
  чем на 2–3 мажора (около квартала): такая доля реального трафика ещё обычна,
  а дальше популяция старой версии становится маленькой и бот‑насыщенной, плюс
  ребейз через полгода больнее, чем через квартал. Повод обновиться раньше —
  поломка на цели или новая API, которую цель начала проверять. Автоматической
  пересборки на каждый stable‑тег нет.
- **Лицензии:** Chromium — BSD; портированные файлы Brave — MPL‑2.0 с
  сохранением заголовков; шрифты — только оригиналы пользователя или
  перечисленные свободные клоны; Widevine CDM не распространяем сами.

## 8. Фазы

**F0. Измерение (до патчей).** Статус 2026‑10‑11: `capture`/`diff` реализованы
(`src/disguisebrowser/fingerprint/`, CLI `disguise capture` / `disguise diff`,
юнит‑ и интеграционные тесты). Первая запись базы — `fingerprints/linux/
intel-mesa-iris-xe-graphics/154-chromium.json` (snap‑Chromium 154 на этой машине;
полезна как образец небрендированной сборки для G1: бренд `Chromium`, токен
`Ubuntu` в UA). Уже измерено: `navigator.gpu` на Linux существует, но
`requestAdapter()` возвращает `null` — для Windows‑персоны нужен рабочий
адаптер (5.3). Остальные пункты F0 — ниже.
- ~~`capture`/`diff` (6.1); echo‑эндпоинт для заголовков~~ — сделано.
- Референсы: системный Chrome на Linux (этой машины и Xvfb‑llvmpipe), Windows 11
  Chrome stable минимум на трёх GPU (Intel Iris Xe/UHD 770, AMD RX 6600/7600,
  NVIDIA RTX 3060/4060), с DPR 1 и 1.25.
- Сборочный конвейер на этой машине (раздел 7): `fork/bootstrap.sh [TAG]` —
  `depot_tools`, `fetch --nohooks chromium`, чекаут тега (по умолчанию — версия
  установленного `google-chrome`), `fork/args.gn`, сборка `chrome`;
  перезапускаемый, логи в `~/chromium/logs/`. Параллельно —
  `google-chrome-stable` из `.deb` как референс. Затем `capture` на чистой сборке и дифф с Google Chrome →
  калибровка инструмента и точный бэклог G1.
- Выход: записи базы устройств, список расхождений, подтверждённые пути в
  исходниках для разделов 5.1–5.7 (все «проверить» закрыты).

**F1. Паритет с Chrome (G1).** Бренды, кодеки, решение по Widevine, голоса,
`--disguise-config` как пустой канал. Гейт: T2 дифф с Linux Chrome пуст; T4 на
уровне системного Chrome (текущая базовая линия 5/5).

**F2. Энтропия (G2), персона остаётся Linux.** Шум canvas/WebGL‑чтения/аудио;
`hardwareConcurrency`/`deviceMemory`; таблицы WebGL для 2–3 Linux‑GPU из
референсов (замена driconf). Гейт: T3 (N профилей → N отпечатков, стабильных
между сессиями); CreepJS без «lies»; fingerprint.com — разные visitorId.

**F3. Windows‑персона (G3).** 5.1 целиком; пакет шрифтов + fontconfig +
параметры рендера текста; скроллбары/хром окна; голоса; HEVC/кодеки; WebGPU
адаптер; таблицы WebGL/расширений D3D11 для выбранных GPU. Гейт: T2 дифф с
Windows‑референсом пуст вне шумового набора; T4; T5 на целях пользователя.

**F4. Опционально.** 5.8 (pipe без `webdriver`, CDP‑ввод, стелс‑headless);
macOS‑персона; Widevine, если потребуется.

## 9. Риски

- **Плохой патч хуже отсутствующего.** Единственная защита — F0 и гейты T2/T3
  перед каждой публикацией; патч без фикстуры не принимается.
- **Побочные каналы реального железа:** тайминг ядер, скорость WebGL/шейдеров,
  давление памяти. Митигация: персоны с интегрированной графикой и ядрами ≤
  квоты контейнера; не заявлять топовые дискретные GPU.
- **Остаточные признаки ОС в Windows‑персоне:** emoji, субпиксельный рендер
  текста, Widevine, сетевые голоса, WebGPU‑бэкенд. Каждый измеряется в F0 и
  либо закрывается, либо фиксируется в `SIGNALS.md` как ЧАСТЬ/НЕТ с оценкой
  веса у конкретных детекторов.
- **Стоимость ребейза:** Blink и `content/` меняются; хуки чтения canvas могут
  «молча» перестать применяться при появлении нового пути. Митигация —
  список путей как тест (5.4), небольшие патчи, CI на каждый тег.
- **Версия.** Свежесть сама по себе слабый сигнал: реальные пользователи сидят
  на 1–3 мажорах позади. Сильный сигнал — соврать о версии (заявить текущий
  stable, а поведение и набор API — старые) или заморозить одну сборку навсегда,
  так что все профили кластеризуются на версии, которой в живом трафике уже нет.
  Митигация — честный UA своей сборки (F6) и обновление примерно раз в квартал.
- **Юридическое:** шрифты и Widevine — только лицензионно чистые варианты;
  выдача себя за другой браузер/ОС — зона ответственности пользователя, как и
  раньше.

## 10. Решения, которые нужно принять до F0

| Вопрос | Предлагаемое по умолчанию |
|---|---|
| Персоны v1 | Linux (честная, с энтропией) + Windows 11; macOS — F4 |
| GPU‑профили v1 | интегрированные: Intel Iris Xe / UHD 770, AMD Radeon Graphics (Renoir/Rembrandt); дискретные — после замеров побочных каналов |
| Хранение патчей | серия `.patch` поверх тега (ungoogled‑стиль), не overlay `chromium_src` |
| Сборочная инфраструктура | **эта машина** (решение владельца 2026‑10‑11): неофициальная конфигурация без ThinLTO/PGO, swap 32 ГБ; официальная сборка — позже на 32+ ГБ RAM или с большим swap |
| Пакет шрифтов Windows | пользователь приносит оригиналы; свободные метрические клоны — fallback, оба варианта через один fontconfig |
| Политика шума | константный по профилю (как у реального устройства), не по origin |
| Widevine | вне v1, если среди целей нет стриминга |
| Канал конфигурации | `--disguise-config=<файл>`; CDP‑расширение не делаем |
