#!/usr/bin/env bash
# install.sh - install Pinecone on THIS box, beside its TAK Server (Spec 001, slice 0).
#
#   sudo ./install.sh [--yes] [--bind 127.0.0.1] [--port 8765] [--dry-run]
#
# In order: check the box (root, /opt/tak, python3, PostgreSQL over the local socket); discover
# the server and print what was found and where; ask before touching anything (--yes skips the
# question); create the pinecone system user; lay the release tree in /opt/pinecone; create a
# read-only database role of Pinecone's own (SELECT on cot_router, nothing else) with a generated
# password written only to /etc/pinecone/pinecone.env; write and start pinecone.service bound to
# loopback; write the non-secret discovery to /etc/pinecone/discovery.json; print the URL and the
# exposure line. Idempotent: a second run keeps the credential and reports only the refreshed
# discovery. Nothing here touches the firewall or TAK Server's own configuration.
#
# PINECONE_ROOT=<dir> relocates every absolute path (the suite's box-in-a-directory); --dry-run
# prints each action instead of taking it. Neither is for production use.
set -euo pipefail

ROOT="${PINECONE_ROOT:-}"; YES=0; DRY=0; BIND=127.0.0.1; PORT=8765; BIND_GIVEN=0; PORT_GIVEN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)   YES=1; shift ;;
        --dry-run)  DRY=1; shift ;;
        --bind)     BIND="${2:-}"; BIND_GIVEN=1; shift 2 ;;
        --port)     PORT="${2:-}"; PORT_GIVEN=1; shift 2 ;;
        -h|--help)  sed -n '2,17p' "$0"; exit 0 ;;
        *)          echo "ERR unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ "$BIND" =~ ^[0-9.]{7,15}$ ]] || { echo "ERR bad --bind (an IPv4 address)" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || { echo "ERR bad --port" >&2; exit 2; }

# Where the box is reachable is the operator's decision, and an update must not quietly undo it.
SRC="$(cd "$(dirname "$0")" && pwd)"
V="$(tr -d '[:space:]' < "$SRC/VERSION")"
log() { printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
die() { echo "ERR $*" >&2; exit 2; }
act() { if (( DRY )); then echo "would: $*"; else log "$*"; fi; }
do_() { (( DRY )) && return 0; "$@"; }

# logical paths (what we print) and real paths (what we touch)
L_OPT=/opt/pinecone;                  OPT="$ROOT$L_OPT"
L_ETC=/etc/pinecone;                  ETC="$ROOT$L_ETC"
L_ENV="$L_ETC/pinecone.env";          ENVF="$ROOT$L_ENV"
L_DISC="$L_ETC/discovery.json";       DISC="$ROOT$L_DISC"
L_LIB=/var/lib/pinecone;              LIB="$ROOT$L_LIB"

L_UNIT=/etc/systemd/system/pinecone.service; UNITF="$ROOT$L_UNIT"
SVCUSER=pinecone; ROLE=pinecone

# Where the box answers is the operator's decision, and an update must not quietly undo it. An
# update reconciles the services by re-running this script with no arguments, so a bind that lived
# only in the unit file would be reverted to loopback on the next release, taking Pinecone off the
# network with nothing said. The choice is kept in the environment file beside the credential and
# honoured unless this run was given an explicit --bind or --port. Read through $ENVF rather than a
# second copy of that path, so the two cannot drift apart.
#
# Every box installed before 0.4.0 recorded the operator's choice in the unit file and nowhere
# else, so on the first update to 0.4.0, the very update this mechanism exists to make safe, the
# environment file has nothing to remember and a box deliberately bound off loopback would leave
# the network. The existing unit is read as the fallback: it is the only place a box in the field
# today holds that choice, and it is sitting right there.
# Whether to take the server's history is the operator's choice and lives in the same file as the
# address, for the same reason: this script rewrites that file wholesale on every run, so a setting
# it does not carry forward is a setting an update deletes. Three documents promised this one
# survived an update. It did not, until now.
BACKFILL=yes
if [[ -f "$ENVF" ]]; then
    KEPT_BACKFILL="$(sed -n 's/^PINECONE_BACKFILL=//p' "$ENVF" | head -1 | tr '[:upper:]' '[:lower:]')"
    case "$KEPT_BACKFILL" in no|false|0|off) BACKFILL=no ;; esac
