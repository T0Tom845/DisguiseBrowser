# Реестр сигналов детекта

Что может выдать профиль, что с этим сделано и чем это доказано. Файл
обновляется вместе с любым изменением стелс‑слоя: новая мера без строки
здесь или новый сигнал без статуса — это неполное изменение.

Слои — из `ARCHITECTURE.md`, раздел 3. Статус:

| Код | Значение |
|---|---|
| **OK** | закрыто, есть автоматическая проверка (юнит, интеграция или scorecard) |
| **OK*** | закрыто по коду и ручной проверке, автоматического теста нет |
| **ЧАСТЬ** | закрыто при условии (флаг, пакет в системе, явный opt‑in) или не для всех случаев |
| **НЕТ** | не закрыто; известно, что делать |
| **ВНЕ** | не в контуре библиотеки: ответственность прокси, скрипта или сознательное решение |

Колонка «Проверка»: `unit` — `tests/unit`, `integ` — `tests/integration` на реальном
Chrome, `live` — `disguise test` по публичным детекторам, `нет` — не проверяется.

## L0. Сеть

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| Репутация exit‑IP, datacenter ASN | базы IP, ASN | ВНЕ | прокси приносит пользователь | нет | предупреждать в `geoip`, если провайдер отдаёт `connection.type = hosting` |
| Утечка реального IP через WebRTC | ICE‑кандидаты | OK* | `webrtc.ip_handling_policy = disable_non_proxied_udp` в Preferences при прокси (`launcher.default_preferences`) | unit (prefs) | интеграционный тест с `RTCPeerConnection` на фикстуре |
| Утечка DNS (резолв на сервере, а не на прокси) | сравнение резолвера и exit‑IP | OK* | Chrome получает SOCKS5‑шлюз и отдаёт ему имя хоста; шлюз передаёт имя upstream (`proxy.py`) | unit (шлюз) | — |
| `Proxy-Authorization` и прочие `Proxy-*` в запросах | заголовки на сервере | OK | Chrome не знает о креды: их держит шлюз | integ (`test_chrome_traffic_goes_through_authenticated_upstream`) | — |
| Креды прокси в командной строке процесса | `ps`, логи | OK | только `socks5://127.0.0.1:порт` в `--proxy-server` | unit | — |
| IPv6 в обход прокси | двойной стек | ЧАСТЬ | через SOCKS всё идёт в шлюз; прямого запрета IPv6 на хосте нет | нет | документировать `sysctl` / отключение IPv6 в контейнере |
| TTL/окно TCP не соответствует заявленной ОС | p0f на сервере | ВНЕ | сайт видит стек exit‑узла прокси, а не наш; персона и так Linux | нет | — |

## L1. Транспорт

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| JA3/JA4, порядок HTTP/2 SETTINGS | TLS‑fingerprint | OK | системный Chrome, ничего не патчится | live (`tls`) | — |
| `sec-ch-ua*` стёрты или не совпадают с UA | серверная проверка без JS | OK | политика запрещает UA‑override без `userAgentMetadata` (`cdp/policy.py`); в headed UA не трогаем | unit (policy), integ (`brands >= 2`) | — |
| `Accept-Language` ≠ `navigator.languages` ≠ страна IP | сравнение заголовка, JS и гео | OK | заголовок и JS совпадают всегда (`--lang` + prefs). Прокси без явного `geoip=False` выравнивает таймзону и языки по exit‑IP (`wants_geoip`); неудачный lookup по‑прежнему роняет запуск. Профиль Camoufox хранит свой флаг и явные язык/таймзону | unit (`wants_geoip`), integ (`test_geoip_aligns_timezone_and_languages`) | привязка профиль ↔ прокси ↔ `seed`, чтобы один профиль не ходил с разных стран |
| `HeadlessChrome` в UA, в т.ч. из воркеров | UA | ЧАСТЬ | headed — нет проблемы; в `headless=True` UA подменяется флагом | live (`tls`: UA), unit | в headless воркер‑UA не проверен |
| Устаревшая версия Chrome | мажор UA | OK | всегда установленный системный Chrome; в Docker — stable на момент сборки | нет | пересобирать образ по расписанию |

