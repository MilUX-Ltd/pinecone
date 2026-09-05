# Changelog

## 0.6.4 (5 September 2026)

The README, corrected: the badge that pointed at a workflow the public
repository no longer has, a status paragraph left over from the 0.1.0 spike,
and two sentences in the paragraph on the name. Nothing in the product
changed.

## 0.6.3 (5 September 2026)

The mark and the guide. Pinecone has its own mark, a cone of scales, every
scale a report the box kept and the one in the other colour the moment the
room kept; it is in the page header, in the browser tab, and on the front of
the README with the strapline, the debrief replay for TAK. A user guide,
`docs/guide/README.md`, starts the moment Pinecone is installed and takes a
reader through a debrief with pictures of the app over a synthetic exercise.
The README says where the name came from. The public repository no longer
carries a workflow, because a publish surface has no CI to fail.

## 0.6.2 (5 September 2026)

The hygiene suite is a maintainer's test over the private tree and it carries
the very words it keeps out of the public snapshot, so it stays private too.
Nothing in the product changed.

## 0.6.1 (5 September 2026)

The public surface, tidied. The architecture decision records stay in the
private repository with the rest of the working record, and comments and test
notes no longer name people, board cards or particular hardware; a check in
the hygiene suite now reads every snapshot before it leaves. The public
repository's history restarts at this release, and the earlier tags and their
downloads are withdrawn, because they carried those. Nothing in the product
changed.

## 0.6.0 (5 September 2026)

One release for slices 2 to 8, built one at a time on one branch and each read by a
fresh reviewer before the next.

Named moments. Press `m` while the replay is running and the moment the clock
is at is marked; name it and press Enter, or press Enter to keep the time as
the name. Moments are listed as an index of the debrief, kept on the box so
they are there for whoever opens the page next, drawn on the timeline, and a
click jumps back to one. Up to six can be promoted, and that is a budget with
the count visible rather than a setting, because people recall about six per
cent of thirty messages a week later and less is measurably more; the number
keys jump to the promoted ones in order. A moment can be handed to somebody as
a link, and the replay opens at it. The doctrine research put it plainly: this,
not the map, is the product.

Honest time. Every report carries the device's own time and the server's, so
each callsign now shows how late its reports typically were and how late at
worst, and a report more than ten seconds late says so on its marker, because
that is the latency at which the research found people making significantly
more false alarms. A device whose clock runs ahead of the server is reported
rather than folded in as a fast link. Dropouts are listed with the threshold
that found them and the time they add up to, and drawn on the timeline as
dropouts, so a silent gap is read as a gap in the comms and not as a person
standing still. The page says plainly what the record holds: what the server
received and when, never what any handset showed.

Chat on the timeline. The recorder now takes GeoChat too, with its whole
detail, from the server's own chat table (on TAK Server 5.8 chat never lands
in `cot_router`; it has a table of its own with its own ids, and so does the
archive now, each with its own cursor), and the bundle carries
messages beside the tracks: who said it, to which room, what, and when by both
clocks. Messages are marked along the top of the timeline, the latest one is
shown as the replay runs, and the list is an index that jumps. "I told you at
half past" is settled by looking. A box that does not want chat in the record
sets `PINECONE_CHAT=no` in the environment file, which survives an update. The
archive now holds what people said and not only where a radio was, and the
threat note says what that changes.

The page in task order. After a first morning's use, a product, a
research and a content read of the page agreed the side panel had grown in
build order, with the newest slice on top and the callsigns pushed down, and
that tabs by phase would hide the index and make every switch a public act on
a projector. So the panel now runs in the order a debrief does: the window,
the callsigns, the moments with what the box noticed under them as one list,
the messages, the ground, the record, and the marks and keys on request.
Everything that is set once or read by whoever set the box up, the version and
update, what is loaded, the gap and trail settings, mission pack import and
how late the reports were, sits behind the Map button and on the status page,
off the surface a facilitator drives in front of a room. The words changed
with it: moments are kept for the debrief rather than promoted, the box holds
reports rather than "the record", a boundary is reported outside then inside
rather than crossed, a reported contact says reported in its sentence, and
where a callsign was is answered by a press beside its name in two lines.
Pressing m in the field that says press m now marks the moment. A kept
message is cut at a word, never mid-word, on its way into the file that
leaves the room.

