#!/bin/bash
# Update this Pinecone install from the newest GitHub release of MilUX-Ltd/pinecone.
#
#   ./update.sh --check      # report current and latest, change nothing
#   ./update.sh              # download the release tarball and its sha256, verify, apply
#   ./update.sh --reconcile  # re-apply the installed services for the copy already on disk
#
# --reconcile runs the installer against the copy in this directory, so it is refused in a git
# checkout for the same reason an apply is: only a tagged release is ever put on a box, never a
# branch. An installed copy under /opt/pinecone is not a checkout, so the supported path is
# unaffected.
#
# Only tagged releases are ever applied, never a branch, so development churn on
# the source repo does not reach an installed box. data/ and maps/ are left alone.
# Needs curl, python3 and tar. No network means "cannot check", not an error.
set -euo pipefail
REPO="${PINECONE_REPO:-MilUX-Ltd/pinecone}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CUR="$(tr -d '[:space:]' < "$HERE/VERSION" 2>/dev/null || echo 0)"
MODE="${1:-apply}"

# Bring this box's services into line with the copy of Pinecone now sitting in $HERE.
#
# An update is not only new files. A release can add a service, as 0.4.0 adds the recorder, and a
# box that restarted only the services it already had would take the new files, report success, and
# run without the new one. Restarting is not enough on its own either: replacing a .py underneath a
# running interpreter changes nothing until that process restarts. The installer already writes
# every unit and restarts what it wrote, and it is idempotent, so this defers to it rather than
# keeping a second and quietly diverging copy of the same knowledge here.
reconcile() {
  if [ "$(id -u)" != 0 ]; then
    echo "result=run as root to apply the services, or restart them yourself"
    return 0
  fi
  if [ ! -x "$HERE/install.sh" ]; then
    echo "error=no install.sh beside this copy; the files are updated and the services are not"
    return 5
  fi
  # install.sh's output is kept, not discarded. Its ERR lines say which precondition failed, and
  # none of them carry the credential. Its other lines matter just as much on this path: where the
  # box ended up bound, whether an address was carried over from the old unit, and any value it
  # could not use. Sending those to /dev/null meant an update could change where the box answers,
  # or put it back on every interface, and say nothing at all.
  INSTALL_OUT="$("$HERE/install.sh" --yes 2>&1)" || {
    echo "error=install.sh could not apply the services; the files are updated and the services are not"
    [ -n "$INSTALL_OUT" ] && printf 'install.sh said: %s\n' "$(printf '%s' "$INSTALL_OUT" | tail -3)"
    return 5
  }
  # Everything an operator has to see even when it worked. The exposure line is here because an
  # update that leaves the box reachable from the network must say so on the way past.
  printf '%s' "$INSTALL_OUT" | grep -E '^(WARN|Bound to)|carried the address over' || true
  # An exit status is not evidence that the unit this whole step exists for was written. Check the
  # file, because that is the thing whose absence was the finding: a box with the new code, a happy
  # exit, and no recorder.
  if [ ! -f "${PINECONE_ROOT:-}/etc/systemd/system/pinecone-recorder.service" ]; then
    echo "error=install.sh returned success but wrote no pinecone-recorder.service; this box would run with no record"
    return 5
  fi
  echo "services=reconciled"
  for u in pinecone.service pinecone-recorder.service; do
    if systemctl is-active "$u" >/dev/null 2>&1; then echo "active=$u"; else echo "inactive=$u"; fi
  done
  return 0
}

if [ "$MODE" != "--check" ] && [ -e "$HERE/.git" ] && [ "${2:-}" != "--force" ]; then
  echo "error=this is a git checkout; use git pull, or ./update.sh apply --force"; exit 2
fi
if [ "$MODE" = "--reconcile" ]; then
  echo "current=$CUR"
  # Asking for the services to be applied and being told to run as root is a request that did not
  # happen, so it does not exit 0. On the apply path the same advisory is fine, because the files
  # were updated and only the services were left; here the services were the whole request.
  if [ "$(id -u)" != 0 ]; then
    echo "error=run as root to apply the services"; exit 4
  fi
  if reconcile; then exit 0; fi
  exit 5
fi
AUTH=(); [ -n "${PINECONE_GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $PINECONE_GITHUB_TOKEN")
JSON="$(curl -fsSL --max-time 20 -H 'Accept: application/vnd.github+json' ${AUTH[@]+"${AUTH[@]}"} \
        "https://api.github.com/repos/$REPO/releases?per_page=30" 2>/dev/null)" || { echo "current=$CUR"; echo "error=cannot reach GitHub (no network, or the repository is not visible)"; exit 3; }
read -r LATEST TGZ SHA < <(printf '%s' "$JSON" | python3 -c '
import json, sys, re
def key(v): return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
best = None
for r in json.load(sys.stdin):
    if r.get("draft"): continue
    v = r.get("tag_name", "").lstrip("v")
    tgz = next((a["browser_download_url"] for a in r.get("assets", []) if a["name"] == f"pinecone-{v}.tgz"), None)
    sha = next((a["browser_download_url"] for a in r.get("assets", []) if a["name"] == f"pinecone-{v}.tgz.sha256"), None)
    if tgz and sha and (best is None or key(v) > key(best[0])): best = (v, tgz, sha)
print(*(best or ("", "", "")))')
[ -n "$LATEST" ] || { echo "current=$CUR"; echo "error=no release with a tarball found"; exit 3; }
NEWER="$(python3 -c '
import re, sys
k = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3])
print("yes" if k(sys.argv[2]) > k(sys.argv[1]) else "no")' "$CUR" "$LATEST")"
echo "current=$CUR"; echo "latest=$LATEST"; echo "available=$NEWER"; echo "url=$TGZ"
[ "$MODE" = "--check" ] && exit 0
[ "$NEWER" = "yes" ] || { echo "result=already current"; exit 0; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL --max-time 120 ${AUTH[@]+"${AUTH[@]}"} -o "$TMP/pinecone-$LATEST.tgz" "$TGZ"
curl -fsSL --max-time 30 ${AUTH[@]+"${AUTH[@]}"} -o "$TMP/pinecone-$LATEST.tgz.sha256" "$SHA"
( cd "$TMP" && { command -v sha256sum >/dev/null && sha256sum -c --quiet "pinecone-$LATEST.tgz.sha256" || shasum -a 256 -c --quiet "pinecone-$LATEST.tgz.sha256"; } ) \
  || { echo "error=sha256 mismatch, nothing applied"; exit 4; }
echo "verified=sha256"
tar -xzf "$TMP/pinecone-$LATEST.tgz" -C "$TMP"
SRC="$TMP/pinecone-$LATEST"; [ -d "$SRC" ] || { echo "error=unexpected tarball layout"; exit 4; }
rm -rf "$SRC/maps"; find "$SRC/data" -type f ! -name synthetic.json -delete 2>/dev/null || true
tar -C "$SRC" -cf - . | tar -C "$HERE" -xf -
chmod +x "$HERE"/*.sh 2>/dev/null || true
echo "updated=$LATEST"
if ! reconcile; then exit 5; fi
[ "$(id -u)" = 0 ] && echo "result=running $LATEST"
exit 0
