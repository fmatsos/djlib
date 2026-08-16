# djlib

A local, deterministic, read-only-over-source CLI for cataloguing a DJ music
archive and detecting exact/audio-equivalent duplicate files.

`djlib` is designed to run inside a dedicated Proxmox LXC container that
mounts the source archive read-only as `/music` and a writable state
directory as `/data`. It never renames, moves, deletes or retags files under
`/music`.

See `docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md`
for the full Milestone 1 design and
`docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md` for
the implementation plan.

## Status

Milestone 1 (catalogue + exact/audio-equivalent duplicate detection) is
complete: incremental scanning, metadata extraction/resolution, provisional
tracks, duplicate blocking/hashing/Chromaprint/quality analysis/
classification/consolidation, human duplicate review (static HTML report +
decision import), track overrides/merge/split, a durable curation journal,
`djlib doctor` health checks, and a proven full-catalogue rebuild. See
`docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md`
for the design and
`docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md` for
the task-by-task implementation history.

## Installation

See `INSTALL.md` for full instructions. Short version:

- **Local install** (dev/testing): `python -m pip install -e '.[dev]'` plus
  `exiftool`, `ffmpeg` and `libchromaprint-tools` on `PATH`.
- **Proxmox LXC** (production, `/music` read-only + `/data` read/write): one
  command on the Proxmox host provisions the container, installs every
  requirement, installs djlib, writes the default config and migrates the
  database -- ready to use. No clone needed, like the Proxmox community
  scripts:

  ```bash
  CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
    bash -c "$(curl -fsSL https://raw.githubusercontent.com/fmatsos/djlib/main/infra/lxc/create-container.sh)"
  ```

  The individual steps (`infra/lxc/configure-mounts.sh`,
  `infra/lxc/bootstrap.sh`, `infra/lxc/install-djlib.sh`) are also usable on
  their own -- see `INSTALL.md`.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
djlib --help
```

`tests/fixtures/build_audio_fixtures.py` generates a small, deterministic,
synthetic-audio fixture library (`tests/fixtures/library/`, gitignored) used
by `tests/integration/test_end_to_end.py` -- run it once before that test:

```bash
python tests/fixtures/build_audio_fixtures.py
pytest
```

## Configuration

`djlib` reads paths from a `[paths]` section in a TOML file (see
`config.example.toml`), pointed to via the `DJLIB_CONFIG` environment
variable (e.g. `DJLIB_CONFIG=/etc/djlib/config.toml djlib scan`). Defaults,
if no config file is given, are:

```text
music_root = /music   # read-only source archive
data_root  = /data    # writable djlib state (catalog.sqlite, logs, etc.)
```

`[duplicates]` (`duration`, `chromaprint`) tunes candidate-blocking duration
tolerance and Chromaprint classification thresholds -- see
`config.example.toml` for every key and its default.

## Environment variables

### Runtime (`djlib` CLI)

| Variable       | Role                                                                                    | Default              |
| -------------- | ---------------------------------------------------------------------------------------- | -------------------- |
| `DJLIB_CONFIG` | Path to a TOML config file (see `config.example.toml`) supplying `music_root`, `data_root` and `[duplicates]` thresholds. | unset -- `music_root=/music`, `data_root=/data` |
| `DJLIB_REPO_ROOT` | Checkout containing `alembic.ini`/`alembic/`, used by `djlib doctor`'s migration check and `djlib rebuild` to run/inspect migrations. Only needed for a non-editable install (e.g. the LXC container, where `install-djlib.sh` sets it to `DJLIB_SRC_DIR`) -- an editable (`pip install -e`) dev install finds it on its own. | unset -- derived from this file's own location (works only for an editable install) |

### `infra/lxc/create-container.sh` (provisions the LXC container)

| Variable           | Role                                                                 | Default                                             |
| ------------------ | --------------------------------------------------------------------- | ---------------------------------------------------- |
| `CTID`              | LXC container ID to create or reuse.                                   | *(required)*                                         |
| `HOSTNAME`          | Container hostname.                                                    | `djlib`                                              |
| `STORAGE`           | Proxmox storage for the container rootfs.                              | `local-lvm`                                          |
| `TEMPLATE_STORAGE`  | Storage holding the OS template.                                       | `local`                                              |
| `TEMPLATE`          | OS template: a full volid (`local:vztmpl/debian-13-standard_...`) or just its filename/basename within `TEMPLATE_STORAGE`, with or without the archive extension. | highest-version `debian-<N>-standard` already downloaded (or, failing that, available) for this host's architecture |
| `ARCH`              | Container architecture to match when auto-selecting a template.        | host arch (`dpkg --print-architecture`)              |
| `ROOTFS_SIZE_GB`    | Rootfs size in GB.                                                     | `8`                                                   |
| `MEMORY_MB`         | RAM in MB.                                                             | `2048`                                                |
| `SWAP_MB`           | Swap in MB.                                                            | `512`                                                 |
| `CORES`             | CPU cores.                                                             | `2`                                                   |
| `BRIDGE`            | Network bridge.                                                        | `vmbr0`                                              |
| `NET_CONFIG`        | Full `pct --net0` string (overrides `BRIDGE`).                         | `name=eth0,bridge=$BRIDGE,ip=dhcp`                   |
| `MUSIC_SRC`         | Host path with the source DJ archive, mounted read-only as `/music`.   | `/mnt/tank/djing`                                    |
| `DATA_SRC`          | Host path for djlib state, mounted read/write as `/data`.              | `/mnt/tank/djlib`                                    |
| `DJLIB_REPO_URL`    | Git remote to install djlib from, inside the container.                | `https://github.com/fmatsos/djlib.git`               |
| `DJLIB_REPO_REF`    | Git ref (branch/tag) to install, and to fetch this script's own sibling helpers from when run remotely. | `main`                          |
| `DJLIB_RAW_BASE`    | Raw-content base URL used to fetch sibling helpers when run without a local clone. | `https://raw.githubusercontent.com/fmatsos/djlib` |

