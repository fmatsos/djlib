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

# Symlinked into both /usr/local/bin and /usr/bin: `pct enter` attaches via
# `lxc-attach`, a non-login shell that skips /etc/profile entirely and can
# start with a bare PATH (no /usr/local/bin) depending on the container's
# base image -- /usr/bin is in that minimal PATH regardless.
ln -sf "$VENV/bin/djlib" /usr/local/bin/djlib
ln -sf "$VENV/bin/djlib" /usr/bin/djlib

# `djlib doctor`'s migration check and `djlib rebuild` also need to find this
# checkout without relying on DJLIB_REPO_ROOT actually reaching the process:
# the same non-login-shell gap above means /etc/profile.d/djlib.sh (below)
# is never sourced by a `pct enter` session either. This marker file, read
# from the venv's own `sys.prefix` (see `djlib.doctor._repo_root`), works
# regardless of how djlib ends up invoked.
printf '%s\n' "$SRC_DIR" > "$VENV/.djlib-repo-root"

cat > /etc/profile.d/djlib.sh <<EOF
export DJLIB_CONFIG="$CONFIG_DIR/config.toml"
export DJLIB_REPO_ROOT="$SRC_DIR"
EOF

# Also written to the interactive-shell rc (Debian's bash reads this for
# every interactive shell, login or not) so a plain `pct enter` session gets
# DJLIB_CONFIG/DJLIB_REPO_ROOT too, not just a true login shell.
touch /etc/bash.bashrc
sed -i '/# BEGIN djlib env/,/# END djlib env/d' /etc/bash.bashrc
cat >> /etc/bash.bashrc <<EOF
# BEGIN djlib env
export DJLIB_CONFIG="$CONFIG_DIR/config.toml"
export DJLIB_REPO_ROOT="$SRC_DIR"
# END djlib env
EOF

export DJLIB_CONFIG="$CONFIG_DIR/config.toml"
export DJLIB_REPO_ROOT="$SRC_DIR"
"$VENV/bin/alembic" -c "$SRC_DIR/alembic.ini" upgrade head
"$VENV/bin/djlib" doctor
