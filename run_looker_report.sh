#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

PROPERTY_MAP_FILE="${GA4_PROPERTY_MAP:-$SCRIPT_DIR/ga4-reporting/property_ids.tsv}"
AUTH_CODE="${GA4_AUTH_CODE:-}"
AUTH_MODE="${GA4_AUTH_MODE:-oauth}"
SERVICE_ACCOUNT_FILE="${GA4_SERVICE_ACCOUNT_FILE:-$SCRIPT_DIR/ga4-reporting/keys/service-account.json}"
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

latest_common_month_from_csvs() {
  python3 - "$SCRIPT_DIR/imports/ga4_daily.csv" "$SCRIPT_DIR/imports/rss_posts.csv" <<'PY'
from pathlib import Path
import csv
import sys

ga4_path = Path(sys.argv[1])
rss_path = Path(sys.argv[2])

def months_from_csv(path: Path, date_field: str) -> set[str]:
    months: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if date_field not in (reader.fieldnames or []):
                return months
            for row in reader:
                value = str(row.get(date_field, "")).strip()
                if len(value) >= 7:
                    months.add(value[:7])
    except OSError:
        return months
    return months

ga4_months = months_from_csv(ga4_path, "date")
rss_months = months_from_csv(rss_path, "last date")
common = sorted(ga4_months & rss_months)

if not common:
    raise SystemExit(1)

print(common[-1])
PY
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
    MONTH="$(latest_common_month_from_csvs || true)"
    if [ -z "$MONTH" ]; then
      echo "error: no common month exists in imports/ga4_daily.csv and imports/rss_posts.csv" >&2
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

if [ ! -x "$PYTHON_BIN" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$PIP_BIN" install -r "$SCRIPT_DIR/requirements.txt"

if [ ! -f "$PROPERTY_MAP_FILE" ]; then
  echo "error: property map not found at $PROPERTY_MAP_FILE" >&2
  echo "expected columns: school, ga4_property_id, search_console_property" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
  "$PYTHON_BIN" "$SCRIPT_DIR/school_news_feed.py" --csv-output "$SCRIPT_DIR/imports/rss_posts.csv"

  GA4_CMD=(
    "$PYTHON_BIN"
    "$SCRIPT_DIR/test_ga4.py"
    --property-map "$PROPERTY_MAP_FILE"
    --start-date "$START_DATE"
    --end-date "$END_DATE"
    --output "$SCRIPT_DIR/imports/ga4_daily.csv"
    --client-secret "$SCRIPT_DIR/ga4-reporting/keys/client_secret_754186957411-ggvs2h3f6pes5checp32fkqlhrjbqcjt.apps.googleusercontent.com.json"
    --token-file "$SCRIPT_DIR/ga4-reporting/keys/oauth_token.json"
    --service-account-file "$SERVICE_ACCOUNT_FILE"
    --auth-mode "$AUTH_MODE"
  )

  if [ -n "$AUTH_CODE" ]; then
    GA4_CMD+=(--auth-code "$AUTH_CODE")
  fi

  "${GA4_CMD[@]}"
else
  echo "Dry run: skipping Google API export and using local CSV inputs."
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exec "$SCRIPT_DIR/run_news_views.sh" \
    --ga4 "$SCRIPT_DIR/imports/ga4_daily.csv" \
    --rss "$SCRIPT_DIR/imports/rss_posts.csv" \
    --spreadsheet-id "" \
    --start-date "$START_DATE" \
    --end-date "$END_DATE"
fi

REPORT_CMD=(
  "$SCRIPT_DIR/run_news_views.sh"
  --ga4 "$SCRIPT_DIR/imports/ga4_daily.csv"
  --rss "$SCRIPT_DIR/imports/rss_posts.csv"
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
