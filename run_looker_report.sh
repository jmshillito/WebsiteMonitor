#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${WEBSITE_MONITOR_STAGED:-0}" != "1" ] && [ ! -w "$SOURCE_DIR" ]; then
  STAGE_DIR="${WEBSITE_MONITOR_STAGE_DIR:-$(mktemp -d /tmp/WebsiteMonitorProject-run.XXXXXX)}"
  mkdir -p "$STAGE_DIR"
  cp -a "$SOURCE_DIR/." "$STAGE_DIR/"
  export WEBSITE_MONITOR_STAGED=1
  export WEBSITE_MONITOR_STAGE_DIR="$STAGE_DIR"
  exec "$STAGE_DIR/run_looker_report.sh" "$@"
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

PROPERTY_MAP_FILE="${GA4_PROPERTY_MAP:-$SCRIPT_DIR/ga4-reporting/property_ids.tsv}"
AUTH_CODE="${GA4_AUTH_CODE:-}"
AUTH_MODE="${GA4_AUTH_MODE:-oauth}"
SERVICE_ACCOUNT_FILE="${GA4_SERVICE_ACCOUNT_FILE:-$SCRIPT_DIR/ga4-reporting/keys/service-account.json}"
CLIENT_SECRET_FILE="${GA4_CLIENT_SECRET_FILE:-}"
DRY_RUN=0
MONTH=""
START_DATE=""
END_DATE=""
PROMPT_DATES=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --auth-code)
      AUTH_CODE="${2:-}"
      shift 2
      ;;
    --month)
      MONTH="${2:-}"
      shift 2
      ;;
    --start-date)
      START_DATE="${2:-}"
      shift 2
      ;;
    --end-date)
      END_DATE="${2:-}"
      shift 2
      ;;
    --prompt-dates)
      PROMPT_DATES=1
      shift
      ;;
    --auth-mode)
      AUTH_MODE="${2:-}"
      shift 2
      ;;
    --help|-h)
      echo "usage: $0 [--dry-run] [--auth-mode MODE] [--auth-code CODE] [--prompt-dates] [--month YYYY-MM | --start-date YYYY-MM-DD --end-date YYYY-MM-DD]" >&2
      exit 0
      ;;
    *)
      MONTH="$1"
      shift
      ;;
  esac
done

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

latest_month_from_csv() {
  python3 - "$1" <<'PY'
from pathlib import Path
import csv
import sys

path = Path(sys.argv[1])
months: set[str] = set()
try:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "date" not in (reader.fieldnames or []):
            raise SystemExit(1)
        for row in reader:
            value = str(row.get("date", "")).strip()
            if len(value) >= 7:
                months.add(value[:7])
except OSError:
    raise SystemExit(1)

if not months:
    raise SystemExit(1)

print(sorted(months)[-1])
PY
}

