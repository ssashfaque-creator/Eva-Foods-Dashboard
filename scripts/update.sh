#!/usr/bin/env bash
# Bootstrap / update Eva Foods Dashboard from GitHub ZIP (no git required).
# Always installs into ~/Eva-Foods-Dashboard-new unless you pass a folder.
# Refuses legacy *-sales-dashboard-pdf-* / *-ai-chatbot-data-testing-* paths.
# Preserves data/ and .venv/ when refreshing an existing install.
#
# One-liner (does NOT depend on whatever stale eva-dashboard is on PATH):
#   curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/main/scripts/update.sh" | bash
#   # or branch: .../cursor/react-agent-tools-50eb/scripts/update.sh | bash
#
# Then launch with the FULL PATH printed at the end.

set -euo pipefail

REPO="${EVA_UPDATE_REPO:-ssashfaque-creator/Eva-Foods-Dashboard}"
BRANCH="${EVA_UPDATE_BRANCH:-main}"
MIN_VERSION="${EVA_MIN_VERSION:-1.4.3}"
TARGET="${1:-${EVA_HOME:-$HOME/Eva-Foods-Dashboard-new}}"

case "$TARGET" in
  *sales-dashboard-pdf*|*ai-chatbot-data-testing*)
    echo "error: refusing legacy install folder:" >&2
    echo "  $TARGET" >&2
    echo "Use the canonical home instead (no argument):" >&2
    echo "  bash update.sh" >&2
    echo "  # → $HOME/Eva-Foods-Dashboard-new" >&2
    exit 1
    ;;
esac

mkdir -p "$TARGET"
ROOT="$(cd "$TARGET" && pwd)"
URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/eva-update.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "Updating: $ROOT"
echo "From:     $URL"
echo "Need:     >= v${MIN_VERSION}"

curl -fL --progress-bar -o "$TMP/app.zip" "$URL"
unzip -q "$TMP/app.zip" -d "$TMP/extracted"

SRC="$(find "$TMP/extracted" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$SRC" ]; then
  echo "error: unexpected ZIP layout" >&2
  exit 1
fi

# Copy project files; never touch data/ or .venv/
for item in "$SRC"/*; do
  [ -e "$item" ] || continue
  name="$(basename "$item")"
  case "$name" in
    data|.venv|venv) continue ;;
  esac
  rm -rf "$ROOT/$name"
  cp -R "$item" "$ROOT/$name"
  echo "  updated $name"
done

# Dotfiles we care about
for dot in .gitignore .streamlit; do
  if [ -e "$SRC/$dot" ]; then
    rm -rf "$ROOT/$dot"
    cp -R "$SRC/$dot" "$ROOT/$dot"
  fi
done

echo "$BRANCH" > "$ROOT/.eva-install-branch"

# Create venv on first bootstrap
if [ ! -d "$ROOT/.venv" ]; then
  echo "Creating .venv ..."
  python3 -m venv "$ROOT/.venv"
fi

# Drop stale installed copies that shadow the project source
echo "Clearing stale package installs in .venv ..."
find "$ROOT/.venv" -type d -name 'eva_dashboard' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT/.venv" -type d -name 'eva_dashboard-*.dist-info' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT/.venv" -type d -name 'eva_dashboard*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT/.venv" \( -name 'eva_dashboard*.egg-link' -o -name '__editable__.eva_dashboard*' -o -name '__editable___eva_dashboard*' \) -delete 2>/dev/null || true
if [ -d "$ROOT/eva_dashboard" ]; then
  find "$ROOT/eva_dashboard" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
fi

if [ -x "$ROOT/.venv/bin/pip" ]; then
  PIP="$ROOT/.venv/bin/pip"
  PY="$ROOT/.venv/bin/python"
  BIN="$ROOT/.venv/bin/eva-dashboard"
elif [ -x "$ROOT/.venv/Scripts/pip.exe" ]; then
  PIP="$ROOT/.venv/Scripts/pip.exe"
  PY="$ROOT/.venv/Scripts/python.exe"
  BIN="$ROOT/.venv/Scripts/eva-dashboard.exe"
else
  echo "error: .venv pip not found in $ROOT" >&2
  exit 1
fi

echo "Reinstalling package..."
"$PIP" install -U pip
"$PIP" install -e "$ROOT" --force-reinstall --no-deps
"$PIP" install -e "$ROOT"

echo
echo "Verify install:"
"$PY" - <<PY
import inspect
import re
import sys
from pathlib import Path

import eva_dashboard
import eva_dashboard.app as app

version = eva_dashboard.__version__
print("  version :", version)
print("  package :", inspect.getfile(eva_dashboard))
print("  app     :", inspect.getfile(app))
root = Path(inspect.getfile(eva_dashboard)).resolve().parent.parent
text = str(root).lower()
if "sales-dashboard-pdf" in text or "ai-chatbot-data-testing" in text:
    raise SystemExit(f"ERROR: still on legacy path: {root}")
parts = [int(x) for x in re.findall(r"[0-9]+", version)[:3]]
need = [int(x) for x in re.findall(r"[0-9]+", "${MIN_VERSION}")[:3]]
if tuple(parts) < tuple(need):
    raise SystemExit(f"ERROR: got v{version}, need >= ${MIN_VERSION}")
print("  OK")
PY

echo
echo "Update complete."
echo
echo "IMPORTANT — launch with the FULL PATH (do not type bare eva-dashboard):"
echo "  \"$BIN\" app --data-dir ~/Documents/EvaFoodsData"
echo
echo "Chat banner must show v${MIN_VERSION}+ and path containing Eva-Foods-Dashboard-new."
echo "If you still see sales-dashboard-pdf-8203, you launched the wrong binary."