## L2. Протокол управления

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| `navigator.webdriver = true` | JS | OK | явный `--remote-debugging-port`, без `--enable-automation` и pipe (ADR‑1) | integ, live (sannysoft, dabi) | — |
| Инфобар «управляется ПО», `--test-type`, `--disable-blink-features` | `innerHeight`, флаги | OK | флаги не используются | unit (`FORBIDDEN_FLAGS`) | — |
| `Runtime.enable` (сериализация аргументов консоли) | getter на `Error.stack` в `console.log` | OK | запрещён политикой; клиент работает без него | unit, integ (`runtime_enable_leak.html`), live (rebrowser) | — |
| `//# sourceURL` в стеках | `Error().stack` | OK | не добавляется | integ | — |
| Выполнение из main world (стек вызовов, подменённые DOM‑методы) | хуки на `getElementsByClassName` и т.п. | OK | JS только в изолированном мире со случайным именем (ADR‑3) | integ (`main_world_hook.html`), live (rebrowser) | — |
| `Page.setBypassCSP` | CSP‑проба | OK | запрещён | unit | — |
| Эмуляция viewport (`setDeviceMetricsOverride`) | рассинхрон screen/window | OK | запрещена; геометрия задаётся размером реального окна | unit | — |
| Init‑скрипты в main world (`__pwInitScripts`, изменённые прототипы) | проверка прототипов, `toString` | OK | запрещены; изолированный мир разрешён | unit | — |
| `cdc_*`, `__puppeteer_*`, `__playwright*` глобалы | JS | OK | нет ни одного драйвера в контуре | integ (`isolated world cannot see page globals` косвенно) | — |
| Открытый DevTools‑порт на loopback | локальные процессы | ВНЕ | только `127.0.0.1`; страницы не достают до loopback из‑за LNA | нет | изолированный пользователь/контейнер на многопользовательских хостах |
| Воркеры и OOPIF с `Runtime.enable` | те же утечки в воркере | OK | дочерние цели резюмируются без `Runtime.enable`; iframe получает изолированный мир | integ (`test_targets.py`) | — |
| `--headless=new` включает `AutomationControlled` | `navigator.webdriver` в headless | НЕТ | headless — явный opt‑in; webdriver там `true` | нет | либо не поддерживать headless, либо документировать как «не стелс» громче |