fi
# Whether to record chat beside the positions is the operator's choice too, and it lives here for
# the same reason. Spec 008 promised it survived an update in four places while this script was
# not carrying it forward; the pre-UAT review of slice 5 caught it. Written by default so the
# knob is visible in the file rather than something an operator has to know to add.
CHAT=yes
if [[ -f "$ENVF" ]]; then
    KEPT_CHAT="$(sed -n 's/^PINECONE_CHAT=//p' "$ENVF" | head -1 | tr '[:upper:]' '[:lower:]')"
    case "$KEPT_CHAT" in no|false|0|off) CHAT=no ;; esac
fi
# The record's shape, ODCR (the UK's unit of feedback) or sustain-and-improve (the US's), is the
# unit's own and lives here for the same reason as the two above.
RECORD=odcr
if [[ -f "$ENVF" ]]; then
    KEPT_RECORD="$(sed -n 's/^PINECONE_RECORD=//p' "$ENVF" | head -1 | tr '[:upper:]' '[:lower:]')"
    case "$KEPT_RECORD" in sustain-improve) RECORD=sustain-improve ;; esac
fi
KEPT_BIND=""; KEPT_PORT=""; KEPT_BIND_FROM="$L_ENV"; KEPT_PORT_FROM="$L_ENV"
if [[ -f "$ENVF" ]]; then
    KEPT_BIND="$(sed -n 's/^PINECONE_BIND=//p' "$ENVF" | head -1)"
    KEPT_PORT="$(sed -n 's/^PINECONE_PORT=//p' "$ENVF" | head -1)"
fi
MIGRATED=0
if [[ -z "$KEPT_BIND" && -f "$UNITF" ]]; then
    # Anchored to a real directive: an unanchored match takes a commented-out ExecStart above the
    # live one, which is exactly how an operator leaves the address they have just moved away from.
    KEPT_BIND="$(sed -n 's/^[[:space:]]*ExecStart=.*serve\.py .*--bind \([^ ]*\).*/\1/p' "$UNITF" | tail -1)"
    [[ -n "$KEPT_BIND" ]] && { MIGRATED=1; KEPT_BIND_FROM="$L_UNIT"; }
fi
if [[ -z "$KEPT_PORT" && -f "$UNITF" ]]; then
    KEPT_PORT="$(sed -n 's/^[[:space:]]*ExecStart=.*serve\.py .*--port \([^ ]*\).*/\1/p' "$UNITF" | tail -1)"
    [[ -n "$KEPT_PORT" ]] && KEPT_PORT_FROM="$L_UNIT"
fi
if (( ! BIND_GIVEN )) && (( MIGRATED )) && [[ "$KEPT_BIND" =~ ^[0-9.]{7,15}$ ]]; then
    log "carried the address over from $L_UNIT ($KEPT_BIND:${KEPT_PORT:-$PORT}); it is kept in $L_ENV from now on"
fi
if (( ! BIND_GIVEN )) && [[ -n "$KEPT_BIND" ]]; then
    if [[ "$KEPT_BIND" =~ ^[0-9.]{7,15}$ ]]; then
        BIND="$KEPT_BIND"
    else
        # Say so rather than falling back in silence. Ignoring an operator's own edit without a
        # word is the same fault this whole mechanism exists to stop.
        echo "WARN the address in $KEPT_BIND_FROM is not an IPv4 address ($KEPT_BIND); using $BIND" >&2
    fi
fi
if (( ! PORT_GIVEN )) && [[ -n "$KEPT_PORT" ]]; then
    if [[ "$KEPT_PORT" =~ ^[0-9]{2,5}$ ]]; then
        PORT="$KEPT_PORT"
    else
        echo "WARN the port in $KEPT_PORT_FROM is not a port number ($KEPT_PORT); using $PORT" >&2
    fi
fi

# ---- the box ---------------------------------------------------------------------------------
if [[ -z "$ROOT" && $DRY -eq 0 ]]; then
    [[ $EUID -eq 0 ]] || die "run as root (sudo)"
fi
[[ -d "$ROOT/opt/tak" ]] || die "TAK Server is not installed on this box (/opt/tak missing)"
command -v python3 >/dev/null || die "python3 is missing"
command -v psql >/dev/null || die "psql is missing (it comes with TAK Server's PostgreSQL)"

