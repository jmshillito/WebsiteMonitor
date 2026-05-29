#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"
DEFAULT_GA4="$SCRIPT_DIR/data/ga4_daily.csv"
DEFAULT_RSS="$SCRIPT_DIR/data/rss_posts.csv"
DEFAULT_SPREADSHEET_ID="109tHI2m1olk6oXMZbV_OOneUT7apMbRJCQkYxXldA1I"
DEFAULT_SERVICE_ACCOUNT_FILE="$SCRIPT_DIR/ga4-reporting/keys/service-account.json"
DEFAULT_VIEWS_SHEET_NAME="Views and Clicks"
DEFAULT_POST_IMPACT_SHEET_NAME="Post Impact"
DEFAULT_POST_DETAILS_SHEET_NAME="Post Details"
IMPORTS_DIR="$SCRIPT_DIR/imports"
IMPORTS_GA4="$IMPORTS_DIR/ga4_daily.csv"
IMPORTS_RSS="$IMPORTS_DIR/rss_posts.csv"
MIN_GA4_DATA_ROWS=10
MIN_GA4_DISTINCT_SCHOOLS=5

resolve_path() {
  local path="$1"
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *) printf '%s\n' "$SCRIPT_DIR/$path" ;;
  esac
}

csv_has_data_rows() {
  local file="$1"
  [ -f "$file" ] || return 1
  [ "$(wc -l < "$file")" -gt 1 ]
}

ga4_file_is_tiny() {
  local file="$1"
  python3 - "$file" "$MIN_GA4_DATA_ROWS" "$MIN_GA4_DISTINCT_SCHOOLS" <<'PY'
from pathlib import Path
import csv
import sys

path = Path(sys.argv[1])
min_rows = int(sys.argv[2])
min_schools = int(sys.argv[3])

try:
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
except OSError:
    sys.exit(1)

if len(rows) < min_rows:
    sys.exit(0)

schools = {str(row.get("school", "")).strip().upper() for row in rows if str(row.get("school", "")).strip()}
if len(schools) < min_schools:
    sys.exit(0)

sys.exit(1)
PY
}

stage_input_csv() {
  local source="$1"
  local target="$2"
  mkdir -p "$IMPORTS_DIR"
  if [ "$source" != "$target" ]; then
    cp "$source" "$target"
  fi
}

discover_ga4_csv() {
  python3 - "$SCRIPT_DIR" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
imports_dir = root / "imports"
excluded_parts = {"output", ".venv", "__pycache__"}
required_header = "school,date,views"
best_path = None
best_mtime = -1.0

candidate_roots = []
if imports_dir.exists():
    candidate_roots.append(imports_dir)
for home_candidate in [
    Path.home() / "Downloads",
    Path.home() / "Desktop",
]:
    if home_candidate.exists():
        candidate_roots.append(home_candidate)
candidate_roots.append(root)

for search_root in candidate_roots:
  for path in search_root.rglob("*.csv"):
    if any(part in excluded_parts for part in path.parts):
        continue
    if path.name.startswith("news_posts_dated_"):
        continue
    try:
        if path.stat().st_size == 0:
            continue
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            header = f.readline().strip().lower()
            second_line = f.readline()
        if header != required_header or not second_line:
            continue
        mtime = path.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = path
    except OSError:
        continue

if best_path:
    print(best_path)
PY
}

latest_news_posts_csv() {
  python3 - "$SCRIPT_DIR" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
imports_dir = root / "imports"
best_path = None
best_mtime = -1.0

candidate_paths = []
if imports_dir.exists():
    candidate_paths.append(imports_dir / "rss_posts.csv")
candidate_paths.extend(sorted(root.glob("news_posts_dated_*.csv")))

for path in candidate_paths:
    try:
        if not path.exists() or path.stat().st_size == 0:
            continue
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            header = f.readline().strip().lower()
            second_line = f.readline()
        if header not in {"school,ga4 school,title,last date,link", "school,title,last date,link"}:
            continue
        if header == "school,ga4 school,title,last date,link" and not second_line:
            continue
        if header == "school,title,last date,link" and not second_line:
            continue
        mtime = path.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = path
    except OSError:
        continue

if best_path:
    print(best_path)
PY
}