The map asks, never guesses. Nothing is chosen for you: with a network the
replay opens on OpenStreetMap, fetched by your browser, and without one the map
is blank and asks you to pick a map pack. A Map button on the map lists the
online map and every pack the box holds, says which tiles leave the laptop,
and keeps your choice. The box never fetches a tile. (Google Maps
was ruled out because its tiles need an account, a key and a bill.)

Proposals, never narration. The page now offers the moments the record can
point at: two callsigns within fifty metres for three minutes, a callsign's
dropout, ten minutes with no report and no message from anybody, a track
crossing a boundary the mission pack drew, a reported contact appearing, a
message that mentions a casualty or a contact. Each carries who, when and what
the record shows, and nothing that says why, because that is the room's to
supply and a tool that supplied it would be grading people. Accept keeps one
as a named moment; dismiss keeps it from coming back on this box. A where-was
box answers "where was ALPHA at the clock" with the last report and how old it
was then, and says stale when it was. There is no model in this: every
proposal is arithmetic over the record and the overlays, on the box, offline,
and anything cleverer that comes later is held to the same shape.

The record. The debrief leaves the room as a file, the moment it ends rather
than three months later, which is the twenty-five-year-old complaint about
take-home packages the research turned up. A record is started for the window
with a title and what was supposed to happen; the promoted moments are its
index; each item is observation, discussion, conclusion and recommendation,
sustain or improve, owned by a duty position and never by a name. A moment
becomes an observation with one press, in its own words, and the tool writes
nothing else. Twelve items at most, with the doctrine's number beside the
count. The export is a Markdown file in the unit's own shape, ODCR by default
or sustain and improve by a setting the installer carries through an update,
escaped so that what was typed reads as text wherever it is opened, and headed
with a handling line. There is no per-person section, and there never will be.

The ground it happened on. A replay with no plan behind it is narration, so
Pinecone now reads the mission pack an exercise was run from and draws it under
the movement: the boundary, the phase lines, the objectives. Import a TAK data
package from the replay page or with one request, and its overlays are listed
beside the callsigns, each one switchable, the choice remembered.

An overlay that carries a time window is drawn only while the replay clock is
inside it, because the picture changed during the exercise and a static overlay
lies. One with no window applies throughout and says so, rather than having a
window invented for it.

**Reported is never truth.** A contact that is hostile, suspect, unknown or
neutral, or one a person entered by hand, is what somebody believed and
reported, not where anything was. It is drawn differently, labelled as reported
wherever it appears, and carries the accuracy that was claimed for it. A
planner's control measure is a plan rather than a report and is not labelled.
Mislabelling either way would put a claim in a debrief the data cannot support.

This is the first thing Pinecone opens that somebody else made, so it is careful
with it: an entry that would be written outside its own directory is refused, a
doctype or an entity declaration is refused outright rather than resolved and
stripped, and there are two size limits, one for what is unpacked and a smaller
one for what is parsed.

`synth_pack.py` builds a synthetic pack over Andover for demonstrating this
without any real exercise material.

Three things 0.4.0 got wrong about what it tells you, found by a review run
after it shipped.

Asking for a window with nothing in it said so, and then overwrote its own
warning a moment later with a summary of what the record holds. An operator saw
an empty map and a message about reports, which is exactly the confusion between
an empty window and a broken query that the message exists to prevent.

Turning the history off did not survive an update, while the README, the
recorder's help and the changelog all said it did: the installer rewrites the
environment file on every run and was not carrying that setting forward. It is
written on a fresh install and kept on every later one.

