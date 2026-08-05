#!/usr/bin/env bash
# Update Eva Foods Dashboard from GitHub ZIP (no git required).
# Preserves data/ and .venv/
#
# Usage:
#   ./scripts/update.sh
#   ./scripts/update.sh ~/Eva-Foods-Dashboard
#   EVA_UPDATE_BRANCH=main ./scripts/update.sh
#
# One-time bootstrap (from anywhere):
#   curl -fsSL https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/cursor/sales-dashboard-pdf-8203/scripts/update.sh | bash -s -- ~/Eva-Foods-Dashboard

set -euo pipefail

REPO="${EVA_UPDATE_REPO:-ssashfaque-creator/Eva-Foods-Dashboard}"
BRANCH="${EVA_UPDATE_BRANCH:-cursor/sales-dashboard-pdf-8203}"
TARGET="${1:-${EVA_HOME:-$HOME/Eva-Foods-Dashboard}}"

if [[ ! -d "$TARGET" ]]; then
  echo "Install folder not found: $TARGET" >&2
  echo "Pass the folder path:  bash update.sh ~/Eva-Foods-Dashboard" >&2
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
SRC="$(find "$TMP/extracted" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [[ -z "$SRC" ]]; then
  echo "error: unexpected ZIP layout" >&2
  exit 1
fi

shopt -s nullglob
for item in "$SRC"/* "$SRC"/.[!.]* "$SRC"/..?*; do
  [[ -e "$item" ]] || continue
  name="$(basename "$item")"
  case "$name" in
    data|.venv|venv|.git|.env|__pycache__|.pytest_cache|.|. ..) continue ;;
  esac
  if [[ "$name" == .* && "$name" != ".gitignore" && "$name" != ".streamlit" ]]; then
    continue
  fi
  rm -rf "$ROOT/$name"
  cp -R "$item" "$ROOT/$name"
  echo "  updated $name"
done

if [[ -x "$ROOT/.venv/bin/pip" ]]; then
  "$ROOT/.venv/bin/pip" install -e "$ROOT"
elif [[ -x "$ROOT/.venv/Scripts/pip.exe" ]]; then
  "$ROOT/.venv/Scripts/pip.exe" install -e "$ROOT"
else
  python3 -m pip install -e "$ROOT"
fi

echo
echo "Update complete. Restart the app:"
echo "  cd \"$ROOT\""
echo "  source .venv/bin/activate"
echo "  eva-dashboard app"