## L3. JS‑окружение и железо

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| Нет WebGL (сервер без GPU) | `getContext('webgl') === null` | OK | Xvfb + GLX, `LIBGL_ALWAYS_SOFTWARE=1`, `--ignore-gpu-blocklist` | integ, live | — |
| Renderer `SwiftShader` / `llvmpipe` | `UNMASKED_RENDERER_WEBGL` | OK | имя GPU через Mesa driconf (ADR‑5) | integ | см. две строки ниже |
| **Имя GPU противоречит параметрам WebGL** (лимиты, расширения llvmpipe) | CreepJS, fingerprint.com сверяют renderer с `MAX_*`, extensions | НЕТ | подменяется только строка | нет | на сервере без GPU честный llvmpipe (слабее, но не противоречив), либо virgl/патч (фаза 4) |
| **Одинаковые canvas/audio/WebGL‑хэши у всех сессий сервера** | кластеризация | НЕТ | хэши от llvmpipe/софт‑аудио, у всех одинаковые; имя GPU на них не влияет. У Camoufox сид `canvas:seed` уже уходит в запуск, но официальный бинарник его не читает | нет | патч `fork/camoufox/zz-canvas-readback.patch` разводит пиксели canvas и `readPixels` WebGL после одной пересборки; замер «N сессий → k хэшей» всё ещё не сделан |
| `hardwareConcurrency`, `deviceMemory` одинаковы у всех сессий | JS | НЕТ | берутся из ОС хоста | нет | отдельные контейнеры с разной CPU‑квотой; проверить, что Chrome видит квоту, а не хост |
| Экран всегда 1920×1080, DPR 1 на виртуальном дисплее | `screen.*`, `devicePixelRatio` | ЧАСТЬ | размер — из персоны (по умолчанию 1920×1080, DPR 1) | integ (геометрия) | выбирать из реального распределения разрешений по `seed`; DPR 1.25/1.5 через `-dpi`/scale |
| `availHeight == height` (нет панели) | `screen.avail*` | OK | `_NET_WORKAREA`: от WM+tint2 либо публикуем сами | integ, live (dabi) | высота панели tint2 жёстко 32 px — брать из `Persona.work_area()` |
| `outer*`/`inner*` не согласованы | JS | OK | реальное окно, ничего не эмулируется | integ | — |
| `document.hasFocus() === false`, `visibilityState` | JS | OK | WM или `Page.bringToFront` перед вводом | live (dabi) | — |
| Таймзона ≠ `Date` ≠ `Intl` ≠ воркеры | JS | OK | `TZ` на весь процесс, не `Emulation.setTimezoneOverride`; `Etc/UTC` хоста канонизируется в `UTC`, как у ICU | integ, unit | — |
| Локаль форматов чисел/дат ≠ язык | `Intl` | OK | `--lang` | integ (`languages`) | — |
| Список шрифтов одинаков у всех и не соответствует стране | измерение ширин, `document.fonts` | НЕТ | шрифты — что установлено в системе/образе (Noto, CJK, emoji у всех) | нет | свой `FONTCONFIG_FILE` на процесс: набор по языку персоны |
| `plugins`/`mimeTypes` пусты | JS | OK | настоящий headed Chrome (PDF viewer) | integ | — |
| `Notification.permission`, `permissions.query` | JS | OK | настоящие | integ | — |
| `enumerateDevices()` пуст (нет аудиовыхода на сервере) | mediaDevices | НЕТ | в Xvfb/контейнере нет PulseAudio/PipeWire | нет | null‑sink PipeWire в образе; проверить `audiooutput` ≥ 1 |
| `speechSynthesis.getVoices()` пуст | JS | ВНЕ | у многих Linux‑десктопов тоже пусто | нет | — |
| `maxTouchPoints`, `colorDepth` | JS | OK | 0 и 24 — норма десктопа; Xvfb 24 бит | нет | — |
| `window.chrome`, `chrome.runtime` | JS | OK | настоящий Chrome | integ | — |
| Платформа Linux (редкость ~2–3 %) | статистика | ВНЕ | сознательно: честный Linux вместо противоречивой «Windows» (ADR‑8) | — | Windows‑персона только со шрифтами Windows, opt‑in |
| Батарея, сенсоры | JS | OK | десктоп без батареи — норма | нет | — |

## L4. Когерентность

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| Таймзона/язык ↔ страна IP | гео | ЧАСТЬ | `geoip=True` переписывает персону до старта | integ (`test_geoip_aligns…`) | по умолчанию при прокси |
| Раскладка ↔ язык | `code`/`key` клавиш | OK | `us,ru` при `ru`; `KeyA` → `ф` | unit | другие раскладки (de, fr, ua) |
| UA ↔ Client Hints ↔ platform ↔ TLS | сервер + JS | OK | ничего не подменяется | live | — |
| GPU ↔ лимиты WebGL | JS | НЕТ | см. L3 | нет | см. L3 |
| Один профиль — один прокси — один `seed` | корреляция между визитами | НЕТ | ничего не связывает `user_data_dir`, прокси и `seed`; можно случайно запустить профиль с другим IP | нет | `ProfileStore`: персона + прокси сохраняются в профиле, несовпадение — ошибка |
| Много профилей с одним exit‑IP | корреляция на стороне сайта | ВНЕ | решает пользователь/прокси‑пул | — | предупреждение в `browser_list`/логах при совпадении exit‑IP |

