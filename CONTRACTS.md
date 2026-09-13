# What Pinecone promises not to break

From 1.0.0, the things on this page are contracts. Everything else in the repository is
implementation, and implementation can change in any release.

Versions follow [semver](https://semver.org/). Against the seams below:

- **MAJOR** breaks something a consumer relied on: a key or column removed, renamed, or given a
  new meaning; a field's position in a point row changed; a setting that stops being read.
- **MINOR** adds without breaking: a new key, a new column, a new setting with a safe default.
- **PATCH** corrects behaviour without changing any of this.

If you build on Pinecone, pin a major version and read this page when it changes.

## 1. The bundle: `pinecone-bundle/0`

A window exported as one JSON file. It opens anywhere, with no server and no Pinecone, and it is
the seam the player itself is written against.

| Key | What it is |
|---|---|
| `format` | `pinecone-bundle/0`. The number changes only on a MAJOR. |
| `source` | Where it came from: `name`, `built_at`. |
| `window` | `start` and `end`, epoch milliseconds, UTC. |
| `counts` | `rows_read`, `rows_kept`, `rows_without_fix`, `tracks`. |
| `point_fields` | The names of the fields in a point row, **in order**. |
| `tracks` | One entry per uid. |

A track carries `uid`, `callsign`, `platform`, `device`, `os`, `version`, `team`, `role`, `type`,
`n`, `first`, `last`, `median_interval_ms` and `points`.

**A point is a list, read by position**, which is why `point_fields` is part of the contract and
not a convenience. The order at `pinecone-bundle/0` is:

```
servertime_ms, lat, lon, hae, speed, course, battery, stale_ms, device_time_ms, how
```

Read `point_fields` rather than hard-coding that order, and a MINOR that appends a field will not
break you.

**What is not promised:** the order of tracks, the exact wording of `source.name`, or that any
optional field is non-null. `battery` is frequently null and always has been.

## 2. The archive on a box

`/var/lib/pinecone/archive/pinecone.db`, SQLite. Every release must be able to open the archive a
previous release wrote, upgrade it in place, and keep every row. There is no migration step for an
operator to run and there will not be one.

The file carries its shape in `PRAGMA user_version`. **1** is the shape at 1.0.0. An archive
written before that reads as 0 and is upgraded on open.

| Table | What it holds |
|---|---|
| `report` | Position reports, one row per CoT event, keyed on the server's own id |
| `chat` | GeoChat, the same shape as `report` so a window can union the two |
| `connection` | Who was on the net: a Connected or Disconnected per client |
| `meta` | Key and value; cursors, floors, retention state |

`report` and `chat` carry `id`, `uid`, `cot_type`, `how`, `device_time`, `device_start`, `stale`,
`servertime`, `arrived`, `lat`, `lon`, `hae`, `ce`, `le`, `detail`, `groups`.

`connection` carries `id`, `servertime`, `arrived`, `event`, `callsign`, `uid`, `username`, `team`,
`role`, `client_version`, `groups`.

**Two meanings that are part of the contract, not just the column list.** `id` is the source
table's own id, so it is both the identity and the cursor. `groups` being null means membership is
**unknown**, not that the row belonged to no group; rows recorded before groups were kept carry
null and must never be read as "no groups".

**What is not promised:** the `meta` keys, which are internal; the indexes; that the file shrinks
when rows are deleted, which it does not.

## 3. The environment file

`/etc/pinecone/pinecone.env`, `0640 root:pinecone`. The installer **rewrites this file wholesale on
every run**, so every setting it does not carry forward is a setting an update would delete. These
are carried forward, and that is the promise:

| Setting | What it does |
|---|---|
| `PINECONE_BIND`, `PINECONE_PORT` | Where the page answers |
| `PINECONE_BACKFILL` | Whether a new install takes the history the server still holds |
| `PINECONE_CHAT` | Whether GeoChat is recorded |
| `PINECONE_RECORD` | The record's shape, `odcr` or `sustain-improve` |
| `PINECONE_KEEP_DAYS` | How long reports and messages are kept, in days; `0` keeps for ever |
| `PINECONE_KEEP_CONNECTION_DAYS` | The same for connection events |

`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD` are Pinecone's own read-only database
credential. The password is generated at install and is never printed, logged or shown on a page.

**What is not promised:** the file's comments or ordering, and that a setting you add yourself will
survive an update. It will not.

## 4. What is deliberately not a contract

The HTTP routes the page uses, the HTML it serves, the structured record's wording, the shape of
anything under `docs/`, and every Python module's internals. The player is a consumer of the
bundle like any other; it gets no privileged access to the archive.

## How this page changes

A change here is a change to the version. If you find something in this document that the code no
longer does, that is a defect in the code or in this page, and either way it is worth reporting:
`SECURITY.md` has the route for anything sensitive, and the repository's issues for the rest.
