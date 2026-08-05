#!/usr/bin/env bash
# Update Eva Foods Dashboard from GitHub ZIP (no git required).
# Preserves data/ and .venv/ — but refreshes the installed Python package.
#
# Usage:
#   bash scripts/update.sh ~/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203
#
# One-liner:
#   curl -fsSL "https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/cursor/sales-dashboard-pdf-8203/scripts/update.sh?$(date +%s)" | bash -s -- ~/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203

set -euo pipefail

REPO="${EVA_UPDATE_REPO:-ssashfaque-creator/Eva-Foods-Dashboard}"
BRANCH="${EVA_UPDATE_BRANCH:-cursor/sales-dashboard-pdf-8203}"
TARGET="${1:-${EVA_HOME:-$HOME/Eva-Foods-Dashboard}}"

if [[ ! -d "$TARGET" ]]; then
  echo "Install folder not found: $TARGET" >&2
  echo "Pass the folder path, e.g.:" >&2
  echo "  bash update.sh ~/Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203" >&2
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

# Drop stale installed copies (non-editable installs keep running old code)
if [[ -d "$ROOT/.venv" ]]; then
  echo "Clearing stale package installs in .venv …"
  find "$ROOT/.venv" -type d -name 'eva_dashboard' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" -type d -name 'eva_dashboard-*.dist-info' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" -type d -name 'eva_dashboard*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/.venv" \( -name 'eva_dashboard*.egg-link' -o -name '__editable__.eva_dashboard*' -o -name '__editable___eva_dashboard*' \) -delete 2>/dev/null || true
  find "$ROOT/eva_dashboard" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
fi

PIP=""
PY=""
if [[ -x "$ROOT/.venv/bin/pip" ]]; then
  PIP="$ROOT/.venv/bin/pip"
  PY="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/Scripts/pip.exe" ]]; then
  PIP="$ROOT/.venv/Scripts/pip.exe"
  PY="$ROOT/.venv/Scripts/python.exe"
else
  PIP="python3 -m pip"
  PY="python3"
fi

echo "Reinstalling editable package…"
# shellcheck disable=SC2086
$PIP install -e "$ROOT" --force-reinstall --no-deps
# shellcheck disable=SC2086
$PIP install -e "$ROOT"

echo
echo "Verify install:"
# shellcheck disable=SC2086
$PY - <<'PY'
import inspect
import eva_dashboard
import eva_dashboard.app as app
print("  version :", eva_dashboard.__version__)
print("  package :", inspect.getfile(eva_dashboard))
print("  app     :", inspect.getfile(app))
print("  has _for_display:", hasattr(app, "_for_display"))
if not hasattr(app, "_for_display"):
    raise SystemExit("ERROR: old app.py still loaded — update failed")
if eva_dashboard.__version__ < "0.2.5":
    raise SystemExit(f"ERROR: version is {eva_dashboard.__version__}, expected >= 0.2.5")
print("  OK")
PY

echo
echo "Update complete. Restart the app:"
echo "  cd \"$ROOT\""
echo "  source .venv/bin/activate"
echo "  eva-dashboard app"
