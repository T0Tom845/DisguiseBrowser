#!/bin/sh
# Official Camoufox lives in the image user's cache. A patched archive
# extracted to /opt/camoufox takes precedence.
if [ -x /opt/camoufox/camoufox-bin ]; then
    export CAMOUFOX_EXECUTABLE_PATH=/opt/camoufox/camoufox-bin
fi
exec disguise "$@"
