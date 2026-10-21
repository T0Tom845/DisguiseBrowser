# DisguiseBrowser

[![Русский](https://img.shields.io/badge/README-%D1%80%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-1f6feb?style=for-the-badge)](README.ru.md)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Linux](https://img.shields.io/badge/platform-Linux-111111?logo=linux&logoColor=white)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A live browser for scraping and automation. The page sees a normal desktop session: the Chrome already installed on the machine, or [Camoufox](https://github.com/daijro/camoufox), a Firefox build whose fingerprint is applied in C++ rather than by a script on the page.

The library, the CLI, and the MCP server share one API. Python 3.12+, Linux. Current release: **v0.3**.

| | Chrome | Camoufox |
|---|---|---|
| Browser | system Google Chrome | Camoufox's Firefox |
| When it fits | the target expects Chrome and its TLS | many long-lived identities |
| Identity | a persona taken from this machine; on a virtual display, the GPU name comes from Mesa | a named profile: device, screen, cookies, cache |
| Input | XTest, events from the X server itself | Playwright/Juggler and Camoufox's own humanization |
| Network | a local SOCKS5 gateway; the proxy password never lands on the command line | the same gateway |

What the checks look for, what is covered, and how it was measured is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/SIGNALS.md`](docs/SIGNALS.md). Those notes are in Russian.

## Install

Chrome is the one already on the system. Camoufox is fetched once, separately.

```bash
# a server with no monitor
sudo apt install xvfb libgl1-mesa-dri mesa-utils openbox fonts-noto-color-emoji

uv sync --extra camoufox --extra mcp
uv run python -m camoufox fetch
uv run disguise doctor
```

`pip install -e '.[camoufox,mcp]'` does the same. The `camoufox-dist/` directory is not in git: a built Firefox does not fit on GitHub. The official binary from `camoufox fetch` runs immediately. The canvas-noise patch is separate, [below](#a-camoufox-build-of-your-own).

## Quick start

A Camoufox profile. The device is pinned when the profile is created and is not redrawn on the next launch.

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

The same flow from the shell:

```bash
disguise profile create shop-1 --os windows --screen 1920x1080 \
    --proxy socks5://user:pass@host:1080 --geoip
disguise open --engine camoufox --profile shop-1 https://example.com
disguise test --engine camoufox --profile shop-1 --display virtual
```

System Chrome, without a Camoufox profile:

```python
from disguisebrowser import Persona, launch

persona = Persona.from_host(
    proxy="http://user:pass@residential-proxy:8080",
    seed=42,
)
browser = await launch(persona=persona, geoip=True, user_data_dir="./profiles/berlin")
```

`geoip=True` takes the timezone and languages from the proxy's exit IP. Chrome only sees a local passwordless SOCKS5. Several browsers in one process are several `launch()` calls: each gets its own display, profile, and proxy.

## Profiles

A profile is `profile.json` (device, OS, proxy, languages, timezone, seed; mode `0600`) plus a Firefox directory with cookies, localStorage, and cache. Storage lives in `$DISGUISE_HOME/profiles` or `~/.local/share/disguisebrowser/profiles`.

```bash
disguise profile list
disguise profile create work --os macos --kind preset --seed 7
disguise profile set shop-1 --proxy ''          # network changes, the device does not
disguise profile set shop-1 --screen 1920x1080  # fonts and canvas noise recompute on the next launch
disguise profile delete work --yes
```

One profile, one browser at a time. `delete` also refuses while the profile is in use.

`disguise manager` serves one page at `127.0.0.1:8787` with every profile. Each row has a switch, an address, and an "In the browser" button: clicks, scrolling, and the keyboard go to that profile, so this view does not need VNC. Screen size, proxy, and a note are edited there too. `Ctrl+C` in the terminal closes the manager and the windows it opened.

## MCP

```bash
disguise mcp                      # stdio, for Cursor / Claude
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

Sessions are independent: `browser_launch` returns a `session_id`. Then `navigate`, `snapshot` (an accessibility tree with `[ref=eN]`), `click` / `type_text` / `scroll` by ref or CSS, `screenshot`, `evaluate`, tabs, `browser_list`, `browser_close`. For Camoufox: `browser_launch(engine="camoufox", profile="shop-1", create=true, os="windows")`, plus `profile_list` and `profile_create`. Deleting profiles stays in the CLI.

## Docker

The Chrome image: system Chrome, Xvfb, Mesa, openbox, tint2.

```bash
docker build -t disguisebrowser .
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser doctor
docker run --rm --shm-size=1g --security-opt seccomp=unconfined disguisebrowser test --display virtual
```

`seccomp=unconfined` is what Chrome's sandbox needs. The library does not pass `--no-sandbox`.

The Camoufox image downloads the official build unless you hand it another one:

```bash
docker build -f Dockerfile.camoufox -t disguisebrowser-fox .
docker run --rm disguisebrowser-fox doctor --engine camoufox
docker run --rm -v fox-data:/data disguisebrowser-fox profile create shop-1 --os windows
docker run --rm --shm-size=1g -v fox-data:/data disguisebrowser-fox \
    test --engine camoufox --profile shop-1 --display virtual
```

Profiles, cookies, and cache live in `/data`. Keep that volume.

A build that includes the canvas patch is passed in as an archive. Firefox sources are not part of the image build:

```bash
docker build -f Dockerfile.camoufox \
    --build-arg CAMOUFOX_DIST_URL=https://example.com/camoufox-dist.tar.zst \
    --build-arg CAMOUFOX_DIST_SHA256=<sha256 of the archive> \
    -t disguisebrowser-fox .
```

The archive is the contents of the build directory, with `camoufox-bin` at the root:

```bash
tar -C camoufox-dist -I zstd -cf camoufox-dist.tar.zst .
sha256sum camoufox-dist.tar.zst
```

That archive belongs in a [GitHub Release](https://github.com/T0Tom845/DisguiseBrowser/releases). It is about 300 MB, past GitHub's limit for a file in git.

## A Camoufox build of your own

Official Camoufox 156 does not change canvas pixels. Profiles on one machine share the same picture. The patch [`fork/camoufox/zz-canvas-readback.patch`](fork/camoufox/zz-canvas-readback.patch) shifts a few rare pixels on read. Where it shifts them depends on the profile seed (`canvas:seed`), which launch already sends. The official binary ignores that key.

Build once, in a separate clone of [camoufox](https://github.com/daijro/camoufox) at tag `v156.0.1-beta.36`:

```bash
cp fork/camoufox/zz-canvas-readback.patch patches/
make dir
make build
```

The finished browser directory is then packed for Docker, or selected with `CAMOUFOX_EXECUTABLE_PATH`. When to rebuild is in [`fork/camoufox/README.md`](fork/camoufox/README.md). Firefox sources are not in this repository. The patch and the files it is built from live in `fork/camoufox/` and stay under MPL-2.0.

Measured on build 156.0.1-beta.36 in an image with no GPU: sannysoft 0 of 56 failed (1 check does not apply to Firefox), rebrowser 5 green, TLS is a Firefox JA4. A "many profiles, many distinct canvas hashes" run is not wired up as its own command yet.

Camoufox's TLS and HTTP/2 belong to Firefox and match its User-Agent. Targets that need Chrome specifically go through the system engine. The sannysoft "Chrome (New)" check is reported as `n/a for this engine` on Camoufox. A proxy without `--geoip` leaves the host timezone and language in place, and Camoufox prints a `LeakWarning`.

## What was measured on Chrome

`disguise test --display virtual` in a container with no GPU, Chrome 155:

```
PASS  sannysoft             0 of 57 checks failed
PASS  rebrowser             6 green, 4 not triggered
PASS  deviceandbrowserinfo  You are human!
PASS  browserscan           verdict: Normal
PASS  tls                   JA4 t13d1517h2_… · HTTP/2 52d84b11…
```

Chrome input goes through XTest. CDP pointer events keep `pressure` at 0 while the button is down; a physical mouse and XTest report 0.5. On a virtual display, a Russian persona gets a real ЙЦУКЕН layout, not a chord the page could observe.

Capture a fingerprint and compare it with another machine:

```bash
disguise capture
disguise capture --serve
disguise diff fingerprints/a.json fingerprints/b.json
```

## Limits

- Headless (`headless=True`) is an explicit mode with weak stealth.
- On a virtual display with no GPU, canvas, audio, and WebGL limits stay on llvmpipe. Renaming the GPU does not change that.
- ЙЦУКЕН is switched on DisguiseBrowser's own virtual display. On someone else's X server, Cyrillic goes through `Input.insertText`.
- Scripted Camoufox input (MCP, `click`, `type`) stays on Playwright/Juggler. The live view in `disguise manager` turns humanization off. That view's clipboard is not yet tied to the clipboard of the machine where the manager is open.

Use it within the rules of the sites you open. What you do with it is on you.

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | why this is a library, not a fork you rebuild for every change (Russian) |
| [`docs/SIGNALS.md`](docs/SIGNALS.md) | signal table: covered, open, and how each one was checked (Russian) |
| [`docs/FORK_PLAN.md`](docs/FORK_PLAN.md) | if a Chromium patch is ever actually needed (Russian) |
| [`fork/camoufox/README.md`](fork/camoufox/README.md) | the canvas patch: what it changes and how to build the browser |

The library lives in `src/disguisebrowser/`. Chrome is `launcher.py`, `cdp/`, `persona.py`, `input/`. Camoufox is the `camoufox/` package. The shared page contract is `protocols.py`, so MCP, the scorecard, and warmup do not branch on the engine.

## License

DisguiseBrowser's own code is [MIT](LICENSE).

Camoufox, Firefox, the patch in `fork/camoufox/`, and any program built from them are Mozilla Public License 2.0. The MIT license on the rest of the repository does not take those rights away. The list and the attributions are in [NOTICE](NOTICE). The Firefox and Camoufox names belong to their projects. This build is not official Camoufox and it is not a Mozilla product.