The catch-up line called the server's row ids "reports", so a server whose table
is mostly not position reports read as though nineteen in twenty had been lost.
It says rows now, and says plainly that the two numbers are not comparable.

Two tests could not fail: one asserted an unbroken run of ids in a way that is
true of an empty list, and one asserted an end boundary the fixture met whatever
the code did. Both now pin the behaviour they name.

## 0.4.0 (4 September 2026)

Pinecone takes the history the server still holds. Installing it on a server
that has been up for months now reaches the exercises already in its table,
rather than starting empty and being useful only from tomorrow. It reads oldest
first, in bounded batches with a pause between them so it does not hold the
database down, resumes where it stopped if it is interrupted, and says on the
page how far it has to go. Reports from before the install are TAK's copy of
history, taken once; everything after is Pinecone's own. `PINECONE_BACKFILL=no`
in the environment file starts at the server's current position instead.

The replay takes a start and an end. A debrief is run over a period somebody
names, which is almost never the last hour, six hours or day, so those three are
still one press each and the window itself is now yours to set. A chosen window
is in the address, so a replay can be handed to somebody. A window with nothing
in it says so, and says what the record does hold, rather than showing an empty
map.

Pinecone keeps its own record. A second service records every position report
the TAK Server routes into an append-only archive on the box, from the moment
it is installed, reading the server's table forward from where it last got to
and writing each report once. Every timestamp and the whole detail blob are
kept as they arrived. It stops before it fills a disk the server is also using,
and says so. The status page reports what the archive holds and whether the
recorder is running, and the replay can ask the archive for the last hour, six
hours or day. The bundle builder is now one piece of code, used by both a
window from the archive and a window pulled to a CSV.

The record is also honest about itself, which took a pre-UAT review to get
right. Each pass reads from just below the highest report it holds rather than
from the mark itself, because the server hands out its row ids before the rows
commit and a report could otherwise be stepped over for good. A read that fails
now says so instead of looking like a quiet net, every pass leaves a heartbeat
so a stopped recorder cannot pass for a healthy one, and the page says when the
recorder last checked in as well as when it last wrote. The page reads the
archive read-only, each offered window reports its own count, a window is
capped so a day of a busy exercise cannot be used to take the page down, and a
damaged archive answers with a reason rather than a dropped connection.

A second review of those fixes found three more, two of them introduced by the
fixes themselves. The record now starts at whatever position the server's table
has reached when it is installed, rather than reading back through everything
the server still holds, which is what this changelog and the README already
promised. A window that had to be cut says what it holds, what it returned and
the cap that bit, instead of handing back a short answer whose own counts
certify it as complete. The page no longer reports a write time that moves on a
pass that wrote nothing.

A third review found three more, all of them where one fix met another. An
update now applies the services rather than restarting the ones the box already
had, because a release can add a service and this one does: a box taking 0.4.0
through the supported path would have received the files, restarted the page,
reported success, and run with no recorder at all. The record's starting point
is asked for on every pass rather than once at startup, so an archive that is
deleted underneath a running recorder starts again from the server's current
position instead of reading its whole table back. And a first run against a
database that is not answering now says so and tries again, rather than leaving
a traceback in the journal and nothing on the page, which is what happened on
any box that started Pinecone before the database was ready.

A fourth review, and a hunt through the seam the third round's own fix opened,
found two more in the update path. The address a box is bound to now survives an
update: it is kept beside the credential and honoured on every install, because
an update re-runs the installer and would otherwise have put a deliberately
exposed box back on loopback with nothing said. And `--reconcile` obeys the same
guard as an apply, so it cannot lay a working tree over an installed box; only a
tagged release ever reaches one.

