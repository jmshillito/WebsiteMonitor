#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${WEBSITE_MONITOR_STAGED:-0}" != "1" ] && [ ! -w "$SOURCE_DIR" ]; then
  STAGE_DIR="${WEBSITE_MONITOR_STAGE_DIR:-$(mktemp -d /tmp/WebsiteMonitorProject-run.XXXXXX)}"
  mkdir -p "$STAGE_DIR"
  cp -a "$SOURCE_DIR/." "$STAGE_DIR/"
  export WEBSITE_MONITOR_STAGED=1
  export WEBSITE_MONITOR_STAGE_DIR="$STAGE_DIR"
  exec "$STAGE_DIR/run_gsc_probe.sh" "$@"
fi

SCRIPT_DIR="$SOURCE_DIR"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
PYTHON_BIN_OVERRIDE="${PYTHON_BIN:-}"
if [ -n "$PYTHON_BIN_OVERRIDE" ]; then
  PYTHON_BIN="$PYTHON_BIN_OVERRIDE"
  PYTHON_BIN_SOURCE="override"
elif [ -x "$VENV_DIR/bin/python" ]; then
  PYTHON_BIN="$VENV_DIR/bin/python"
  PYTHON_BIN_SOURCE="venv"
else
  PYTHON_BIN="python3"
  PYTHON_BIN_SOURCE="system"
fi
CLIENT_SECRET_FILE="${GA4_CLIENT_SECRET_FILE:-$SCRIPT_DIR/ga4-reporting/keys/client_secret_463400512765-k9faff977lqifvpiqt69263r8c9dnr6e.apps.googleusercontent.com.json}"
GSC_TOKEN_FILE="${GA4_GSC_TOKEN_FILE:-$SCRIPT_DIR/ga4-reporting/keys/gsc_oauth_token.json}"
SITE_URL="${1:-https://schools.edu.ky/chhs/}"
SEARCH_TYPE="${2:-web}"

has_runtime_deps() {
  "$PYTHON_BIN" - <<'PY'
import importlib.util
mods = [
    "google.analytics.data_v1beta",
    "google_auth_oauthlib",
    "googleapiclient.discovery",
    "google.oauth2.service_account",
    "requests",
]
def exists(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except ModuleNotFoundError:
        return False

raise SystemExit(0 if all(exists(mod) for mod in mods) else 1)
PY
}

bootstrap_venv() {
  if [ "$PYTHON_BIN_SOURCE" = "override" ]; then
    return 0
  fi

  if [ -x "$VENV_DIR/bin/python" ]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
    if has_runtime_deps; then
      return 0
    fi
    echo "Refreshing .venv dependencies..." >&2
    if ! "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE"; then
      echo "warning: dependency install failed; continuing without required runtime libraries." >&2
      return 1
    fi
    return 0
  fi

  if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "warning: requirements.txt not found; skipping virtualenv bootstrap." >&2
    return 1
  fi

  echo "Bootstrapping .venv and installing Python dependencies..." >&2
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    echo "warning: failed to create .venv; continuing without required runtime libraries." >&2
    return 1
  fi

  PYTHON_BIN="$VENV_DIR/bin/python"
  if ! "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE"; then
    echo "warning: dependency install failed; continuing without required runtime libraries." >&2
    return 1
  fi

  return 0
}

bootstrap_venv || true

exec "$PYTHON_BIN" "$SCRIPT_DIR/test_ga4.py" \
  --gsc-only \
  --gsc-site-url "$SITE_URL" \
  --gsc-search-type "$SEARCH_TYPE" \
  --client-secret "$CLIENT_SECRET_FILE" \
  --gsc-token-file "$GSC_TOKEN_FILE"