if [ ! -x "$PYTHON_BIN" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$PIP_BIN" install -r "$SCRIPT_DIR/requirements.txt"

GA4_INPUT="$DEFAULT_GA4"
RSS_INPUT="$DEFAULT_RSS"
OUTPUT_DIR="$SCRIPT_DIR/output"
START_DATE=""
END_DATE=""
SPREADSHEET_ID="$DEFAULT_SPREADSHEET_ID"
SERVICE_ACCOUNT_FILE="$DEFAULT_SERVICE_ACCOUNT_FILE"
VIEWS_SHEET_NAME="$DEFAULT_VIEWS_SHEET_NAME"
POST_IMPACT_SHEET_NAME="$DEFAULT_POST_IMPACT_SHEET_NAME"
POST_DETAILS_SHEET_NAME="$DEFAULT_POST_DETAILS_SHEET_NAME"
PROMPT_DATES=0
EXTRA_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ga4)
      GA4_INPUT="$(resolve_path "${2:-}")"
      shift 2
      ;;
    --rss)
      RSS_INPUT="$(resolve_path "${2:-}")"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$(resolve_path "${2:-}")"
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
    --month)
      EXTRA_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --spreadsheet-id)
      SPREADSHEET_ID="${2:-}"
      shift 2
      ;;
    --service-account-file)
      SERVICE_ACCOUNT_FILE="$(resolve_path "${2:-}")"
      shift 2
      ;;
    --views-sheet-name)
      VIEWS_SHEET_NAME="${2:-}"
      shift 2
      ;;
    --post-impact-sheet-name)
      POST_IMPACT_SHEET_NAME="${2:-}"
      shift 2
      ;;
    --post-details-sheet-name)
      POST_DETAILS_SHEET_NAME="${2:-}"
      shift 2
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if ! csv_has_data_rows "$GA4_INPUT"; then
  if [ -f "$IMPORTS_GA4" ] && csv_has_data_rows "$IMPORTS_GA4"; then
    GA4_INPUT="$IMPORTS_GA4"
    echo "Using imports GA4 export: $GA4_INPUT"
  else
    AUTO_GA4="$(discover_ga4_csv || true)"
    if [ -n "${AUTO_GA4:-}" ]; then
      GA4_INPUT="$AUTO_GA4"
      echo "Using discovered GA4 export: $GA4_INPUT"
    else
      echo "error: no GA4 export with school,date,views rows was found" >&2
      exit 1
    fi
  fi
fi

if ga4_file_is_tiny "$GA4_INPUT"; then
  echo "error: GA4 export looks too small to be a real daily report. Provide a full export with at least $MIN_GA4_DATA_ROWS rows and $MIN_GA4_DISTINCT_SCHOOLS schools." >&2
  exit 1
fi

if [ "$PROMPT_DATES" -eq 1 ] && [ -z "$START_DATE" ] && [ -z "$END_DATE" ]; then
  if [ -t 0 ]; then
    DEFAULT_START="$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m-01)"
    DEFAULT_END="$(date -d "$(date +%Y-%m-01) -1 day" +%Y-%m-%d)"
    read -r -p "START DATE [${DEFAULT_START}]: " START_DATE
    START_DATE="${START_DATE:-$DEFAULT_START}"
    read -r -p "END DATE [${DEFAULT_END}]: " END_DATE
    END_DATE="${END_DATE:-$DEFAULT_END}"
  else
    echo "error: --prompt-dates requires an interactive terminal" >&2
    exit 1
  fi
fi

stage_input_csv "$GA4_INPUT" "$IMPORTS_GA4"
GA4_INPUT="$IMPORTS_GA4"

if ! csv_has_data_rows "$RSS_INPUT"; then
  AUTO_RSS="$(latest_news_posts_csv || true)"
  if [ -n "${AUTO_RSS:-}" ]; then
    RSS_INPUT="$AUTO_RSS"
    echo "Using latest RSS export: $RSS_INPUT"
  else
    echo "error: no dated RSS export (news_posts_dated_*.csv) was found" >&2
    exit 1
  fi
fi

stage_input_csv "$RSS_INPUT" "$IMPORTS_RSS"
RSS_INPUT="$IMPORTS_RSS"

REPORT_CMD=(
  "$PYTHON_BIN"
  "$SCRIPT_DIR/news_views_impact.py"
  --ga4 "$GA4_INPUT"
  --rss "$RSS_INPUT"
  --output-dir "$OUTPUT_DIR"
  --spreadsheet-id "$SPREADSHEET_ID"
  --service-account-file "$SERVICE_ACCOUNT_FILE"
  --views-sheet-name "$VIEWS_SHEET_NAME"
  --post-impact-sheet-name "$POST_IMPACT_SHEET_NAME"
  --post-details-sheet-name "$POST_DETAILS_SHEET_NAME"
)

if [ -n "$START_DATE" ]; then
  REPORT_CMD+=(--start-date "$START_DATE")
fi

if [ -n "$END_DATE" ]; then
  REPORT_CMD+=(--end-date "$END_DATE")
fi

REPORT_CMD+=("${EXTRA_ARGS[@]}")

exec "${REPORT_CMD[@]}"
