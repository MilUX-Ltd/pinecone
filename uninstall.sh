#!/usr/bin/env bash
# uninstall.sh - remove Pinecone from THIS box (Spec 014).
#
#   sudo ./uninstall.sh [--purge]
#
# In order: read the whole command line and refuse anything it does not understand before touching
# a thing; disable and delete both units the installer writes; drop Pinecone's own database role;
# remove /opt/pinecone and /etc/pinecone; delete the pinecone user. /var/lib/pinecone holds the
# archive of where people were, so it stays unless --purge says otherwise: deleting personal data
# is a separate decision and it is never the default.
#
# Nothing of TAK Server's is touched. Not its database, not its roles, not /opt/tak.
#
# Both units go, not just pinecone.service. The installer writes pinecone.service and
# pinecone-recorder.service; removing only the first leaves a unit with Restart=always pointing at
# an ExecStart this same run has just deleted, which sits in a restart loop for ever and is
# inherited unseen by the next install.
#
# PINECONE_ROOT=<dir> relocates every absolute path (the suite's box-in-a-directory) and skips the
# root check with it. It is not for production use.
set -euo pipefail

ROOT="${PINECONE_ROOT:-}"; PURGE=0

# The whole command line is read before anything is removed. The draft read only $1, so
# `./uninstall.sh --dry-run` removed the lot without warning, and `--oops --purge` would have
# purged on a line it should have rejected.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)    PURGE=1; shift ;;
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        *)          echo "ERR unknown option: $1" >&2; exit 2 ;;
    esac
done

# logical paths (what we print) and real paths (what we touch)
L_OPT=/opt/pinecone;                              OPT="$ROOT$L_OPT"
L_ETC=/etc/pinecone;                              ETC="$ROOT$L_ETC"
L_ENV="$L_ETC/pinecone.env";                      ENVF="$ROOT$L_ENV"
L_LIB=/var/lib/pinecone;                          LIB="$ROOT$L_LIB"
L_UNIT=/etc/systemd/system/pinecone.service;      UNITF="$ROOT$L_UNIT"
L_RECUNIT=/etc/systemd/system/pinecone-recorder.service; RECUNITF="$ROOT$L_RECUNIT"
SVCUSER=pinecone; ROLE=pinecone

log() { printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*"; }

[[ -n "$ROOT" ]] || [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "ERR run as root (sudo)" >&2; exit 2; }

# Was there anything here? Answered before the first removal, so the closing line reports the state
# this run found rather than the state it left. A second run says there was nothing to remove
# instead of narrating a removal it did not make.
#
# Residual, stated rather than hidden: a box whose tree was deleted by hand but whose database role
# survives reads as absent here, and the role is left. The user is caught (id below); the role is
# not, because probing it costs a query on every run to cover a case only a hand-edit produces.
PRESENT=0
for p in "$OPT" "$ETC" "$UNITF" "$RECUNITF"; do [[ -e "$p" ]] && PRESENT=1; done
id -u "$SVCUSER" >/dev/null 2>&1 && PRESENT=1
(( PURGE )) && [[ -e "$LIB" ]] && PRESENT=1

if (( ! PRESENT )); then
    echo "Pinecone: nothing to remove."
    exit 0
fi

# ---- units ------------------------------------------------------------------------------------
# Disabled with --now so a running service stops before its files go, and both are disabled even if
# only one unit file is on disk: enablement is a symlink elsewhere and can outlive the unit.
log "disable and remove $L_UNIT and $L_RECUNIT"
systemctl disable --now pinecone.service >/dev/null 2>&1 || true
systemctl disable --now pinecone-recorder.service >/dev/null 2>&1 || true
rm -f "$UNITF" "$RECUNITF"
systemctl daemon-reload >/dev/null 2>&1 || true

# ---- database role ------------------------------------------------------------------------------
# Pinecone's own role and nothing else: no database is dropped and no role of TAK Server's is
# named. The privileges are revoked before the role goes because PostgreSQL refuses to drop a role
# that still owns grants. One -c on one line, tolerant throughout: a box whose PostgreSQL has
# already gone is still a box this script must finish removing.
DB="$(sed -n 's/^PGDATABASE=//p' "$ENVF" 2>/dev/null | head -1)"
DB="${DB:-cot}"
log "drop the $ROLE database role in $DB"
sudo -u postgres psql -At -d "$DB" -c "REVOKE ALL ON cot_router FROM $ROLE; REVOKE ALL ON SCHEMA public FROM $ROLE; REVOKE CONNECT ON DATABASE \"$DB\" FROM $ROLE; DROP ROLE IF EXISTS $ROLE;" >/dev/null 2>&1 || true

# ---- trees ---------------------------------------------------------------------------------------
log "remove $L_OPT and $L_ETC"
rm -rf "$OPT" "$ETC"

if (( PURGE )); then
    log "remove $L_LIB, the archive included"
    rm -rf "$LIB"
fi

# ---- user ------------------------------------------------------------------------------------------
log "delete the $SVCUSER user"
userdel "$SVCUSER" >/dev/null 2>&1 || true

if (( PURGE )); then
    echo "Pinecone removed, $L_LIB and the archive in it with it."
else
    echo "Pinecone removed; the archive at $L_LIB was kept."
fi
