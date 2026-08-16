# Installing djlib

`djlib` is a Python CLI. It has two supported install paths: a plain local
install for development/testing, and the production path, a dedicated
Proxmox LXC container with `/music` mounted read-only and `/data` mounted
read/write (see
`docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md`
sec 3.1).

## Requirements

- Python >= 3.12
- System executables on `PATH`: `exiftool`, `ffprobe` (from FFmpeg), `fpcalc`
  (from Chromaprint)
- `djlib doctor` checks all of the above (plus the database schema and
  catalogue invariants) and is the fastest way to confirm an install is
  healthy.

## Option A: local install (development, testing, or a non-LXC host)

```bash
python -m pip install -e '.[dev]'
djlib --help
```

On Debian/Ubuntu the system requirements above come from:

```bash
sudo apt-get install exiftool ffmpeg libchromaprint-tools
```

Point djlib at your archive and state directory with a config file (see
`config.example.toml`):

```bash
cp config.example.toml /etc/djlib/config.toml
# edit music_root / data_root in /etc/djlib/config.toml
DJLIB_CONFIG=/etc/djlib/config.toml djlib doctor
```

Without `DJLIB_CONFIG`, djlib defaults to `music_root=/music`,
`data_root=/data`.

## Option B: Proxmox LXC (production)

This is the target runtime: djlib never mutates the source archive, so it
runs in a container where `/music` is physically mounted read-only and
`/data` is the only writable state.

### Automated, remote: one command, no clone needed

Like the Proxmox community scripts, this can be run straight from a `curl`,
with nothing checked out on the host beforehand. On the Proxmox VE host, as
root:

```bash
CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/fmatsos/djlib/main/infra/lxc/create-container.sh)"
```

`create-container.sh` fetches its sibling helpers (`configure-mounts.sh`,
`bootstrap.sh`, `install-djlib.sh`) from GitHub itself when they aren't
present next to it, so this one command is the entire install.

### Automated, from a local clone

If the repo is already checked out on the host, run the same script from
its path instead -- it then uses the sibling scripts on disk and needs no
network access to GitHub for itself (djlib's own source is still fetched by
`install-djlib.sh` inside the container):

```bash
CTID=200 MUSIC_SRC=/mnt/tank/djing DATA_SRC=/mnt/tank/djlib \
  ./infra/lxc/create-container.sh
```

Either way, this creates the LXC container (or reuses `CTID` if it already
exists), mounts `/music` (read-only) and `/data`, upgrades system packages,
installs every system requirement, installs djlib into a dedicated venv,
writes the default config to `/etc/djlib/config.toml`, migrates the
database, and runs `djlib doctor`. When it finishes:

```bash
pct enter 200
djlib scan
```

`djlib` is on `PATH` inside the container (symlinked to the venv) and
`DJLIB_CONFIG` is set for every login shell. See the comment header of
`infra/lxc/create-container.sh` for every configuration variable (template,
resources, network, git ref to install, etc.). Re-running the script against
the same `CTID` pulls package updates and the latest djlib release into the
existing container -- it does not recreate it.

### Manual, step by step

If you'd rather drive each step yourself (or the container already exists
and was provisioned another way):

1. Create the container (`pct create ...`) from a Debian 12 template.
2. Mount the archive and state directories:
   ```bash
   ./infra/lxc/configure-mounts.sh <ctid>
   ```
   Edit the script first if your host paths differ from
   `/mnt/tank/djing` / `/mnt/tank/djlib`.
3. Inside the container, install the system requirements and create the
   venv:
   ```bash
   ./infra/lxc/bootstrap.sh
   ```
4. Inside the container, install djlib itself, write the default config and
   migrate the database:
   ```bash
   ./infra/lxc/install-djlib.sh
   ```
   Set `DJLIB_REPO_URL` / `DJLIB_REPO_REF` first to install from a fork or a
   specific branch/tag.
5. Verify:
   ```bash
   djlib doctor
   ```

## Updating an existing install

- Local install: `python -m pip install -e '.[dev]' --upgrade`
- LXC container: re-run `infra/lxc/create-container.sh` (recreates nothing,
  just updates packages and djlib) or, inside the container,
  `infra/lxc/install-djlib.sh` on its own to update djlib without touching
  system packages.