# ---- discover, show, ask ---------------------------------------------------------------------
log "discovering the TAK Server on this box"
DISC_JSON="$(python3 "$SRC/pinecone_discover.py" --root "${ROOT:-/}" --pinecone-version "$V")"
DBNAME="$(printf '%s' "$DISC_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin)["database"]; print(d.get("database") or "")')"
[[ -n "$DBNAME" ]] || die "could not find the database in /opt/tak/CoreConfig.xml or the example file"
[[ "$DBNAME" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "the database name in CoreConfig is not a plain identifier; refusing to put it in SQL"
psql_as_postgres() { sudo -u postgres psql -At -d "$DBNAME" "$@"; }
if (( DRY )); then
    echo "would: check PostgreSQL answers over the local socket as the postgres user"
else
    psql_as_postgres -c "SELECT 1;" >/dev/null 2>&1 \
        || die "PostgreSQL did not answer over the local socket as the postgres user; Pinecone needs that to create its own role (a Docker-based TAK Server is not supported yet)"
fi
echo
printf '%s' "$DISC_JSON" | python3 -c 'import json,sys; sys.path.insert(0, sys.argv[1]); import pinecone_discover as d; print(d.render_text(json.load(sys.stdin)))' "$SRC"
echo
if (( ! YES )); then
    read -r -p "Install Pinecone $V here with what it found above? It will create a read-only database role of its own. [y/N] " ans || ans=""
    [[ "$ans" =~ ^[Yy] ]] || { echo "not confirmed; nothing was changed"; exit 1; }
fi

# ---- the credential: Pinecone's own read-only role -------------------------------------------
CHANGED=()
HOST="$(printf '%s' "$DISC_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["database"].get("host") or "127.0.0.1")')"
PGPORT="$(printf '%s' "$DISC_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["database"].get("port") or 5432)')"
write_env() {  # the non-secret lines from discovery, the password from $1; never printed
    mkdir -p "$ETC"; umask 027
    printf 'PGHOST=%s\nPGPORT=%s\nPGDATABASE=%s\nPGUSER=%s\nPGPASSWORD=%s\nPINECONE_BIND=%s\nPINECONE_PORT=%s\nPINECONE_BACKFILL=%s\nPINECONE_CHAT=%s\nPINECONE_RECORD=%s\n' \
        "$HOST" "$PGPORT" "$DBNAME" "$ROLE" "$1" "$BIND" "$PORT" "$BACKFILL" "$CHAT" "$RECORD" > "$ENVF"
    chmod 640 "$ENVF"; umask 022
}
role_exists="$(psql_as_postgres -c "SELECT rolname FROM pg_roles WHERE rolname = '$ROLE';" 2>/dev/null || true)"
CRED_CREATED=0
EXISTING_PW=""; [[ -f "$ENVF" ]] && EXISTING_PW="$(sed -n 's/^PGPASSWORD=//p' "$ENVF" | head -1)"
if [[ -n "$EXISTING_PW" && "$role_exists" == "$ROLE" ]]; then
    log "keeping the existing credential ($L_ENV, role $ROLE)"
    (( DRY )) || write_env "$EXISTING_PW"     # refresh the non-secret lines, drop anything stale
else
    PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
    if [[ "$role_exists" == "$ROLE" ]]; then
        act "reset the password of role $ROLE (the env file was missing or empty)"
        (( DRY )) || printf "ALTER ROLE %s WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '%s';\n" "$ROLE" "$PW" | psql_as_postgres >/dev/null 2>&1 \
            || die "could not reset the password of role $ROLE; see PostgreSQL's log on this box"
        CHANGED+=("reset the password of role $ROLE")
    else
        act "create role $ROLE (SELECT on cot_router only)"
        (( DRY )) || printf "CREATE ROLE %s WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '%s';\n" "$ROLE" "$PW" | psql_as_postgres >/dev/null 2>&1 \
            || die "could not create role $ROLE; see PostgreSQL's log on this box"
        CHANGED+=("created role $ROLE")
    fi
    act "write $L_ENV (0640 root:$SVCUSER)"
    (( DRY )) || write_env "$PW"
    CHANGED+=("wrote $L_ENV")
    CRED_CREATED=1
