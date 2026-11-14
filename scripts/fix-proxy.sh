#!/usr/bin/env bash
# Fix rolling-context PROXYERROR on WSL/Linux when the proxy venv has no CA bundle.
#
# Two failure modes this repairs:
#   1. The proxy venv lacks `certifi`, and its Python build does not load the OS
#      CA store -> ssl.create_default_context().cert_store_stats() reports
#      {'x509_ca': 0}. Every outbound TLS handshake to the upstream then fails
#      CERTIFICATE_VERIFY_FAILED, which Claude Code surfaces as PROXYERROR.
#   2. A proxy spawned with `nohup ... &` from an SSH (or hook) session is
#      reaped when that session exits, so the port dies silently. `setsid` with
#      stdin from /dev/null detaches it into its own session.
set -euo pipefail

VENV="${ROLLING_CONTEXT_VENV:-$HOME/.claude/rolling-context-venv}"
PROXY_DIR="${ROLLING_CONTEXT_PROXY_DIR:-$HOME/.claude/plugins/cache/nestor-plugins/rolling-context/1.13.4/proxy}"
PY="$VENV/bin/python"
PORT="${ROLLING_CONTEXT_PORT:-5588}"

echo "[1/3] Ensuring certifi is present in $VENV"
if ! "$PY" -c 'import certifi' >/dev/null 2>&1; then
    "$VENV/bin/pip" install certifi
fi
"$PY" - <<'PY'
import ssl, sys
try:
    import certifi
    ctx = ssl.create_default_context(); ctx.load_verify_locations(cafile=certifi.where())
except Exception:
    ctx = ssl.create_default_context()
n = ctx.cert_store_stats().get("x509_ca", 0)
print(f"     CA roots loaded: {n}")
sys.exit(0 if n > 0 else 1)
PY

echo "[2/3] Stopping any stale proxy on :$PORT"
if [ -f "$HOME/.claude/rolling-context-proxy.pid" ]; then
    kill "$(cat "$HOME/.claude/rolling-context-proxy.pid")" 2>/dev/null || true
fi
pkill -f "rolling-context.*server.py" 2>/dev/null || true
sleep 1

echo "[3/3] Starting proxy detached (setsid, survives the spawning session)"
cd "$PROXY_DIR"
ROLLING_CONTEXT_PYTHON="$PY" setsid "$PY" server.py \
    > "$HOME/.claude/rolling-context-proxy.log" 2>&1 < /dev/null &
sleep 4

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
if [ "$code" = "200" ]; then
    echo "     proxy healthy on :$PORT (HTTP 200)"
else
    echo "     WARNING: :$PORT returned '$code' — see ~/.claude/rolling-context-debug.log"
fi
