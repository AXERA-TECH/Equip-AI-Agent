#!/usr/bin/env bash
# Expose the local Cat debug UI through Cloudflare Tunnel.
#
# This file is intentionally only a launcher. It does not install cloudflared,
# start the local UI, or run a tunnel until a user invokes it explicitly:
#
#   ./scripts/cloudflare-tunnel.sh quick
#   CLOUDFLARE_TUNNEL_TOKEN=... ./scripts/cloudflare-tunnel.sh named
#
# For production, prefer a named tunnel with an Access policy. Quick tunnels
# are temporary and should not be used for a control interface.

set -Eeuo pipefail

MODE="${1:-quick}"
LOCAL_URL="${CAT_DEBUG_UI_URL:-http://127.0.0.1:8765}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-cloudflared}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/cloudflare-tunnel.sh quick
  CLOUDFLARE_TUNNEL_TOKEN=... ./scripts/cloudflare-tunnel.sh named

Environment:
  CAT_DEBUG_UI_URL       Local service URL (default: http://127.0.0.1:8765)
  CLOUDFLARED_BIN        cloudflared executable (default: cloudflared)
  CLOUDFLARE_TUNNEL_TOKEN Required by the named mode

The local debug UI must already be running. This script never starts it.
EOF
}

if [[ "${MODE}" == "-h" || "${MODE}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${MODE}" != "quick" && "${MODE}" != "named" ]]; then
  echo "Unsupported mode: ${MODE}" >&2
  usage >&2
  exit 2
fi

if ! command -v "${CLOUDFLARED_BIN}" >/dev/null 2>&1; then
  echo "cloudflared was not found: ${CLOUDFLARED_BIN}" >&2
  echo "Install it separately, then invoke this script again." >&2
  exit 127
fi

if [[ "${MODE}" == "named" ]]; then
  if [[ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
    echo "CLOUDFLARE_TUNNEL_TOKEN is required for named mode." >&2
    exit 2
  fi
  exec "${CLOUDFLARED_BIN}" tunnel --no-autoupdate run --token "${CLOUDFLARE_TUNNEL_TOKEN}"
fi

exec "${CLOUDFLARED_BIN}" tunnel --no-autoupdate --url "${LOCAL_URL}"