## L5. Поведение

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| `isTrusted === false` | JS | OK | и CDP, и XTest дают trusted | integ | — |
| `pressure == 0` при нажатой кнопке (CDP‑артефакт) | PointerEvent | ЧАСТЬ | XTest по умолчанию на X‑дисплее; на хосте без X и в headless — CDP | integ (`xtest pressure`) | — |
| `screenX == pageX` (crbug 40280325) | MouseEvent | OK | в headed не воспроизводится; XTest даёт реальные координаты | integ | — |
| Клик мимо цели из‑за рамки оконного менеджера | промах по элементу | OK | начало вьюпорта калибруется по реальному `mousemove` (слушатель в изолированном мире), а не по геометрии окна | integ (`move(300,200)` → событие в ±1 px) | — |
| Телепортация курсора, прямые линии, постоянная скорость | траектории | OK* | Безье + minimum‑jerk, тремор, overshoot (`humanizer.py`) | unit (форма пути) | прогон по поведенческим детекторам (например, `bot.incolumitas.com`) |
| Колесо с `deltaY` 53 вместо 120 | WheelEvent | OK | XTest — настоящие щелчки; CDP калиброван на 120 | integ (`wheels`) | — |
| Мгновенный ввод, равные интервалы | тайминг keydown | OK* | лог‑нормальные задержки, биграммы | unit | опечатки с исправлением (есть в дизайне, не реализованы) |
| Кириллица без keydown (`insertText`) | нет `keydown` при `input` | ЧАСТЬ | ЙЦУКЕН на своём виртуальном дисплее — реальные клавиши (`й` → `KeyQ`, `Б` → `Shift+Comma`, всё `isTrusted`); на чужом дисплее — `insertText` | integ (`test_jcuken_…`, `test_without_ru_layout_…`) | `setxkbmap` для других языков (de, fr, ua) |
| Ни одного `mousemove` за сессию | счётчик событий | ЧАСТЬ | движение есть только при клике/скролле | нет | фоновые микродвижения при чтении страницы (opt‑in) |
| Навигация без `Referer`, `history.length == 1` | заголовки | ВНЕ | `goto()` — прямой заход; переходы по ссылкам делать `click()` | нет | параметр `referrer` в `goto()` |
| Мгновенный переход к цели без чтения | тайминг | ВНЕ | задача скрипта; `warmup` добавляет паузы и скролл | — | — |

## L6. История и профиль

| Сигнал | Как проверяют | Статус | Что сделано / где | Проверка | Что дальше |
|---|---|---|---|---|---|
| Пустой профиль: нет истории, кэша, cookies | косвенно (время загрузки ресурсов, consent) | OK* | `Browser.warmup()` / `disguise warmup` | unit (выбор URL) | интеграционный тест: после warmup в профиле есть `History` |
| Профиль не переживает перезапуск | cookies | OK | `user_data_dir` | integ (`cookies`) | — |
| «Неаккуратное завершение» (restore‑bubble) | UI | OK | `exit_type = Normal` в prefs | unit | — |
| Приватные/privacy‑sandbox диалоги | UI | OK | prefs | unit | — |
| Один `seed` → разные окна/GPU между запусками | корреляция | OK | детерминированная геометрия и GPU по `seed` | unit | — |
| Отсутствие расширений, закладок | низкий вес | ВНЕ | — | — | — |

## Измеримость

| Что | Статус | Что дальше |
|---|---|---|
| Scorecard: sannysoft, rebrowser, dabi, browserscan, tls | OK (live) | — |
| CreepJS, fingerprint‑scan, pixelscan, browserleaks, incolumitas (запланированы в разделе 6.7) | НЕТ | добавить в `scorecard.py`; CreepJS ловит противоречие GPU |
| Полный отпечаток браузера и дифф с эталоном реального устройства (`disguise capture` / `disguise diff`, `FORK_PLAN.md` 6.1) | OK (unit + integ) | снять эталоны Windows 11 на реальных машинах (`--serve` + SSH‑туннель); записи — в `fingerprints/` |
| Уникальность между сессиями одного сервера (canvas/audio/WebGL/fonts/cores) | ЧАСТЬ | `capture` снимает хэши и помечает их «шумовым набором»; интеграционный тест проверяет стабильность внутри одной машины. Нет прогона «N сессий → k отпечатков» одной командой |
| Scorecard изнутри Docker‑образа | OK (live, 2026‑10‑11: Chrome 155, 5/5 PASS) | повторять при пересборке образа |

## Порядок закрытия

1. Привязка профиль ↔ прокси ↔ `seed` (L4): `geoip` при прокси уже включён по умолчанию.
   Осталось не давать одному профилю менять страну между запусками.
2. Инструмент уникальности сессий (измеримость). Без него пункты 3–5 нельзя оценить.
3. Распределение разрешений и панель из персоны, fontconfig по языку (L3).
4. Честный llvmpipe на серверах без GPU вместо противоречивого имени; аудиовыход в образе (L3).
5. Расширение scorecard до CreepJS и поведенческих детекторов.
