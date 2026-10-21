# Шум пикселей canvas в Camoufox

Официальный Camoufox 156 пиксели canvas не меняет: все профили на одной машине
получают один и тот же хэш картинки и склеиваются в один компьютер. Старый шум
Camoufox (его убрали в [#528](https://github.com/daijro/camoufox/issues/312))
сдвигал примерно каждый второй пиксель на ±1, и CreepJS помечал это как
подделку. Этот патч другой.

На чтении копия пикселей получает редкий сдвиг на один уровень яркости, около
0,4 % тех пикселей, которые и так не являются ровной заливкой. Где именно —
функция сида профиля и самой картинки. Одни и те же правила для обычного canvas
(`getImageData`, `toDataURL` / `toBlob`) и для WebGL (`readPixels` и снимок,
из которого собирается `toDataURL` WebGL-холста). У WebGL шум ставится только
на плотно уложенные RGBA/BGRA-байты: редкая упаковка строк с дырками не
трогается.

- тот же профиль и та же картинка дают те же байты, в том числе после перезапуска;
- другой профиль даёт другой хэш;
- белый, чёрный и любая ровная заливка не меняются;
- сам холст не портится, поэтому `drawImage` не накапливает шум.

Сид читается из ключа `canvas:seed` (его уже кладёт запуск DisguiseBrowser).
Странице этот ключ не виден: отдельной функции `setCanvasSeed` нет.

`createImageBitmap`, потом холст `bitmaprenderer` и его `toDataURL` / `toBlob`,
получает тот же шум на копии. Сам холст остаётся чистым. Снимок WebGL уже
шумный, второй раз патч его не трогает. `toDataURL` и `toBlob` холста WebGPU
шумят тем же кодом.

Хэш PNG у `toDataURL` в Camoufox прыгает от запуска к запуску. Связка профилей
идёт по пикселям `getImageData`. `canvas.captureStream` патч не шумит.

## Когда пересобирать

Сборка Firefox долгая. Её не надо повторять на каждый beta Camoufox.

Патч рассчитан на **156.0.1-beta.36** (Firefox 156, `FIREFOX_156_0_1_RELEASE`).
Пересобрать браузер нужно только если:

- `patch` перестал накладываться;
- в новом Firefox сдвинулись `GetImageBuffer` или `GetImageDataArray` в
  `dom/canvas/CanvasRenderingContext2D.cpp`, `GetImageBuffer` в
  `ImageBitmapRenderingContext.cpp` или `dom/webgpu/CanvasContext.cpp`;
- меняется сам шум в `CanvasFingerprintManager.cpp`.

Пока стоит скачанный официальный бинарник, `canvas:seed` уходит в конфиг, но на
пиксели не влияет.

## Как собрать один раз

В клоне [camoufox](https://github.com/daijro/camoufox) на теге `v156.0.1-beta.36`:

```bash
cp /path/to/DisguiseBrowser/fork/camoufox/zz-canvas-readback.patch patches/
make dir
make build
```

`make dir` сбрасывает дерево Firefox и заново накладывает всё из `patches/`.
Файл назван `zz-`, чтобы идти после патча WebGL, который добавляет каталог
`camoucfg` в сборку canvas. Если дерево уже собрано, снова скопируйте патч в
`patches/` и повторите `make dir`, иначе следующий сброс сотрёт правку.

Запуск против своего бинарника: переменная `CAMOUFOX_EXECUTABLE_PATH`.

`fork/bootstrap.sh` в корне `fork/` собирает Chromium, не этот браузер.

Исходники шума в этом каталоге — то, из чего собран патч. После правки
`CanvasFingerprintManager.cpp` или `.h` патч нужно пересобрать: unit-тест
сравнивает их побайтно и гоняет C++ против `canvas_noise.py`.