A fifth review caught what the fourth's fix had missed: no box already in the
field has an address to remember, because every one of them was installed by a
version that wrote it into the unit file and nowhere else. So the first update
to this release, the very update the mechanism exists to make safe, would still
have taken a deliberately exposed box off the network. The installer now reads
the existing unit when the environment file has nothing to carry over, says that
it did so, and keeps it from then on. That read is anchored to a real
directive and takes the last one, because an unanchored read takes a
commented-out line, which is how an operator leaves an address they have just
moved away from, and carrying that one over could put a box back on every
interface. An update also says what it did: the address it carried over, any
value it could not use, and where the box ended up bound, none of which it said
before.

A seventh review turned back to the recorder, which four passes had left alone,
and found two faults that had been there all along. A pass that failed for any
reason other than the server refusing left the page saying it was recording,
with a heartbeat that kept moving; every failed pass now reaches the page and
says what kind of failure it was. And the recorder read the server's rows at the
default field limit, so a single report with an over-long detail blob raised an
error that stopped the record for good: the same batch retried for ever, the
cursor never moving, the page reporting health throughout. The limit now matches
the bundle builder's, and a row that cannot be read is reported rather than
swallowed.

## 0.3.0 (4 September 2026)

The map comes from what the estate already carries. Pinecone finds the
`.mbtiles` on this box (its own maps directory, `/opt/tak-maps`, or whatever a
local tile server serves from), asks a tile server already running here what it
serves, and reads the TAK `customMapSource` definitions this estate hands out,
loose or inside a data package. The discovery page lists them with where each
came from; one click chooses which the replay draws on, with no restart. Only
loopback and private addresses are ever asked, and nothing is fetched from the
internet.

Folded in from 0.2.1, which was never released: two defects the pre-UAT re-review found in 0.2.0, both in the installer.
`--dry-run` no longer runs the PostgreSQL preflight, so it prints the plan
without needing root, as the README says it does. Re-running an installed copy
(`sudo /opt/pinecone/install.sh`) no longer copies the tree onto itself; it
leaves the tree alone and refreshes everything else. The example-file finding
never derives its claim about a password from a filename.

## 0.2.0 (4 September 2026)

Slice 0: install, discover, show. `sudo ./install.sh` on a box with TAK Server
discovers the server (version from the package manager, unit state, ports,
database location and which file it came from, config-file permissions with
the world-readable example-file finding, retention TTLs, display timezone, row
count and time span), shows the report, asks before touching anything, creates
Pinecone its own read-only database role (`SELECT` on `cot_router`, generated
password in `/etc/pinecone/pinecone.env`, root and the service user only),
lays the tree in `/opt/pinecone`, writes a hardened `pinecone.service` bound
to loopback, and prints the URL and the exposure line. Re-runs keep the
credential. The page at `/` is the discovery
report with the live facts; the player moved to `/replay`. Also: update from
GitHub releases (`update.sh --check`, `update.sh`, and *Check for updates* on
the page; an installed copy is updated with `sudo /opt/pinecone/update.sh`,
which restarts the unit); timestamps parsed by hand so Python 3.10 accepts
what PostgreSQL prints; the server says when it is serving; the README
explains how it works, how it finds your TAK Server, the bundle format and
updating; the repository carries its decisions, `SECURITY.md`, a test suite
and a CI pipeline.

## 0.1.1 (4 September 2026)

`pull.sh --local` no longer aborts when `/opt/tak/CoreConfig.xml` is unreadable
by the current user; it falls back to the example file as intended. Proven on a
TAK 5.8 box as a non-root user. A failed pull no longer leaves an empty CSV
behind.

## 0.1.0 (4 September 2026)

First cut: the spike that proved the pipe from a live TAK Server to a moving
picture. Pull a window of `a-*` CoT out of `cot_router` (locally or over ssh),
build a bundle with callsign and device read out of the `detail` blob and every
timestamp kept, and play it back on a map served entirely offline from an
mbtiles file. Play, pause, slider and timeline scrub, 1x to 300x, reverse, per
callsign visibility, bounded or whole-run trails. Gaps render as gaps; nothing
is interpolated; stale markers go hollow with their age. Synthetic fixture
included. No installer, no recorder, no authentication yet.
