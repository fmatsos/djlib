---
paths:
  - "src/djlib/scan/**/*.py"
  - "src/djlib/metadata/**/*.py"
  - "src/djlib/resolve/**/*.py"
  - "infra/lxc/**"
description: The source archive (music_root) is physically and logically read-only
---

# Source archive is read-only (djlib)

## The invariant
`music_root` (mounted as `/music` in the production LXC, see
`docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md` §2) is the
original DJ archive. djlib is an analytical layer on top of it, never an editor of it.

## Rules
- Never open a file under `music_root` in a write mode (`'w'`, `'a'`, `'r+'`, etc.), never call
  `os.rename`/`shutil.move`/`os.remove`/`Path.unlink`/`Path.rename` on a path derived from
  `music_root`, and never invoke a tag-writing command (e.g. `exiftool` without `-j`/read-only
  flags) against a source file. Scanner, metadata extractor, and resolver only ever *read*.
- Discovery, extraction, and resolution work from cheap filesystem stats (path, size, `mtime_ns`)
  and read-only tool invocations (`exiftool -j`, `ffprobe`) — see Tasks 3-5 of the implementation
  plan. Never add a "fix/normalize the file" convenience feature.
- `infra/lxc/configure-mounts.sh` must keep the music mount `ro=1`; never change it to a
  read-write mount as a shortcut for some other feature.
- A source-mount health check (`djlib doctor`) must treat "write attempt to `/music` unexpectedly
  succeeds" as a FAIL, not a pass — see design §27. If you add a doctor probe, clean up any probe
  file you create and never touch a pre-existing media file to test writability.
- Per-file extraction/read errors (corrupt file, unreadable tags) must not abort a scan; they are
  recorded and counted, and the scan continues. Only infrastructure failures (mount missing,
  `/data` unavailable, required executable missing) should raise and abort the command.
