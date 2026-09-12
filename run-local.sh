#!/usr/bin/env bash
#
# Run mediabox on this machine.
#
# It borrows the server's Threadfin for the channel lineup (over Tailscale) and
# talks to the IPTV provider directly for search and the TV guide, so what you see
# locally matches production. The database is a local copy — nothing you click here
# touches the live one.
#
#   ./run-local.sh              start it
#   PORT=9000 ./run-local.sh    on a different port
#   ./run-local.sh --fresh-db   re-pull the database from the server
#
set -euo pipefail
cd "$(dirname "$0")"

SERVER="${MEDIABOX_SERVER:-100.113.186.78}"     # Tailscale IP
SSH_HOST="${MEDIABOX_SSH:-oli@192.168.0.34}"    # LAN, works even if Tailscale is down
DATA="$PWD/.local-data"   # absolute: SQLAlchemy cannot resolve a relative sqlite path
PORT="${PORT:-8888}"

[ "${1:-}" = "--fresh-db" ] && rm -f "$DATA/mediabox.db"

# ── dependencies ──────────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
  echo "→ creating .venv"
  python3 -m venv .venv
fi
echo "→ installing dependencies"
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# ── config ────────────────────────────────────────────────────────────────────
# Parsed rather than sourced: provider URLs contain & and ? which the shell would eat.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|'#'*) continue ;; esac
    value="${value%\"}"; value="${value#\"}"
    export "$key=$value"
  done < .env
fi

if [ -z "${M3U_URL:-}" ]; then
  echo "✗ M3U_URL is not set. Add M3U_URL and EPG_URL to .env (see README)." >&2
  exit 1
fi

export MEDIABOX_DATA_DIR="$DATA"
export THREADFIN_URL="${THREADFIN_URL:-http://$SERVER:34400}"
export JWT_SECRET="${JWT_SECRET:-local-development-only}"
export STREAM_LIMIT="${STREAM_LIMIT:-1}"
export EPG_WINDOW_HOURS="${EPG_WINDOW_HOURS:-72}"
export PYTHONUNBUFFERED=1

# ── database ──────────────────────────────────────────────────────────────────
mkdir -p "$DATA"
if [ ! -f "$DATA/mediabox.db" ]; then
  echo "→ copying the database from the server"
  if scp -q "$SSH_HOST:mediabox/data/mediabox.db" "$DATA/mediabox.db" 2>/dev/null; then
    echo "  got it — log in with your normal account"
  else
    echo "  server unreachable; creating a local admin instead (local / local)"
    ./.venv/bin/python cli.py add-user local local --admin >/dev/null
  fi
fi

# ── sanity check ──────────────────────────────────────────────────────────────
if ! curl -sf -m 5 -o /dev/null "$THREADFIN_URL/lineup.json"; then
  echo "⚠ Threadfin at $THREADFIN_URL is unreachable — the channel list will be empty."
  echo "  Is Tailscale up? (tailscale status)"
fi

echo
echo "→ mediabox on http://127.0.0.1:$PORT"
echo "  lineup from : $THREADFIN_URL"
echo "  database    : $DATA/mediabox.db (local copy)"
echo
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
