#!/usr/bin/env bash
# Fetch Chromium at a Chrome stable tag and build it with fork/args.gn.
#
# Usage:
#   fork/bootstrap.sh [TAG] [CHROMIUM_DIR]
#
#   TAG           Chromium tag to build, e.g. 154.0.8037.97. Default: the version
#                 of the installed google-chrome, so the reference and our build
#                 are the same code.
#   CHROMIUM_DIR  Where depot_tools and src/ live. Default: ~/chromium
#
# Re-runnable: every step is skipped when its result already exists. The first
# run downloads ~40 GB and builds for 6-10 hours on this host; keep the laptop
# on mains power and do not suspend. Logs go to CHROMIUM_DIR/logs/.
#
# Before the first run (root needed, once):
#   sudo fallocate -l 32G /swapfile && sudo chmod 600 /swapfile \
#     && sudo mkswap /swapfile && sudo swapon /swapfile
#   src/build/install-build-deps.sh is run by this script and asks for sudo.
#
# Network: the checkout needs chromium.googlesource.com (git), chrome-infra-
# packages.appspot.com (CIPD) and storage.googleapis.com (GCS). Where these are
# blocked, run under a VPN or pass a proxy — git, curl, cipd and gclient all
# honour it:
#   HTTPS_PROXY=http://127.0.0.1:PORT HTTP_PROXY=http://127.0.0.1:PORT fork/bootstrap.sh
# (socks5://127.0.0.1:PORT works too). The preflight below checks all three.
set -euo pipefail

retry() {
  local attempts=$1
  shift
  local n=1
  until "$@"; do
    if ((n >= attempts)); then
      echo "!! giving up after $attempts attempts: $*" >&2
      return 1
    fi
    echo "== attempt $n failed, retrying in $((n * 15))s: $*"
    sleep $((n * 15))
    n=$((n + 1))
  done
}

preflight() {
  local failed=0 host
  for host in chromium.googlesource.com chrome-infra-packages.appspot.com storage.googleapis.com; do
    if ! curl -sS -o /dev/null -m 20 "https://$host/" 2>/dev/null; then
      echo "!! cannot reach https://$host/ (TLS reset or timeout)"
      failed=1
    fi
  done
  if [[ $failed -ne 0 ]]; then
    cat >&2 <<'EOF'
The Chromium checkout cannot proceed without these hosts. Either enable a VPN
for this shell or rerun with a proxy, e.g.
  HTTPS_PROXY=http://127.0.0.1:PORT HTTP_PROXY=http://127.0.0.1:PORT fork/bootstrap.sh
Set NO_PREFLIGHT=1 to skip this check.
EOF
    exit 3
  fi
}

TAG="${1:-}"
CHROMIUM_DIR="${2:-$HOME/chromium}"
JOBS="${JOBS:-12}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGS_GN="$HERE/args.gn"

mkdir -p "$CHROMIUM_DIR/logs"
LOG="$CHROMIUM_DIR/logs/bootstrap-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) bootstrap start (log: $LOG)"
PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"
if [[ -n "$PROXY" ]]; then
  echo "== using proxy $PROXY"
  # git does not always honour the env vars through this proxy, and HTTP/2
  # tunnels get reset. GIT_CONFIG_* applies to every git gclient spawns
  # without writing ~/.gitconfig.
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0=http.proxy
  export GIT_CONFIG_VALUE_0="$PROXY"
  export GIT_CONFIG_KEY_1=http.version
  export GIT_CONFIG_VALUE_1=HTTP/1.1
fi
[[ -n "${NO_PREFLIGHT:-}" ]] || preflight

if [[ -z "$TAG" ]]; then
  if command -v google-chrome >/dev/null 2>&1; then
    TAG="$(google-chrome --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+')"
    echo "== tag from installed google-chrome: $TAG"
  else
    echo "no TAG given and google-chrome is not installed; pass the tag explicitly" >&2
    exit 2
  fi
fi

# 1. depot_tools
if [[ ! -d "$CHROMIUM_DIR/depot_tools" ]]; then
  echo "== cloning depot_tools"
  git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git "$CHROMIUM_DIR/depot_tools"
fi
export PATH="$CHROMIUM_DIR/depot_tools:$PATH"
export DEPOT_TOOLS_UPDATE=1

# 2. source checkout (full history: rebases between tags need it)
cd "$CHROMIUM_DIR"
if [[ ! -d src ]]; then
  echo "== fetch --nohooks chromium (~40 GB)"
  fetch --nohooks chromium
fi
cd src

# 3. check out the tag and sync dependencies to it
CURRENT="$(git describe --tags --exact-match 2>/dev/null || true)"
if [[ "$CURRENT" != "$TAG" || ! -f "$CHROMIUM_DIR/.synced-$TAG" ]]; then
  echo "== checking out $TAG"
  retry 5 git fetch origin "refs/tags/$TAG:refs/tags/$TAG" --no-tags
  git checkout -q "tags/$TAG"
  echo "== gclient sync"
  # -D drops the half-written dependency checkouts left by a 429.
  retry 5 gclient sync -D --nohooks --with_branch_heads --with_tags
  touch "$CHROMIUM_DIR/.synced-$TAG"
fi

# 4. build dependencies (sudo) and hooks.
# A background run has no terminal, so sudo would wait for a password that
# cannot be typed. Ask for it up front, in a normal terminal.
if [[ ! -f "$CHROMIUM_DIR/.deps-installed" ]]; then
  if [[ ! -t 0 ]] || ! sudo -n true 2>/dev/null; then
    if [[ ! -t 0 ]]; then
      cat <<EOF
== build packages need a sudo password, and this run has nowhere to type it.
   In a normal terminal (not nohup), run:

     cd $CHROMIUM_DIR/src && ./build/install-build-deps.sh --no-prompt && touch $CHROMIUM_DIR/.deps-installed

   Then start this script again.
EOF
      exit 4
    fi
  fi
  echo "== install-build-deps.sh (sudo will ask for your password below)"
  ./build/install-build-deps.sh --no-prompt
  touch "$CHROMIUM_DIR/.deps-installed"
fi
if [[ ! -f "$CHROMIUM_DIR/.hooks-$TAG" ]]; then
  echo "== gclient runhooks"
  gclient runhooks
  touch "$CHROMIUM_DIR/.hooks-$TAG"
fi

# 5. configure and build
OUT=out/Release
mkdir -p "$OUT"
if ! cmp -s "$ARGS_GN" "$OUT/args.gn"; then
  cp "$ARGS_GN" "$OUT/args.gn"
fi
echo "== gn gen $OUT"
gn gen "$OUT"
echo "== autoninja -C $OUT chrome -j $JOBS (this is the long part)"
autoninja -C "$OUT" chrome -j "$JOBS"

echo "== $(date) done: $PWD/$OUT/chrome"
"$OUT/chrome" --version
echo
echo "capture it with:"
echo "  DISGUISE_CHROME=$PWD/$OUT/chrome disguise capture"
