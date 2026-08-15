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

This is the Milestone 1 bootstrap only (Task 1 of the implementation plan):
project scaffolding, packaging metadata, configuration loading, public-ID
generation, a minimal Typer CLI entry point, and LXC runtime helper scripts.
Scanning, metadata extraction, duplicate detection and every other Milestone
1 capability are implemented in later tasks and are not present yet.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
djlib --help
```

## Configuration

`djlib` reads paths from a `[paths]` section in a TOML file (see
`config.example.toml`). Defaults, if no config file is given, are:

```text
music_root = /music   # read-only source archive
data_root  = /data    # writable djlib state (catalog.sqlite, logs, etc.)
```