### `infra/lxc/install-djlib.sh` (installs/updates djlib inside the container)

| Variable            | Role                                                        | Default        |
| -------------------- | ------------------------------------------------------------- | ---------------- |
| `DJLIB_REPO_URL`     | Git remote to clone/fetch djlib from.                          | `https://github.com/fmatsos/djlib.git` |
| `DJLIB_REPO_REF`     | Git ref (branch/tag) to install.                               | `main`          |
| `DJLIB_SRC_DIR`      | Where djlib's source is checked out inside the container.      | `/opt/djlib`    |
| `DJLIB_VENV`         | Python venv (created by `bootstrap.sh`) to install djlib into.  | `/opt/djlib-venv` |
| `DJLIB_CONFIG_DIR`   | Directory for the default `config.toml`.                       | `/etc/djlib`    |
| `DJLIB_DATA_ROOT`    | `/data` layout root (cache/curation/reports/decisions/logs).    | `/data`         |

See `INSTALL.md` and each script's own header comment for full details and
usage examples.

## Operator workflow

A typical session against a real DJ archive, in order:

```bash
djlib doctor
```

Health check: `/music` present (and, on a real read-only mount, physically
read-only), `/data` writable, the SQLite schema is migrated, required
executables (`exiftool`, `ffprobe`, `fpcalc`) are on `PATH`, BLAKE3 is
importable, and a handful of internal catalogue invariants hold. Run this
first, and again any time something looks wrong. `--repair-journal` exports
any not-yet-exported curation events before reporting.

```bash
djlib scan
```

Incrementally walks `music_root`, extracting/resolving metadata for new or
changed files and creating one `PROVISIONAL` track per new file. Re-running
`scan` with nothing changed does no unnecessary work (0 new, 0 changed). A
corrupt or unreadable individual file is recorded and counted, never aborts
the scan.

```bash
djlib duplicates run
djlib duplicates stats
```

`run` conservatively blocks duplicate candidates by metadata, computes
targeted BLAKE3/Chromaprint/quality evidence only for those candidates,
classifies each pair, and automatically consolidates only the groups every
piece of evidence agrees on (`AUTO_CONFIRMED`: identical bytes or the same
audio re-encoded losslessly). Anything genuinely ambiguous, or where
metadata explicitly conflicts (e.g. "Original Mix" vs a remix/radio edit),
is left `REVIEW_REQUIRED` for a human -- never silently merged. `stats`
prints current group/pair counts by status and classification.

```bash
djlib duplicates calibrate
```

Before trusting automatic classification at library scale, export pairwise
evidence for every blocked candidate (binary hash always; Chromaprint/
similarity only when the hashes differ) and sample it by hand: exact binary
duplicates as positive controls, explicit remix/edit/bootleg conflicts as
negative controls, real same-version multi-encoding pairs as the case that
actually matters. This command only reports -- it never writes a duplicate
group or changes a threshold. If it surfaces false positives at the
configured threshold, raise `[duplicates.chromaprint]` in your config and
re-run `duplicates run`.

```bash
djlib duplicates report
```

Generates a static, serverless HTML review page under
`/data/reports/duplicates-review-<timestamp>/` (`index.html` +
`manifest.json`) listing every duplicate group with its evidence, reasons
and a proposed preferred file. Open it in a browser, review each
`REVIEW_REQUIRED` group, and export a `decisions.json` (CONFIRM /
CHANGE_PREFERRED / REJECT / DEFER per group) from the page itself.

```bash
djlib duplicates import-decisions /data/decisions/decisions.json
```

Atomically applies a reviewed `decisions.json`: validated against the
current catalogue state before a single row is written (stale or malformed
input rejects the whole file, with no partial apply and no `--force`).
CONFIRM/CHANGE_PREFERRED consolidate the group onto one track; REJECT/DEFER
record the human decision without merging anything. Every accepted decision
is durably exported to `/data/curation/events.jsonl` as part of the same
step.

```bash
djlib catalog inspect <public-id>
```

Given a `fil_...` or `trk_...` public ID, shows raw/resolved metadata,
effective identity (after any human override), any duplicate group the
underlying file(s) belong to with its pairwise evidence and reasons, the
preferred-file rationale, and the human decision provenance (curation
events) behind the current state.

```bash
djlib rebuild
```

Rebuilds `catalog.sqlite` from `music_root` plus `/data/curation/events.jsonl`
alone: backs up the current database (kept even on success), migrates a
fresh one, does a full rescan, replays the curation journal, then re-runs
`doctor`'s invariants. This is the concrete proof that catalogue loss is
recoverable without re-reviewing anything a human already decided.

### Out of scope: physical cleanup

`djlib` never deletes, moves, or retags a single file under `music_root` --
duplicate detection, preferred-file selection and every curation decision
above are purely database-level bookkeeping (design's "read-only-over-source"
invariant). Deciding what to actually do with a duplicate file on disk --
delete it, archive it, replace it with the preferred copy -- is a manual,
out-of-band step for the operator; `djlib` deliberately never automates it.