fi
unset EXISTING_PW
unset PW
# The grants run on every install, not only when the role is created: a box installed before chat
# was recorded needs SELECT on cot_router_chat at its next update, and a grant is idempotent.
# GeoChat lives in cot_router_chat on TAK Server 5.8; an older server without that table still
# records positions, and says so.
act "grant $ROLE SELECT on cot_router and cot_router_chat (read only, every run)"
if (( ! DRY )); then
    printf 'GRANT CONNECT ON DATABASE "%s" TO %s;\nGRANT USAGE ON SCHEMA public TO %s;\nGRANT SELECT ON cot_router TO %s;\n' "$DBNAME" "$ROLE" "$ROLE" "$ROLE" | psql_as_postgres >/dev/null \
        || die "could not grant SELECT on cot_router to $ROLE"
    printf 'GRANT SELECT ON cot_router_chat TO %s;\n' "$ROLE" | psql_as_postgres >/dev/null 2>&1 \
        || log "this server has no cot_router_chat table; positions are recorded and chat is not"
fi

# ---- user, tree, directories -----------------------------------------------------------------
if id -u "$SVCUSER" >/dev/null 2>&1; then
    log "user $SVCUSER exists"
else
    act "create system user $SVCUSER"
    do_ useradd --system --home-dir "$L_LIB" --shell /usr/sbin/nologin "$SVCUSER"
    CHANGED+=("created user $SVCUSER")
fi
tree_digest() { ( cd "$1" 2>/dev/null && find . -type f ! -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-16 ) || echo none; }
if [[ "$(cd "$SRC" 2>/dev/null && pwd -P)" == "$(cd "$OPT" 2>/dev/null && pwd -P)" ]]; then
    SELF_INSTALL=1
    log "running from $L_OPT itself; leaving the tree alone"
else
    SELF_INSTALL=0
