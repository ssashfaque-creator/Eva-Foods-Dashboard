#!/usr/bin/env bash
# Update Eva Foods Dashboard from GitHub ZIP (no git required).
# Preserves data/ and .venv/
#
# Usage:
#   bash scripts/update.sh ~/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203
#
# One-liner (use commit/raw URL so GitHub CDN cannot serve a stale script):
#   curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/5e72391ffa422b032327008374b9ce1241eb455a/scripts/update.sh" | bash -s -- "$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203"

set -euo pipefail

REPO="${EVA_UPDATE_REPO:-ssashfaque-creator/Eva-Foods-Dashboard}"
BRANCH="${EVA_UPDATE_BRANCH:-cursor/ai-chatbot-data-testing-ed65}"
TARGET="${1:-${EVA_HOME:-$HOME/Eva-Foods-Dashboard}}"

if [ ! -d "$TARGET" ]; then
  echo "Install folder not found: $TARGET" >&2
  echo "Example:" >&2
  echo "  bash update.sh \$HOME/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203" >&2
  exit 1
fi

ROOT="$(cd "$TARGET" && pwd)"
URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/eva-update.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "Updating: $ROOT"
echo "From:     $URL"

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

# Also refresh .gitignore if present in the ZIP
if [ -f "$SRC/.gitignore" ]; then
  cp "$SRC/.gitignore" "$ROOT/.gitignore"
fi

# Drop stale installed copies that shadow the project source
if [ -d "$ROOT/.venv" ]; then
  echo "Clearing stale package installs in .venv ..."
  find "$ROOT/.venv" -type d -name 'eva_dashboard' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" -type d -name 'eva_dashboard-*.dist-info' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" -type d -name 'eva_dashboard*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" \( -name 'eva_dashboard*.egg-link' -o -name '__editable__.eva_dashboard*' -o -name '__editable___eva_dashboard*' \) -delete 2>/dev/null || true
  find "$ROOT/eva_dashboard" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
fi

if [ -x "$ROOT/.venv/bin/pip" ]; then
  PIP="$ROOT/.venv/bin/pip"
  PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/Scripts/pip.exe" ]; then
  PIP="$ROOT/.venv/Scripts/pip.exe"
  PY="$ROOT/.venv/Scripts/python.exe"
else
  echo "error: .venv not found in $ROOT — create it first with: python3 -m venv .venv" >&2
  exit 1
fi

echo "Reinstalling package..."
"$PIP" install -e "$ROOT" --force-reinstall --no-deps
"$PIP" install -e "$ROOT"

echo
echo "Verify install:"
"$PY" - <<'PY'
import inspect
import eva_dashboard
import eva_dashboard.app as app

print("  version :", eva_dashboard.__version__)
print("  package :", inspect.getfile(eva_dashboard))
print("  app     :", inspect.getfile(app))
print("  has _for_display:", hasattr(app, "_for_display"))
if not hasattr(app, "_for_display"):
    raise SystemExit("ERROR: old app.py still loaded — update failed")
print("  OK")
PY

echo
echo "Update complete. Restart the app:"
echo "  cd \"$ROOT\""
echo "  source .venv/bin/activate"
echo "  eva-dashboard app"
