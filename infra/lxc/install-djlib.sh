#!/usr/bin/env bash
# Installs (or updates) djlib itself inside the LXC container: fetches the
# source, installs it into the venv created by bootstrap.sh, lays down the
# default config and /data layout, then migrates the database.
#
# Safe to re-run: re-running fetches the latest REPO_REF, reinstalls the
# package and re-applies migrations, without touching an existing config.toml
# or the catalogue.
set -Eeuo pipefail

REPO_URL="${DJLIB_REPO_URL:-https://github.com/fmatsos/djlib.git}"
REPO_REF="${DJLIB_REPO_REF:-main}"
SRC_DIR="${DJLIB_SRC_DIR:-/opt/djlib}"
VENV="${DJLIB_VENV:-/opt/djlib-venv}"
CONFIG_DIR="${DJLIB_CONFIG_DIR:-/etc/djlib}"
DATA_ROOT="${DJLIB_DATA_ROOT:-/data}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "error: $VENV not found -- run bootstrap.sh first" >&2
  exit 1
fi

if [ -d "$SRC_DIR/.git" ]; then
  git -C "$SRC_DIR" fetch --depth 1 origin "$REPO_REF"
  git -C "$SRC_DIR" checkout --detach "origin/$REPO_REF"
else
  git clone --branch "$REPO_REF" --depth 1 "$REPO_URL" "$SRC_DIR"
fi

"$VENV/bin/python" -m pip install --upgrade "$SRC_DIR"

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  cp "$SRC_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
fi

mkdir -p \
  "$DATA_ROOT/cache/chromaprint" \
  "$DATA_ROOT/cache/analysis" \
  "$DATA_ROOT/curation" \
  "$DATA_ROOT/reports" \
  "$DATA_ROOT/decisions" \
  "$DATA_ROOT/logs"

ln -sf "$VENV/bin/djlib" /usr/local/bin/djlib

cat > /etc/profile.d/djlib.sh <<EOF
export DJLIB_CONFIG="$CONFIG_DIR/config.toml"
EOF

export DJLIB_CONFIG="$CONFIG_DIR/config.toml"
"$VENV/bin/alembic" -c "$SRC_DIR/alembic.ini" upgrade head
"$VENV/bin/djlib" doctor
