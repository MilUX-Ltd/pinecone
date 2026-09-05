# Pinecone

[![ci](https://github.com/MilUX-Ltd/pinecone/actions/workflows/ci.yml/badge.svg)](https://github.com/MilUX-Ltd/pinecone/actions/workflows/ci.yml)

**Replay what a TAK Server saw, so a debrief can settle what happened and get
on with why.** Pinecone reads the position reports (Cursor-on-Target) a TAK
Server already holds and plays them back on a map: each callsign moving, the
ground it has covered trailing behind it, and every gap in reporting shown as a
gap rather than smoothed away.

**Status: version 0.1.0, the spike built on 4 September 2026.** It proves the pipe from a live
TAK Server to a moving picture. It is not the product yet: nothing is installed
as a service, nothing records, there is no authentication. Read it as a working
demonstration you can run on your own TAK Server in about ten minutes.

Pinecone is source-available under the Pinecone Community Licence (see
`LICENSE`): free for personal, non-commercial, non-production use; commercial,
government, defence and production use need a licence from MilUX Ltd.

## How it works

Four steps, four files, and one seam between them.

1. **`pull.sh`** runs one SQL `COPY` against TAK Server's PostgreSQL database
   and writes a CSV: every `a-*` event with a point inside the window you
   asked for, with its uid, type, how, the three timestamps, the point, and
   the whole `detail` blob. Locally on the box, or over ssh from a laptop.
2. **`build_bundle.py`** turns that CSV into a bundle, one JSON file per
   window. It groups reports by uid, reads the callsign, device and team out
   of `detail`, keeps every report in server-receipt order, and computes each
   callsign's median reporting interval. It drops reports with no fix (lat and
   lon both zero) and counts them. It never interpolates, smooths or thins.
3. **`serve.py`** is a stdlib HTTP server bound to loopback. It serves the
   player, lists the bundles in `data/`, serves tiles straight out of an
   mbtiles file, reports its version, and can update itself from a release.
4. **`static/app.js`** is the player: a Leaflet map, one canvas for markers,
   polylines for trails, a timeline canvas, and the playback clock. Everything
   the player knows comes from the bundle, never from the database.

**The bundle is the seam.** The player is written against the bundle format,
not the database, so a bundle exported on one machine opens on another with no
TAK Server anywhere. The format is deliberately plain:

| Field | Meaning |
|---|---|
| `format` | `pinecone-bundle/0` |
| `window.start`, `window.end` | epoch milliseconds, UTC |
| `counts` | rows read, kept, dropped for no fix, and the number of tracks |
| `tracks[]` | one per uid: `uid`, `callsign`, `platform`, `device`, `os`, `version`, `meshtastic_id`, `team`, `role`, `type`, `n`, `first`, `last`, `median_interval_ms` |
| `tracks[].points[]` | arrays in `point_fields` order: server receipt time (ms), lat, lon, HAE, speed, course, battery, stale (ms), device time (ms), how |

Identity is the uid. The callsign is a label, and one handset can appear as
two uids with the same callsign (an ATAK node and a Meshtastic node, say).

## What you need

- A Linux box running TAK Server 5.x (tested against 5.8 on Ubuntu 22.04), with
  its PostgreSQL database reachable from where you run Pinecone. On a stock
  install that is `127.0.0.1:5432`, database `cot`, user `martiuser`.
- Python 3.10 or later (proven on 3.10 and 3.12), with `psql` on whichever
  machine runs the pull and `curl` if you want updates from GitHub. Nothing
  else: no pip, no node, no build step. Leaflet is vendored in `vendor/`.
- A basemap. Pinecone assumes **no internet**. It serves raster tiles straight
  out of an `.mbtiles` file (PNG or JPG tiles, XYZ or TMS, zoom levels of your
  choosing). If you do have internet you can point it at any XYZ tile URL
  instead.

## Install on the TAK Server box

```bash
tar -xzf pinecone-<version>.tgz && cd pinecone-<version>
sudo ./install.sh
```

The installer discovers the server, prints what it found and where, and asks before touching
anything. On yes it creates a `pinecone` system user, lays the tree in `/opt/pinecone`, creates
Pinecone its own read-only database role (`SELECT` on `cot_router`, nothing else; the password
lives in `/etc/pinecone/pinecone.env`, readable by root and the service user only), writes a
hardened `pinecone.service` bound to `127.0.0.1:8765`, and starts it. Then open
`http://127.0.0.1:8765/` (through `ssh -L 8765:127.0.0.1:8765 you@the-box` from elsewhere)
for the discovery page; the replay player is at `/replay`, reading bundles from
`/var/lib/pinecone/data` and basemaps from `/var/lib/pinecone/maps`.

`sudo ./install.sh --yes` skips the question; `--dry-run` prints the plan and changes nothing.
Running it again keeps the credential and refreshes the discovery. `sudo /opt/pinecone/update.sh`
takes the newest release and restarts the unit. Nothing touches the firewall or TAK Server's own
configuration.

## Quick start, without installing, on the TAK Server itself

```bash
git clone https://github.com/MilUX-Ltd/pinecone.git && cd pinecone
./pull.sh --local 2026-09-03T00:00Z 2026-09-04T00:00Z      # -> data/cot_...csv
python3 build_bundle.py data/cot_*.csv --out data/2026-09-03.json
python3 serve.py --tiles /path/to/your-area.mbtiles          # http://127.0.0.1:8765/
# (drop the .mbtiles into maps/ and --tiles is not needed)
```

`serve.py` listens on loopback only. From your laptop, tunnel to it:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-tak-box
```

and open `http://127.0.0.1:8765/`. If you would rather expose it on the box's
network, `--bind 0.0.0.0` does that and prints a warning, because there is no
authentication and anyone who can reach the port sees every track.

## Quick start, from a laptop with ssh to the TAK Server

```bash
./pull.sh 2026-09-03T00:00Z 2026-09-04T00:00Z you@your-tak-box
python3 build_bundle.py data/cot_*.csv --out data/2026-09-03.json
python3 serve.py --tiles /path/to/your-area.mbtiles
```

The pull runs `psql` on the box over ssh and streams a CSV back. Nothing is
installed on the box.

## Where the CoT comes from

TAK Server keeps every event it routes in the `cot_router` table of its
PostgreSQL database, until a retention policy deletes it (out of the box, no
policy is configured and nothing is deleted). Pinecone reads that table
directly: every `a-*` event (the position reports) with a point, inside the
`servertime` window you ask for.

### How it finds your TAK Server

Nothing is hard-coded to any particular server. On the box, `pull.sh` works
out where the database is in this order:

1. If `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE` or `PGPASSWORD` are set in the
   environment, they win. This is how you point it at PostgreSQL running
   somewhere else, in a container for instance.
2. Otherwise it reads the `<connection>` element of `/opt/tak/CoreConfig.xml`,
   which carries the JDBC URL (host, port, database name), the username and the
   password. On a stock install that file is readable only by root and the
   `tak` user, so run the pull as one of those.
3. If it cannot read that file, it reads `/opt/tak/CoreConfig.example.xml`
   instead. A stock install leaves that one world-readable, with the same
   generated password written into it by the post-install script. That is
   convenient for a first look and a hardening item for an operator; Pinecone
   uses it but does not pretend it is not there.

The password is never printed or written anywhere by Pinecone. The `COPY` runs
with `PGTZ=UTC`, because the database's own display timezone is whatever the
server was installed with and cannot be trusted.

**What is in the table.** `cot_router` has no callsign column. The callsign,
the device (`takv`: platform, model, OS, version), the team and role, the
battery and the track all live inside the `detail` column as XML, and
`build_bundle.py` reads them out of it. Every report carries three timestamps,
the device's `time` and `start` and the server's receipt time; all three are
kept and playback runs on receipt time.

**Tested on** TAK Server 5.8 on Ubuntu 22.04 (Python 3.10) and 24.04 (Python
3.12), both native package installs, including one stood up by a third-party
installer. Not yet tested against the Docker distribution of TAK Server, where
`CoreConfig.xml` lives inside the container and the database is reached by its
container name: set the `PG*` variables by hand there.

**What it does not do yet.** It discovers a credential and a database URL,
quietly. It does not report the TAK version, the ports that are listening, or
the retention policy in force, and it does not show you what it found and ask
you to confirm before using a credential it read from a file. That
discover-and-show step is the next slice of the product, and it is deliberately
separate from the player.

Meshtastic trackers, ATAK handsets, WinTAK, CloudTAK and bots all arrive the
same way, as CoT through the server, so there is no separate integration for
any of them. A Meshtastic tracker is recognised by its `takv` platform and drawn
as a triangle; ATAK is a circle; WinTAK a square.

Not yet built: reading through the Marti REST API instead of the database, and
subscribing live on port 8089 so the record continues past the server's own
retention. Both are on the roadmap below.

## What it records

When you install it, Pinecone takes the history the TAK Server still holds, oldest first and
paced so it does not hold the database down, and from then on keeps its own copy of every position
report and every GeoChat message the server routes, in `/var/lib/pinecone/archive/pinecone.db`
(`PINECONE_CHAT=no` in the environment file keeps the messages out). That is the point of the
product: a retention policy set next month cannot take last month's exercise away, because the
record is Pinecone's, not TAK's.

The page says while it is catching up, and how far it has to go. Reports from before the install
are TAK's copy of history, taken once; everything after is Pinecone's own, taken continuously. To
start at the server's current position instead, set `PINECONE_BACKFILL=no` in
`/etc/pinecone/pinecone.env`, which survives an update.

The recorder is its own service (`pinecone-recorder.service`), so the page keeps working if
recording stops and stopping the page does not stop the record. It reads the server's table
forward from where it last got to, writes each report once, and keeps every timestamp and the
whole detail blob exactly as it arrived. It checks free space before every batch and stops, saying
so on the status page, rather than filling a disk the TAK Server is also using.

On a box like MilUX's field kit that is about 7,000 reports a day, roughly 4 MB, so about 1.3 GB a
year.

The status page at `/status` says how many reports are held, the first and last, and whether the
recorder is running. The replay takes a start and an end, so you can put in the period the
exercise actually ran and watch that; the last hour, six hours and day are one press each, and any
bundle files you have built by hand are still in the list. A chosen window is in the address, so a
replay can be sent to somebody else on the box.

## Where the map comes from

Nothing is chosen for you. With a network, the replay opens on OpenStreetMap,
fetched by your browser, and the picker says so; without one, the map is blank
and asks you to pick a map pack. The **Map** button top right of the map lists
OpenStreetMap and every pack this box holds with where each came from; your
choice is kept. The box itself never fetches a tile, and a pack is the right
answer whenever the ground is not for sharing, because with the online map the
area you look at leaves the laptop.

Pinecone looks for what this box already carries, and never fetches a basemap from the internet.
The discovery page lists what it found, with where each came from, and one click chooses which the
replay draws on:

- **`.mbtiles` files** in Pinecone's own maps directory, in `/opt/tak-maps`, or wherever a tile
  server on this box serves from. Their name, zoom range, bounds and attribution come from the
  file's own metadata. Pinecone serves these itself, so they work with no network at all.
- **A tile server already running on this box.** If something answers `/services` on a local port,
  each map it serves is offered, with the zoom range and attribution from its own TileJSON. Only
  loopback and private addresses are ever asked.
- **The estate's own map-source definitions.** TAK `customMapSource` XML, loose or inside a data
  package, is read and offered; its `{$z}` placeholders are normalised for a browser. Tiles for
  one of these are fetched by your browser from the estate's tile server, which the page says
  plainly.

So on a TAK estate that already serves its own maps, Pinecone uses them and nobody builds a second
copy.

## Where the map comes from, in detail

`serve.py --tiles some.mbtiles` serves `/tiles/{z}/{x}/{y}.png` out of the file
with no network at all. Any raster mbtiles works: one built from OS OpenData,
from USGS imagery, from an OpenStreetMap extract, or exported from QGIS. Zoom
levels above the file's maximum are upscaled in the browser, so a file that
stops at zoom 16 still lets you zoom in on a 300 m site.

If you have internet and no file, `--tiles-url "https://tile.openstreetmap.org/{z}/{x}/{y}.png" --attribution "© OpenStreetMap contributors"`
lets the browser fetch tiles itself. Mind the tile provider's usage policy.

Roadmap: TAK Servers usually already carry the map packs their clients use
(data packages holding map source definitions, often pointing at a tile server
on the same network). Pinecone should find those on the server and use them,
so an offline estate gets the same map the handsets have without anyone
building a second one.

## What the picture means

- The **marker** is where the callsign was in its last report at or before the
  clock. Nothing is interpolated between reports.
- The **bold trail** is the ground covered so far in the current run of
  reports, either the whole run or the last 5, 15 or 60 minutes.
- The **faint line** underneath is the callsign's whole track in the window.
  Switch it off when a long window turns it into a hairball.
- A **hollow marker with an age** is stale: no report inside the threshold.
  The threshold is per callsign, four times its median reporting interval by
  default (about 2 minutes for ATAK, 7 to 12 minutes for a Meshtastic tracker),
  or a fixed 2 to 60 minutes if you prefer. The same threshold decides where a
  trail breaks.
- The **timeline** shows a row per callsign with a tick per report, so you can
  see the dropouts before you press play, and click or drag anywhere to seek.
- Wild GPS fixes are drawn as they arrived. A stationary tracker jitters, and
  occasionally reports a kilometre out. That is what the server saw.

Controls: Play, Pause, the slider, click or drag on the timeline, speeds 1x to
300x, Reverse, Fit. Keys: space, left and right arrows (10 s), `[` and `]` for
speed, `r` to reverse, `f` to fit. Click a callsign to hide or show it.

## Updating an installed copy

```bash
./update.sh --check      # what is installed, what the newest release is
./update.sh              # download, verify the sha256, apply, keep data/ and maps/
./update.sh --reconcile  # re-apply the services for the copy already on disk
```

The page shows the same thing: a version line with *Check for updates* and,
when a newer release exists, *Update now*, which works only from the box itself.
Only tagged releases are ever applied, never a branch, so work in progress on the
source repository cannot reach a box, and `--reconcile` is refused in a git
checkout for the same reason.

**An update applies the services, it does not merely restart them.** A release
can add one: 0.4.0 adds the recorder. Applying an update therefore re-runs the
installer, which writes every unit and restarts what it wrote, and reports a
failure rather than a version it is not running. This also means the address the
box is bound to has to survive an update, so it is kept in
`/etc/pinecone/pinecone.env` and honoured on every install: to change it, re-run
the installer with `--bind`, rather than editing the unit file, which the next
update will rewrite.

A box with no internet takes a release by hand: copy `pinecone-<version>.tgz`
and its `.sha256` from the release page, check the hash, extract over the
install directory, then run `./update.sh --reconcile` so any service the release
added is actually installed. `update.sh` picks up from there next time it can
reach GitHub.

## Chat on the timeline

GeoChat is recorded beside the positions and shown on the same timeline: who
said it, to which room, what, and when. On TAK Server 5.8 the server keeps
chat in a table of its own (`cot_router_chat`), so the recorder reads two
tables and the archive keeps two, each with its own cursor; the page counts
reports and messages apart.
The latest message is shown as the replay runs, the list jumps, and every
message is a small tick along the top of the timeline. `PINECONE_CHAT=no` in
`/etc/pinecone/pinecone.env` keeps chat out of the record; it survives an
update.

## Honest time

Every report carries the device's own time and the server's, so the replay
shows how late each callsign's reports were, marks a report more than ten
seconds late on its marker, reports a device whose clock runs ahead of the
server rather than hiding it, and lists every dropout with the time it adds up
to, drawn on the timeline as a dropout rather than as blank space. The record
holds what the server received and when; it does not hold what any handset
displayed, and the page says so.

## Named moments

Press `m` while the replay is at the moment you want, name it, press Enter. It
is kept on the box, listed as an index of the debrief, and drawn on the
timeline; a click jumps back to it. Up to six can be promoted, and the number
keys jump to those in order. The budget is deliberate: a debrief that leaves
with three things to sustain and three to improve is one people remember. A
moment can be handed to somebody as a link (`/replay?at=<time>`), and the
replay opens at it.

## The record

The debrief leaves the room as a file. A record is started for the window being
replayed with a title and what was supposed to happen; the moments the room
promoted are its index; and each item is the four ODCR fields, observation,
discussion, conclusion and recommendation, with a kind (sustain or improve) and
an owner that is a duty position, never a name. A moment goes into the record
with one press, as an observation in its own words, and the tool writes nothing
else: the fields are the room's. Up to twelve items, with the count and the
doctrine's number (three sustains and three improves) shown beside it. Export
is one press and one Markdown file in the unit's shape: ODCR by default, or
sustain and improve with `PINECONE_RECORD=sustain-improve` in
`/etc/pinecone/pinecone.env`, which survives an update (a hand edit takes
`systemctl restart pinecone.service` to apply; an update applies it itself).
An item's kind is sustain, improve, or not yet sorted, and an item made from a
moment is left unsorted, because sorting it is the room's verdict. The file carries a
handling line naming the box, the time and that it contains callsigns, and it
has no per-person section, because the record is one debrief, never one person.

## Proposals, never narration

The page offers the moments the record can point at: two callsigns within
fifty metres for three minutes, a callsign's dropout, ten minutes with no
report and no message from anybody, a track crossing a boundary the mission
pack drew, a reported contact appearing, a message that mentions a casualty or
a contact. Each one says who, when and what the record shows, in the record's
own words, and nothing about why, because that is the room's to supply and a
tool that supplied it would be grading people. Accept keeps one as a named
moment; dismiss keeps it from coming back on this box. The where-was box
answers "where was ALPHA at the clock" with the last report and how old it was
then, and says stale when it was older than that callsign's own threshold.
There is no model in any of this: every proposal is arithmetic over the record
and the overlays, on the box, offline. Anything cleverer that comes later is
held to the same shape. A dismissal is an identity built from the event as this
window cuts it, so it holds for windows that hold the whole event; a window that
cuts through the middle of a co-location sees a different event and proposes it
again. A computed list is kept for a minute per window.

## The ground it happened on

A replay with no plan behind it is narration. Pinecone reads the mission pack an
exercise was run from and draws it under the movement, so the tracks are seen
against the boundaries, phase lines and objectives they were supposed to happen
within.

```bash
python3 synth_pack.py --out /tmp/ex-cedar.zip   # a synthetic pack, for trying this out
```

Import it from the replay page, or with one request:

```bash
curl -X POST -d "path=/tmp/ex-cedar.zip" http://127.0.0.1:8765/api/packs/import
```

The overlays appear beside the callsigns and each can be switched off; the choice
is remembered. An overlay carrying a time window is drawn only while the replay
clock is inside it, because the picture changed during the exercise and a static
overlay lies. One with no window applies throughout, and says so.

**Reported contacts are drawn as reported.** A contact that is hostile, suspect,
unknown or neutral, or one a person entered by hand, is what somebody believed
and reported, not where anything was, so it is drawn differently, labelled
wherever it appears, and carries the accuracy claimed for it. A planner's control
measure, a boundary or an objective, is a plan rather than a report, and is not
labelled.

A pack is a zip full of XML that somebody else made, so Pinecone is careful with
it: an entry that would land outside its own directory is refused, a doctype or
an entity declaration is refused rather than stripped, and both what is unpacked
and what is parsed are capped. The reasoning is in
`docs/security/threat-note-005.md`.

## Data handling

Position history is personal data about identifiable people. Pinecone keeps it
on the machine that pulled it: `data/*.csv` and `data/*.json` are gitignored,
except the synthetic fixture. Nothing is sent anywhere. Delete `data/` when you
are done with a window.

`synth.py` writes a synthetic bundle (six callsigns, mixed rates, a deliberate
dropout, one Meshtastic-sourced track) so the player can be tried with no real
data at all: `python3 synth.py` then pick `synthetic` in the bundle list.

## Files

| File | What it does |
|---|---|
| `install.sh` | install beside a TAK Server as a service |
| `pinecone_discover.py` | what the TAK Server is, where its database is, what it keeps; never a credential |
| `pull.sh` | one `COPY` out of `cot_router`, locally or over ssh, into `data/` |
| `build_bundle.py` | CSV to a bundle: callsign and device out of `detail`, all timestamps kept, nothing interpolated |
| `serve.py` | stdlib HTTP server: the player, the bundles, tiles out of an mbtiles |
| `static/` | the player (`index.html`, `app.js`) |
| `update.sh` | check for, verify and apply the newest tagged release |
| `synth.py` | the synthetic fixture |
| `vendor/` | Leaflet 1.9.4, unmodified |

## Roadmap

1. **Slice 0.** An installer that runs on a box with TAK Server, discovers the
   server (version, ports, database, retention policy), provisions its own
   credential deliberately, and serves one page saying what it found.
2. **Slice 1.** A recorder that subscribes to the server continuously and keeps
   its own archive past TAK's retention, and this player built properly on top
   of it, against a bundle format so an exported window opens anywhere.
3. **Map packs from the server.** Find the map sources the estate already
   carries and use them.
4. **Overlays.** The mission's data packages, so movement is seen against the
   boundaries and objectives it was meant to happen within.

Pinecone will never be a scoring or assessment instrument. It settles what
happened; the room decides why.

## Decisions and contributing

The decisions the code depends on are in `docs/adr/`. This repository is the publish
surface: development happens in a private repository at MilUX and each release lands here as
one snapshot commit, tagged, with a tarball and its sha256. Issues are welcome here; pull
requests are not taken. Security reports: see `SECURITY.md`.

## Contact

MilUX Ltd, matt@milux.co.uk.
