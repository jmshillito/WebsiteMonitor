#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use the prior full month by default. Pass YYYY-MM as the first argument to override.
MONTH="${1:-$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m)}"

python3 "$SCRIPT_DIR/news_views_impact.py" \
  --ga4 "$SCRIPT_DIR/data/ga4_daily.csv" \
  --rss "$SCRIPT_DIR/data/rss_posts.csv" \
  --output-dir "$SCRIPT_DIR/output" \
  --month "$MONTH"