resolve_client_secret_file() {
  if [ -n "$CLIENT_SECRET_FILE" ]; then
    printf '%s\n' "$CLIENT_SECRET_FILE"
    return 0
  fi

  local default_secret="$SCRIPT_DIR/ga4-reporting/keys/client_secret_463400512765-k9faff977lqifvpiqt69263r8c9dnr6e.apps.googleusercontent.com.json"
  if [ -f "$default_secret" ]; then
    printf '%s\n' "$default_secret"
    return 0
  fi

  local candidate
  for candidate in "$SCRIPT_DIR"/ga4-reporting/keys/*.json; do
    [ -f "$candidate" ] || continue
    case "$candidate" in
      *oauth_token.json|*service-account.json)
        continue
        ;;
    esac
    printf '%s\n' "$candidate"
    return 0
  done

  return 1
}

resolve_service_account_file() {
  if [ -n "$SERVICE_ACCOUNT_FILE" ]; then
    printf '%s\n' "$SERVICE_ACCOUNT_FILE"
    return 0
  fi

  local default_service_account="$SCRIPT_DIR/ga4-reporting/keys/service-account.json"
  if [ -f "$default_service_account" ]; then
    printf '%s\n' "$default_service_account"
    return 0
  fi

  return 1
}

month_to_range() {
  local month="$1"
  printf '%s %s\n' \
    "$(date -d "${month}-01" +%Y-%m-01)" \
    "$(date -d "${month}-01 +1 month -1 day" +%Y-%m-%d)"
}

default_previous_month_range() {
  local month
  month="$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m)"
  month_to_range "$month"
}

if [ -n "$MONTH" ] && [ -z "$START_DATE" ] && [ -z "$END_DATE" ]; then
  read -r START_DATE END_DATE < <(month_to_range "$MONTH")
fi

if [ -z "$MONTH" ] && [ -n "$START_DATE" ] && [ -n "$END_DATE" ]; then
  START_MONTH="$(date -d "$START_DATE" +%Y-%m)"
  END_MONTH="$(date -d "$END_DATE" +%Y-%m)"
  if [ "$START_MONTH" = "$END_MONTH" ]; then
    MONTH="$START_MONTH"
  fi
fi

if [ -z "$MONTH" ] && [ -z "$START_DATE" ] && [ -z "$END_DATE" ]; then
  if [ "$PROMPT_DATES" -eq 1 ] && [ -t 0 ]; then
    read -r DEFAULT_START DEFAULT_END < <(default_previous_month_range)
    read -r -p "START DATE [${DEFAULT_START}]: " START_DATE
    START_DATE="${START_DATE:-$DEFAULT_START}"
    read -r -p "END DATE [${DEFAULT_END}]: " END_DATE
    END_DATE="${END_DATE:-$DEFAULT_END}"
    START_MONTH="$(date -d "$START_DATE" +%Y-%m)"
    END_MONTH="$(date -d "$END_DATE" +%Y-%m)"
    if [ "$START_MONTH" = "$END_MONTH" ]; then
      MONTH="$START_MONTH"
    fi
  elif [ "$DRY_RUN" -eq 1 ]; then
    MONTH="$(latest_month_from_csv "$GA4_INPUT" || true)"
    if [ -z "$MONTH" ]; then
      echo "error: no month exists in $GA4_INPUT" >&2
      exit 1
    fi
    read -r START_DATE END_DATE < <(month_to_range "$MONTH")
  elif [ -t 0 ]; then
    read -r DEFAULT_START DEFAULT_END < <(default_previous_month_range)
    read -r -p "Start date [${DEFAULT_START}]: " START_DATE
    START_DATE="${START_DATE:-$DEFAULT_START}"
    read -r -p "End date [${DEFAULT_END}]: " END_DATE
    END_DATE="${END_DATE:-$DEFAULT_END}"
    START_MONTH="$(date -d "$START_DATE" +%Y-%m)"
    END_MONTH="$(date -d "$END_DATE" +%Y-%m)"
    if [ "$START_MONTH" = "$END_MONTH" ]; then
      MONTH="$START_MONTH"
    fi
  else
    read -r START_DATE END_DATE < <(default_previous_month_range)
    MONTH="$(date -d "$START_DATE" +%Y-%m)"
  fi
fi

if [ -n "$MONTH" ] && [ -z "$START_DATE" ] && [ -z "$END_DATE" ]; then
  read -r START_DATE END_DATE < <(month_to_range "$MONTH")
fi

if [ -z "$START_DATE" ] || [ -z "$END_DATE" ]; then
  echo "error: missing date range" >&2
  exit 1
fi

if [ ! -f "$PROPERTY_MAP_FILE" ]; then
  echo "error: property map not found at $PROPERTY_MAP_FILE" >&2
  echo "expected columns: school, ga4_property_id, search_console_property" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
  if [ "$AUTH_MODE" = "service-account" ]; then
    SERVICE_ACCOUNT_FILE="$(resolve_service_account_file || true)"
    if [ -z "$SERVICE_ACCOUNT_FILE" ]; then
      echo "error: no service account JSON found in ga4-reporting/keys" >&2
      echo "expected service-account.json or set GA4_SERVICE_ACCOUNT_FILE" >&2
      exit 1
    fi
  elif [ "$AUTH_MODE" = "oauth" ]; then
    CLIENT_SECRET_FILE="$(resolve_client_secret_file || true)"
    if [ -z "$CLIENT_SECRET_FILE" ]; then
      echo "error: no OAuth client secret JSON found in ga4-reporting/keys" >&2
      echo "expected client_secret_*.json or set GA4_CLIENT_SECRET_FILE" >&2
      exit 1
    fi
  else
    echo "error: unsupported GA4 auth mode: $AUTH_MODE" >&2
    exit 1
  fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
  bootstrap_venv || true
fi

if [ "$DRY_RUN" -eq 0 ] && has_runtime_deps; then
  GA4_CMD=(
    "$PYTHON_BIN"
    "$SCRIPT_DIR/test_ga4.py"
    --property-map "$PROPERTY_MAP_FILE"
    --start-date "$START_DATE"
    --end-date "$END_DATE"
    --output "$SCRIPT_DIR/imports/ga4_daily.csv"
    --token-file "$SCRIPT_DIR/ga4-reporting/keys/oauth_token.json"
    --service-account-file "$SERVICE_ACCOUNT_FILE"
    --auth-mode "$AUTH_MODE"
  )

  if [ "$AUTH_MODE" = "oauth" ]; then
    GA4_CMD+=(--client-secret "$CLIENT_SECRET_FILE")
  fi

  if [ -n "$AUTH_CODE" ]; then
    GA4_CMD+=(--auth-code "$AUTH_CODE")
  fi

  "${GA4_CMD[@]}"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run: skipping Google API export and using local CSV inputs."
  else
    echo "Google client libraries are unavailable; using local CSV inputs."
  fi
fi

GA4_INPUT="$SCRIPT_DIR/imports/ga4_daily.csv"

if [ ! -f "$GA4_INPUT" ] || [ ! -s "$GA4_INPUT" ]; then
  GA4_INPUT="$SCRIPT_DIR/data/ga4_daily.csv"
fi

REPORT_CMD=(
  "$SCRIPT_DIR/run_news_views.sh"
  --ga4 "$GA4_INPUT"
  --start-date "$START_DATE"
  --end-date "$END_DATE"
)

if [ "$PROMPT_DATES" -eq 1 ]; then
  REPORT_CMD+=(--prompt-dates)
fi

if [ -n "$MONTH" ]; then
  REPORT_CMD+=(--month "$MONTH")
fi

exec "${REPORT_CMD[@]}"
