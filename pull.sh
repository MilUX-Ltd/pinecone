#!/bin/bash
# Pull a window of a-* and b-t-f CoT (positions and GeoChat) out of TAK Server's cot_router table into data/ as CSV.
#
#   ./pull.sh 2026-09-03T00:00Z 2026-09-04T00:00Z user@tak-box    # over ssh to the box (or set PINECONE_HOST)
#   ./pull.sh --local 2026-09-03T00:00Z 2026-09-04T00:00Z         # run this on the TAK box itself
#
# Where the credential comes from, in order: PGPASSWORD if already set; the
# <connection> element of /opt/tak/CoreConfig.xml (readable by root or tak);
# /opt/tak/CoreConfig.example.xml (readable by everyone on a stock install and
# carrying the same generated password). It is never printed. PGHOST, PGPORT,
# PGUSER and PGDATABASE override what CoreConfig says.
set -euo pipefail
LOCAL=0
if [ "${1:-}" = "--local" ]; then LOCAL=1; shift; fi
START="${1:?start, e.g. 2026-09-03T00:00Z}"; END="${2:?end, e.g. 2026-09-04T00:00Z}"
HOST="${3:-${PINECONE_HOST:-}}"
[ "$LOCAL" = 1 ] || [ -n "$HOST" ] || { echo "usage: pull.sh [--local] START END [user@tak-box]   (or set PINECONE_HOST)" >&2; exit 2; }
cd "$(dirname "$0")" || exit 2
mkdir -p data
OUT="data/cot_${START//:/}_${END//:/}.csv"

read -r -d '' ON_BOX <<'REMOTE' || true
set -euo pipefail
START="$1"; END="$2"
conn() { { grep -oE '<connection[^>]*>' "$1" 2>/dev/null || true; } | head -1; }   # unreadable file is not an error here
C=$(conn /opt/tak/CoreConfig.xml); [ -n "$C" ] || C=$(conn /opt/tak/CoreConfig.example.xml)
if [ -z "${PGPASSWORD:-}" ]; then
  PGPASSWORD=$(printf '%s' "$C" | grep -oE 'password="[^"]+"' | sed 's/^password="//; s/"$//')
  export PGPASSWORD
fi
URL=$(printf '%s' "$C" | grep -oE 'url="[^"]+"' | sed 's/^url="//; s/"$//')
HP=$(printf '%s' "$URL" | sed -nE 's#^jdbc:postgresql://([^/]+)/([^?]+).*#\1#p')
export PGHOST="${PGHOST:-${HP%%:*}}" PGPORT="${PGPORT:-${HP##*:}}"
export PGUSER="${PGUSER:-$(printf '%s' "$C" | grep -oE 'username="[^"]+"' | sed 's/^username="//; s/"$//')}"
export PGDATABASE="${PGDATABASE:-$(printf '%s' "$URL" | sed -nE 's#^jdbc:postgresql://[^/]+/([^?]+).*#\1#p')}"
[ -n "$PGPASSWORD" ] || { echo "no database password found: set PGPASSWORD" >&2; exit 3; }
export PGTZ=UTC
psql -At -c "COPY (
  SELECT id, uid, cot_type, how, start, time, stale, servertime,
         ST_Y(event_pt) AS lat, ST_X(event_pt) AS lon, point_hae, point_ce, point_le, detail
  FROM cot_router
  WHERE (cot_type LIKE 'a-%' OR cot_type LIKE 'b-t-f%') AND event_pt IS NOT NULL
    AND servertime >= '$START'::timestamptz AND servertime < '$END'::timestamptz
  ORDER BY servertime, id
) TO STDOUT WITH (FORMAT csv, HEADER)"
REMOTE

if [ "$LOCAL" = 1 ]; then
  bash -s -- "$START" "$END" <<<"$ON_BOX" > "$OUT" || { rm -f "$OUT"; echo "pull failed" >&2; exit 3; }
else
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$HOST" 'bash -s' -- "$START" "$END" <<<"$ON_BOX" > "$OUT" || { rm -f "$OUT"; echo "pull failed" >&2; exit 3; }
fi
echo "wrote $OUT: $(($(wc -l < "$OUT") - 1)) rows, $(du -h "$OUT" | cut -f1)"