fi
act "lay the release tree in $L_OPT"
if (( ! DRY && ! SELF_INSTALL )); then
    BEFORE="$(tree_digest "$OPT")"
    mkdir -p "$OPT" "$LIB/data" "$LIB/maps" "$LIB/archive" "$LIB/packs" "$ETC"
    (cd "$SRC" && tar --exclude=.git --exclude=data --exclude=maps --exclude=dist --exclude=tests --exclude=.claude --exclude='*.pyc' --exclude=__pycache__ -cf - .) | tar -xf - -C "$OPT"
    chmod +x "$OPT"/*.sh 2>/dev/null || true
    chown -R root:root "$OPT"
    chown -R "$SVCUSER:$SVCUSER" "$LIB"
    # The archive holds the movements of identifiable people. Its directory is not world-readable,
    # whatever umask the installer was run under.
    chmod 0750 "$LIB/archive"
    chown "root:$SVCUSER" "$ENVF"
    [[ "$(tree_digest "$OPT")" == "$BEFORE" ]] && log "tree unchanged" || CHANGED+=("laid $L_OPT")
elif (( SELF_INSTALL )); then
    mkdir -p "$LIB/data" "$LIB/maps" "$LIB/archive" "$LIB/packs" "$ETC"
    chmod 0750 "$LIB/archive"
    chown -R "$SVCUSER:$SVCUSER" "$LIB"
fi

# ---- the unit --------------------------------------------------------------------------------
RECUNITF="$ROOT/etc/systemd/system/pinecone-recorder.service"
L_RECUNIT=/etc/systemd/system/pinecone-recorder.service
REC_TEXT="[Unit]
Description=Pinecone: record what the TAK Server routes
After=network.target postgresql.service takserver.service
Wants=postgresql.service

[Service]
Type=simple
User=$SVCUSER
Group=$SVCUSER
WorkingDirectory=$L_OPT
EnvironmentFile=$L_ENV
ExecStart=/usr/bin/python3 $L_OPT/pinecone_recorder.py --archive $L_LIB/archive/pinecone.db
Restart=always
RestartSec=5
NoNewPrivileges=true
# The record is the movements of identifiable people: nothing this unit creates is group- or
# world-writable, and nothing is world-readable, whatever umask the system hands the service.
UMask=0027
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$L_LIB
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
"
act "write $L_RECUNIT (the recorder, user $SVCUSER)"
if (( ! DRY )); then
    mkdir -p "$(dirname "$RECUNITF")"
    if [[ -f "$RECUNITF" ]] && printf '%s' "$REC_TEXT" | cmp -s - "$RECUNITF"; then
        log "recorder unit unchanged"
    else
        printf '%s' "$REC_TEXT" > "$RECUNITF"
        CHANGED+=("wrote $L_RECUNIT")
    fi
fi

act "write $L_UNIT (bound to $BIND:$PORT, user $SVCUSER)"
UNIT_TEXT="[Unit]
Description=Pinecone: replay what the TAK Server saw
After=network.target postgresql.service takserver.service
Wants=postgresql.service

[Service]
Type=simple
User=$SVCUSER
Group=$SVCUSER
WorkingDirectory=$L_OPT
EnvironmentFile=$L_ENV
Environment=PINECONE_DISCOVERY=$L_DISC
ExecStart=/usr/bin/python3 $L_OPT/serve.py --bind $BIND --port $PORT --data $L_LIB/data --maps $L_LIB/maps --archive $L_LIB/archive/pinecone.db --packs $L_LIB/packs
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
# The record is the movements of identifiable people: nothing this unit creates is group- or
# world-writable, and nothing is world-readable, whatever umask the system hands the service.
UMask=0027
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$L_LIB
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
"
if (( ! DRY )); then
    mkdir -p "$(dirname "$UNITF")"
    if [[ -f "$UNITF" ]] && printf '%s' "$UNIT_TEXT" | cmp -s - "$UNITF"; then
        log "unit unchanged"
    else
        printf '%s' "$UNIT_TEXT" > "$UNITF"
        CHANGED+=("wrote $L_UNIT")
    fi
fi

# ---- the record the page reads ---------------------------------------------------------------
act "write $L_DISC (the report you confirmed, plus the credential facts; no secret in it)"
if (( ! DRY )); then
    printf '%s' "$DISC_JSON" | python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1]); import pinecone_discover as d
rep = json.load(sys.stdin); created = sys.argv[2] == "1"
rep["credential"] = {"role": "pinecone", "grant": "SELECT on cot_router", "created": created, "statement": d.credential_statement()}
json.dump(rep, sys.stdout, indent=1)' "$SRC" "$CRED_CREATED" > "$DISC"
    chmod 640 "$DISC"; chown "root:$SVCUSER" "$DISC"
fi

# ---- start -----------------------------------------------------------------------------------
act "systemctl daemon-reload; enable and restart pinecone.service and pinecone-recorder.service"
do_ systemctl daemon-reload
do_ systemctl enable pinecone.service >/dev/null 2>&1 || true
do_ systemctl restart pinecone.service
do_ systemctl enable pinecone-recorder.service >/dev/null 2>&1 || true
do_ systemctl restart pinecone-recorder.service

echo
printf '%s' "$DISC_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["credential"]["statement"])'
if (( CRED_CREATED )); then echo "Created role $ROLE with SELECT on cot_router; its password is in $L_ENV, readable by root and $SVCUSER only."; fi
if (( ${#CHANGED[@]} )); then echo "Changed: $(IFS=';'; echo "${CHANGED[*]}"), and the discovery record."; else echo "Changed: nothing but the discovery record."; fi
echo
echo "Recording positions and GeoChat into $L_LIB/archive/pinecone.db from now on: this is Pinecone's own copy, and it keeps what the server later deletes. PINECONE_CHAT=no in $L_ENV keeps the messages out. The record's shape is $RECORD (PINECONE_RECORD in $L_ENV)."
echo "Pinecone $V: http://127.0.0.1:$PORT/  (from another machine: ssh -L $PORT:127.0.0.1:$PORT <user>@<this box>)"
if [[ "$BIND" == "127.0.0.1" ]]; then
    echo "Bound to loopback only ($BIND:$PORT) with no authentication; to expose it on the network re-run this installer with --bind <address>, which is remembered across updates, and open the port yourself, nothing here touches the firewall. Editing $L_UNIT by hand works until the next update rewrites it."
else
    echo "Bound to $BIND:$PORT, reachable from the network, with no authentication: anyone who can reach the port sees everything. Nothing here touches the firewall."
fi
