# djlib Milestone 1 — Catalogue & Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, deterministic, read-only-over-source `djlib` Python CLI that incrementally catalogs `/music`, resolves metadata conservatively, detects exact/audio-equivalent duplicates, recommends a preferred file, supports static HTML review, and preserves human curation independently of SQLite.

**Architecture:** A dedicated Proxmox LXC mounts `/mnt/tank/djing` as `/music` read-only and `/mnt/tank/djlib` as `/data` read/write. `djlib` separates scan, extraction, resolution, catalogue, duplicate evidence, quality analysis, decisions, reporting and curation replay. SQLite is the operational projection; `/music` plus `/data/curation/events.jsonl` are sufficient to reconstruct curated state.

**Tech Stack:** Python 3.12+, Typer, SQLAlchemy 2.x, Alembic, SQLite, ExifTool, FFmpeg/ffprobe, Chromaprint/fpcalc, BLAKE3, RapidFuzz, Jinja2, jsonschema, pytest.

**Source design:** `docs/superpowers/specs/2026-08-15-djlib-milestone-1-catalog-dedup-design.md`

## Global Constraints

- `/music` is physically read-only.
- `/data` is the only mutable application area.
- `djlib` never renames, moves, deletes or retags source files.
- Automatic duplicate classification and preferred-file selection are allowed; source deletion never is.
- Human decisions override automatic decisions.
- One `track` is one precise audio version; remix/edit/bootleg/VIP/radio/live variants stay distinct.
- Multiple encodings of the same precise audio version may belong to one track.
- Public IDs are immutable and never recycled.
- Scan is incremental by default; `--full` re-extracts; `rebuild` recreates SQLite then replays curation.
- BLAKE3, Chromaprint and deep quality analysis are targeted to duplicate candidates only.
- Metadata resolution is: valid embedded tag → conservative filename fallback → unknown.
- Human semantic overrides are track-level and survive rescans.
- Milestone 1 is fully offline/local.
- Static HTML review exposes exactly `CONFIRM`, `CHANGE_PREFERRED`, `REJECT`, `DEFER`.
- Audio previews remain explicitly deferred to a later v2/v3.
- No stale-import `--force` exists in Milestone 1.
- Per-file failures continue the scan; infrastructure/config failures abort.
- All business writes are transactional.
- SQLite enables foreign keys, WAL and busy timeout.
- TDD: introduce each behavior with a failing test.
- Commit after every independently testable task.

---

## Planned repository structure

```text
djlib/
├── pyproject.toml
├── README.md
├── config.example.toml
├── alembic.ini
├── infra/lxc/
│   ├── configure-mounts.sh
│   └── bootstrap.sh
├── docs/superpowers/specs/...
├── docs/superpowers/plans/2026-08-15-djlib-milestone-1-implementation.md
├── src/djlib/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── ids.py
│   ├── logging.py
│   ├── runs.py
│   ├── doctor.py
│   ├── db/{base.py,engine.py,enums.py,models.py,session.py}
│   ├── scan/{scanner.py,service.py}
│   ├── metadata/{types.py,exiftool.py,ffprobe.py,extractor.py}
│   ├── resolve/{types.py,normalizer.py,filename.py,parser.py,resolver.py}
│   ├── catalog/{service.py,queries.py}
│   ├── duplicates/{types.py,blocking.py,similarity.py,hashing.py,chromaprint.py,quality.py,classifier.py,groups.py,preferred.py,calibration.py,service.py}
│   ├── curation/{events.py,journal.py,replay.py,decisions.py}
│   └── report/{generator.py,decision-schema.json,templates/index.html.j2,assets/app.js,assets/style.css}
├── alembic/versions/
└── tests/{unit,integration,fixtures/library}
```

---

### Task 1: Bootstrap repository, CLI, configuration and LXC runtime helpers

**Files:**
- Create: `pyproject.toml`, `README.md`, `config.example.toml`
- Create: `src/djlib/__init__.py`, `src/djlib/cli.py`, `src/djlib/config.py`, `src/djlib/ids.py`
- Create: `infra/lxc/configure-mounts.sh`, `infra/lxc/bootstrap.sh`
- Test: `tests/unit/test_config.py`, `tests/unit/test_ids.py`

**Interfaces:**
- `DjlibConfig.defaults() -> DjlibConfig`
- `DjlibConfig.load(path: Path | None) -> DjlibConfig`
- `new_public_id(prefix: str) -> str`
- CLI entry point `djlib`

- [ ] **Step 1: Write failing configuration tests**

```python
from pathlib import Path
from djlib.config import DjlibConfig

def test_defaults() -> None:
    cfg = DjlibConfig.defaults()
    assert cfg.music_root == Path('/music')
    assert cfg.data_root == Path('/data')
    assert cfg.database_url == 'sqlite:////data/catalog.sqlite'

def test_toml_overrides_paths(tmp_path: Path) -> None:
    p = tmp_path / 'djlib.toml'
    p.write_text('[paths]\nmusic_root="/srv/music"\ndata_root="/srv/data"\n')
    cfg = DjlibConfig.load(p)
    assert cfg.music_root == Path('/srv/music')
    assert cfg.data_root == Path('/srv/data')
```

- [ ] **Step 2: Write failing public-ID test**

```python
from djlib.ids import new_public_id

def test_public_ids_are_prefixed_and_unique() -> None:
    a = new_public_id('trk')
    b = new_public_id('trk')
    assert a.startswith('trk_')
    assert a != b
```

- [ ] **Step 3: Verify tests fail**

```bash
pytest tests/unit/test_config.py tests/unit/test_ids.py -v
```

Expected: import failures.

- [ ] **Step 4: Add package metadata and dependencies**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "djlib"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "typer>=0.12",
  "sqlalchemy>=2.0",
  "alembic>=1.13",
  "blake3>=1.0",
  "rapidfuzz>=3.9",
  "jinja2>=3.1",
  "jsonschema>=4.23",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-cov>=5.0"]

[project.scripts]
djlib = "djlib.cli:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 5: Implement config and IDs**

```python
# src/djlib/config.py
from dataclasses import dataclass
from pathlib import Path
import tomllib

@dataclass(frozen=True)
class DjlibConfig:
    music_root: Path
    data_root: Path

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_root / 'catalog.sqlite'}"

    @classmethod
    def defaults(cls) -> 'DjlibConfig':
        return cls(Path('/music'), Path('/data'))

    @classmethod
    def load(cls, path: Path | None = None) -> 'DjlibConfig':
        base = cls.defaults()
        if path is None:
            return base
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
        paths = raw.get('paths', {})
        return cls(Path(paths.get('music_root', base.music_root)), Path(paths.get('data_root', base.data_root)))
```

```python
# src/djlib/ids.py
import uuid

def new_public_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'
```

- [ ] **Step 6: Add initial Typer app**

```python
import typer
app = typer.Typer(no_args_is_help=True, help='Local DJ-library catalogue and deduplication tool.')
```

- [ ] **Step 7: Add host mount helper**

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
CTID="${1:?Usage: configure-mounts.sh <ctid>}"
pct set "$CTID" -mp0 /mnt/tank/djing,mp=/music,ro=1
pct set "$CTID" -mp1 /mnt/tank/djlib,mp=/data
```

- [ ] **Step 8: Add LXC bootstrap helper**

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
apt-get update
apt-get install -y python3 python3-venv python3-pip exiftool ffmpeg libchromaprint-tools
python3 -m venv /opt/djlib-venv
/opt/djlib-venv/bin/python -m pip install --upgrade pip
```

- [ ] **Step 9: Run tests and CLI smoke test**

```bash
python -m pip install -e '.[dev]'
pytest tests/unit/test_config.py tests/unit/test_ids.py -v
djlib --help
```

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml README.md config.example.toml src/djlib infra/lxc tests/unit/test_config.py tests/unit/test_ids.py
git commit -m "chore: bootstrap djlib project and lxc runtime"
```

---

### Task 2: Build SQLAlchemy/Alembic persistence foundation

**Files:**
- Create: `src/djlib/db/base.py`, `engine.py`, `session.py`, `enums.py`, `models.py`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial_catalog.py`
- Test: `tests/integration/test_database.py`

**Interfaces:**
- `create_engine_for_config(config: DjlibConfig) -> Engine`
- `session_factory(engine: Engine)`
- ORM models for files, tracks, scans, duplicates, quality, decisions, curation and app state.

- [x] **Step 1: Write failing SQLite-pragmas test**

```python
from sqlalchemy import text

def test_sqlite_pragmas(engine) -> None:
    with engine.connect() as c:
        assert c.execute(text('PRAGMA foreign_keys')).scalar_one() == 1
        assert c.execute(text('PRAGMA journal_mode')).scalar_one().lower() == 'wal'
        assert c.execute(text('PRAGMA busy_timeout')).scalar_one() > 0
```

- [x] **Step 2: Verify failure**

```bash
pytest tests/integration/test_database.py -v
```

- [x] **Step 3: Define enums**

Implement string enums for:
`ScanStatus`, `TrackStatus`, `AnalysisStatus`, `RelationshipType`, `DecisionSource`, `DuplicateStatus`, `PairClassification`, `TranscodeSuspicion`.

- [x] **Step 4: Implement SQLite engine setup**

```python
from sqlalchemy import create_engine, event

def create_engine_for_config(config):
    config.data_root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(config.database_url, future=True)
    @event.listens_for(engine, 'connect')
    def configure(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute('PRAGMA foreign_keys = ON')
        cur.execute('PRAGMA journal_mode = WAL')
        cur.execute('PRAGMA busy_timeout = 5000')
        cur.close()
    return engine
```

- [x] **Step 5: Implement full Milestone-1 model set**

Create ORM models with the exact responsibilities from the approved design:

```text
FileRecord
FileFeaturedArtist
Track
TrackFeaturedArtist
TrackFile
TrackOverride
TrackIdentityEvent
ScanRun
DuplicateGroup
DuplicateGroupMember
DuplicatePairEvidence
FileQualityAnalysis
DuplicateDecision
CurationEvent
AppState
```

Use JSON columns for raw metadata/evidence/payload and immutable `public_id` columns for externally referenced entities.

- [x] **Step 6: Enforce one active logical track per file**

Add a partial unique index in migration:

```sql
CREATE UNIQUE INDEX uq_track_files_one_active_per_file
ON track_files(file_id)
WHERE is_active = 1;
```

- [x] **Step 7: Create/apply first migration**

```bash
alembic upgrade head
pytest tests/integration/test_database.py -v
```

- [x] **Step 8: Commit**

```bash
git add src/djlib/db alembic.ini alembic tests/integration/test_database.py
git commit -m "feat: add catalog database schema and migrations"
```

---

### Task 3: Implement incremental filesystem scanning

**Files:**
- Create: `src/djlib/scan/scanner.py`, `src/djlib/scan/service.py`
- Modify: `src/djlib/cli.py`
- Test: `tests/unit/test_scanner.py`, `tests/integration/test_scan.py`

**Interfaces:**
- `DiscoveredFile(relative_path, size_bytes, mtime_ns)`
- `discover_audio_files(root: Path) -> Iterator[DiscoveredFile]`
- `ScanService.scan(full: bool = False) -> ScanSummary`

- [x] **Step 1: Write discovery test**

```python
def test_discovers_only_supported_audio(tmp_path):
    (tmp_path/'a.mp3').write_bytes(b'x')
    (tmp_path/'b.flac').write_bytes(b'x')
    (tmp_path/'cover.jpg').write_bytes(b'x')
    assert [x.relative_path for x in discover_audio_files(tmp_path)] == ['a.mp3', 'b.flac']
```

- [x] **Step 2: Write NEW/UNCHANGED/CHANGED/MISSING integration test**

Run four scans while creating, preserving, modifying and deleting a fixture; assert counters and `is_present` behavior.

- [x] **Step 3: Verify failures**

```bash
pytest tests/unit/test_scanner.py tests/integration/test_scan.py -v
```

- [x] **Step 4: Implement deterministic cheap discovery**

Supported extensions:

```python
{'.mp3','.flac','.wav','.aiff','.aif','.m4a','.aac','.ogg','.opus'}
```

Discovery reads only path/stat information and sorts relative paths.

- [x] **Step 5: Implement scan lifecycle**

Rules:

```text
new path -> insert, later extract/resolve
same path+size+mtime_ns -> unchanged
known path with changed signature -> changed + derived statuses STALE
unseen after successful discovery -> is_present=false
```

Never mark missing after an aborted discovery pass.

- [x] **Step 6: Add `djlib scan [--full]`**

Print seen/new/changed/unchanged/missing/failed counts and scan public ID.

- [x] **Step 7: Run tests**

```bash
pytest tests/unit/test_scanner.py tests/integration/test_scan.py -v
```

- [x] **Step 8: Commit**

```bash
git add src/djlib/scan src/djlib/cli.py tests/unit/test_scanner.py tests/integration/test_scan.py
git commit -m "feat: add incremental filesystem scanning"
```

---

### Task 4: Add ExifTool + ffprobe metadata extraction with per-file fault isolation

**Files:**
- Create: `src/djlib/metadata/types.py`, `exiftool.py`, `ffprobe.py`, `extractor.py`
- Modify: `src/djlib/scan/service.py`
- Test: `tests/unit/test_metadata_extractor.py`, `tests/integration/test_scan_metadata_errors.py`

**Interfaces:**
- `MetadataExtractor.extract(path: Path) -> ExtractedMetadata`
- `RawMetadata`, `TechnicalMetadata`, `ExtractedMetadata`

- [x] **Step 1: Write ExifTool adapter test**

Mock JSON:

```json
[{"SourceFile":"x","Title":"Acid Phase","Artist":"Emmanuel Top","BPM":"145"}]
```

Assert title/artist/BPM and complete raw JSON are preserved.

- [x] **Step 2: Write ffprobe adapter test**

Mock a stream with codec `flac`, sample rate `44100`, channels `2`, bit depth `16`, duration `401.25`; assert normalized technical values.

- [x] **Step 3: Verify failures**

```bash
pytest tests/unit/test_metadata_extractor.py -v
```

- [x] **Step 4: Implement subprocess runner abstraction**

```python
class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> CompletedProcess[str]: ...
```

Production uses `subprocess.run(check=False, capture_output=True, text=True)`.

- [x] **Step 5: Implement ExifTool call**

```bash
exiftool -j -n -Title -Artist -Album -AlbumArtist -Genre -BPM -InitialKey -Comment <file>
```

Valid JSON output remains usable even when stderr contains warnings; malformed/empty output is a per-file extraction error.

- [x] **Step 6: Implement ffprobe call**

```bash
ffprobe -v error -print_format json -show_format -show_streams <file>
```

- [x] **Step 7: Integrate with scan**

NEW/CHANGED and all files under `--full` are extracted. Per-file error sets metadata state ERROR, increments failed count and continues. Infrastructure failure remains fatal.

- [x] **Step 8: Test corrupt-file behavior**

```python
summary = service.scan(full=True)
assert summary.status == ScanStatus.SUCCESS_WITH_ERRORS
assert summary.files_failed == 1
```

- [x] **Step 9: Run tests and commit**

```bash
pytest tests/unit/test_metadata_extractor.py tests/integration/test_scan_metadata_errors.py -v
git add src/djlib/metadata src/djlib/scan/service.py tests
git commit -m "feat: extract audio tags and technical metadata"
```

---

### Task 5: Implement conservative metadata normalization, parsing and filename fallback

**Files:**
- Create: `src/djlib/resolve/types.py`, `normalizer.py`, `filename.py`, `parser.py`, `resolver.py`
- Modify: `src/djlib/scan/service.py`
- Test: `tests/unit/test_normalizer.py`, `test_filename_parser.py`, `test_version_parser.py`, `test_featured_artists.py`

**Interfaces:**
- `normalize_identity(value: str) -> str`
- `parse_filename(name: str) -> FilenameMetadata`
- `parse_title_annotations(title: str) -> ParsedTitle`
- `split_featured_artists(artist: str) -> FeaturedArtistParse`
- `MetadataResolver.resolve(file_name, raw) -> ResolvedMetadata`

- [x] **Step 1: Write normalization tests**

Cover Unicode canonicalization, casefold, typographic dash/quote normalization, whitespace collapse, while retaining meaningful `&`, `+`, `vs.`, `pres.`.

- [x] **Step 2: Write filename tests**

Must parse:

```text
Artist - Title.ext
Artist – Title.ext
01 - Artist - Title.ext
01. Artist - Title.ext
Artist - Title (Version).ext
```

Must not invent artist/title from ambiguous `Acid Track Final New 2.flac`.

- [x] **Step 3: Write version/edition tests**

Cover Remix, Mix, Original Mix, Extended Mix, Radio Edit, Club Mix, Re-edit, Rework, Bootleg, Mashup, VIP, Dub, Instrumental, Live, and separate Remaster/Anniversary/Deluxe/Reissue editions.

- [x] **Step 4: Write featuring tests**

Cover `feat.`, `ft.`, `featuring`; preserve ordered featured artists.

- [x] **Step 5: Verify failures**

```bash
pytest tests/unit/test_normalizer.py tests/unit/test_filename_parser.py tests/unit/test_version_parser.py tests/unit/test_featured_artists.py -v
```

- [x] **Step 6: Implement resolver priority**

```python
resolved = valid_tag or conservative_filename_value or None
source = 'TAG' if valid_tag else 'FILENAME' if conservative_filename_value else 'UNKNOWN'
```

Raw values remain untouched.

- [x] **Step 7: Integrate resolver after metadata extraction**

Persist resolved artist/title/version/edition and file-level featured artists.

- [x] **Step 8: Run tests and commit**

```bash
pytest tests/unit/test_normalizer.py tests/unit/test_filename_parser.py tests/unit/test_version_parser.py tests/unit/test_featured_artists.py -v
git add src/djlib/resolve src/djlib/scan/service.py tests/unit
git commit -m "feat: resolve and normalize dj metadata conservatively"
```

---

### Task 6: Create provisional track identities and catalogue inspection

**Files:**
- Create: `src/djlib/catalog/service.py`, `src/djlib/catalog/queries.py`
- Modify: `src/djlib/scan/service.py`, `src/djlib/cli.py`
- Test: `tests/integration/test_catalog.py`

**Interfaces:**
- `CatalogService.create_provisional_track(file: FileRecord) -> Track`
- `CatalogService.effective_identity(track: Track) -> EffectiveIdentity`
- CLI `catalog stats`, `catalog inspect <public-id>`

- [x] **Step 1: Write one-file-one-provisional-track test**

After first scan of two source files, assert two `FileRecord`, two `PROVISIONAL` tracks and one active PRIMARY relation per file.

- [x] **Step 2: Verify failure**

```bash
pytest tests/integration/test_catalog.py -v
```

- [x] **Step 3: Implement provisional creation**

A newly scanned file always creates its own provisional track. Scan never merges two files by similar tags alone.

- [x] **Step 4: Copy effective resolved identity and featured artists to track**

Keep file raw/resolved metadata separate from track semantic identity.

- [x] **Step 5: Add catalogue stats/inspect**

`stats` includes file presence, track statuses and metadata errors. `inspect` accepts `fil_...` or `trk_...` and shows raw/resolved/effective identity, provenance and analysis statuses.

- [x] **Step 6: Run tests and commit**

```bash
pytest tests/integration/test_catalog.py -v
git add src/djlib/catalog src/djlib/scan/service.py src/djlib/cli.py tests/integration/test_catalog.py
git commit -m "feat: create provisional tracks and catalog inspection"
```

---

### Task 7: Implement duplicate candidate blocking and pairwise metadata evidence

**Files:**
- Create: `src/djlib/duplicates/types.py`, `blocking.py`, `similarity.py`
- Test: `tests/unit/test_similarity.py`, `tests/unit/test_blocking.py`

**Interfaces:**
- `duration_tolerance_ms(duration_ms: int) -> int`
- `version_compatibility(left, right) -> VersionCompatibility`
- `metadata_similarity(left, right) -> MetadataEvidence`
- `CandidateBlocker.find_candidates(file_id: int) -> list[CandidatePair]`

- [x] **Step 1: Write duration-window tests**

```text
<=5 min => 2000 ms
5-10 min => 3000 ms
>10 min => 5000 ms
```

- [x] **Step 2: Write anti-false-positive version tests**

Explicitly incompatible:

```text
Original Mix vs Radio Edit
Original Mix vs Extended Mix
X Remix vs Y Remix
Live vs Studio
Instrumental vs Vocal
Bootleg vs Original
```

`Original Mix` vs empty is compatible-with-penalty, not identical.

- [x] **Step 3: Write featuring-tolerance tests**

Missing feat on one side does not exclude; conflicting explicit feats reduce confidence.

- [x] **Step 4: Verify failures**

```bash
pytest tests/unit/test_similarity.py tests/unit/test_blocking.py -v
```

- [x] **Step 5: Implement explicit evidence object**

```python
@dataclass(frozen=True)
class MetadataEvidence:
    artist_similarity: float
    title_similarity: float
    featured_artist_similarity: float | None
    version_compatibility: str
    edition_compatibility: str
    duration_delta_ms: int
    metadata_similarity: float
```

Use RapidFuzz for string similarity; keep each component.

- [x] **Step 6: Implement conservative SQL blocking**

Strong path: normalized artist/title + duration. Fuzzy title path allowed. Missing artist path requires very close title+duration. Explicit incompatible versions never enter automatic merge path.

- [x] **Step 7: Run tests and commit**

```bash
pytest tests/unit/test_similarity.py tests/unit/test_blocking.py -v
git add src/djlib/duplicates tests/unit/test_similarity.py tests/unit/test_blocking.py
git commit -m "feat: add duplicate candidate blocking and metadata evidence"
```

---

### Task 8: Add targeted BLAKE3 and Chromaprint evidence with calibration output

**Files:**
- Create: `src/djlib/duplicates/hashing.py`, `chromaprint.py`, `calibration.py`
- Modify: `src/djlib/cli.py`
- Test: `tests/unit/test_hashing.py`, `test_chromaprint.py`
- Test: `tests/integration/test_analysis_cache.py`

**Interfaces:**
- `HashService.ensure_current(file: FileRecord) -> str`
- `ChromaprintService.ensure_current(file: FileRecord) -> FingerprintResult`
- `fingerprint_similarity(left, right) -> float`
- CLI `duplicates calibrate`

- [x] **Step 1: Write exact-copy hash test and cache test**

Two byte-identical files must produce the same BLAKE3. Two calls with unchanged `(size,mtime_ns,analyzer_version)` invoke the hasher once.

- [x] **Step 2: Write fpcalc JSON parser/cache tests**

Use fixture:

```json
{"duration":401.25,"fingerprint":"AQADtEmUaEkSRZEG..."}
```

Assert fingerprint/duration parsing and cache invalidation after source signature changes.

- [x] **Step 3: Verify failures**

```bash
pytest tests/unit/test_hashing.py tests/unit/test_chromaprint.py tests/integration/test_analysis_cache.py -v
```

- [x] **Step 4: Implement streaming BLAKE3**

```python
h = blake3()
with path.open('rb') as f:
    for chunk in iter(lambda: f.read(1024*1024), b''):
        h.update(chunk)
return h.hexdigest()
```

Only candidate analysis requests hashes.

- [x] **Step 5: Implement `fpcalc -json` adapter and cache**

```bash
fpcalc -json <file>
```

Only run when binary hashes differ and metadata/duration remain plausible.

- [x] **Step 6: Implement similarity behind one function**

Keep the algorithm encapsulated in `fingerprint_similarity()` so it can be replaced without changing classification code.

- [x] **Step 7: Implement calibration data export**

`djlib duplicates calibrate` emits CSV/JSON containing exact positives, likely same-version different-encoding pairs, explicit version conflicts, similarity and duration delta. It never silently rewrites thresholds.

- [x] **Step 8: Run tests and commit**

```bash
pytest tests/unit/test_hashing.py tests/unit/test_chromaprint.py tests/integration/test_analysis_cache.py -v
git add src/djlib/duplicates src/djlib/cli.py tests
git commit -m "feat: add targeted hash and chromaprint evidence"
```

---

### Task 9: Add targeted technical quality analysis

**Files:**
- Create: `src/djlib/duplicates/quality.py`
- Test: `tests/unit/test_quality.py`, `tests/integration/test_quality_analysis.py`

**Interfaces:**
- `QualityAnalyzer.analyze(path: Path, file: FileRecord) -> QualityResult`
- Persists versioned `FileQualityAnalysis` rows.

- [ ] **Step 1: Write scoring-order tests**

Assert:
- corrupt < valid,
- genuinely lossless FLAC > clean MP3 320 when otherwise equivalent,
- HIGH transcode suspicion strongly penalizes a nominally lossless file,
- metadata completeness breaks close ties but cannot compensate for a large audio deficit.

- [ ] **Step 2: Verify failures**

```bash
pytest tests/unit/test_quality.py -v
```

- [ ] **Step 3: Implement integrity decode**

Use:

```bash
ffmpeg -v error -i <file> -f null -
```

Non-zero decode result marks integrity failure.

- [ ] **Step 4: Implement deterministic technical measurements**

Collect codec, effective bitrate, sample rate, bit depth, channels, peak/loudness and clipping indicators with ffmpeg/ffprobe. Store raw measurements in `details_json`.

- [ ] **Step 5: Implement conservative transcode suspicion**

Return only `NONE`, `LOW`, `MEDIUM`, `HIGH`; store heuristic evidence and never assert a definitive lossy origin without proof.

- [ ] **Step 6: Persist versioned results and prove targeting**

A plain `djlib scan` must not invoke quality analysis. Candidate analysis may invoke it once and reuse it while source signature/analyzer version remain current.

- [ ] **Step 7: Run tests and commit**

```bash
pytest tests/unit/test_quality.py tests/integration/test_quality_analysis.py -v
git add src/djlib/duplicates/quality.py tests/unit/test_quality.py tests/integration/test_quality_analysis.py
git commit -m "feat: add targeted duplicate quality analysis"
```

---

### Task 10: Classify pairwise evidence, build safe groups, choose preferred files and consolidate automatically

**Files:**
- Create: `src/djlib/duplicates/classifier.py`, `groups.py`, `preferred.py`, `service.py`
- Modify: `src/djlib/config.py`, `config.example.toml`, `src/djlib/cli.py`
- Test: `tests/unit/test_classifier.py`, `test_group_builder.py`, `test_preferred.py`
- Test: `tests/integration/test_duplicate_pipeline.py`

**Interfaces:**
- `PairClassifier.classify(evidence) -> PairDecision`
- `DuplicateGroupBuilder.build(pairs) -> list[DuplicateGroupDraft]`
- `PreferredFileSelector.choose(files) -> PreferredChoice`
- `DuplicateService.detect()`, `.analyze()`, `.run()`, `.stats()`

- [ ] **Step 1: Write classifier tests**

Expected outcomes:

```text
same binary hash => EXACT
high fingerprint + compatible metadata/duration => AUDIO_EQUIVALENT
intermediate => PROBABLE
low => DIFFERENT
high fingerprint + explicit version conflict => CONFLICT
```

Inject threshold config into tests; do not embed magic numbers in classifier tests.

- [ ] **Step 2: Write non-transitive group test**

```text
A-B AUDIO_EQUIVALENT
B-C PROBABLE
A-C DIFFERENT
```

must produce `REVIEW_REQUIRED`, never naive auto-confirmation.

- [ ] **Step 3: Write preferred-master tests**

Cover clean lossless vs MP3, suspicious lossless vs clean MP3, metadata tie-break, and historical value being unable to override a clear technical loss.

- [ ] **Step 4: Write automatic-consolidation integration test**

Two provisional tracks with exact duplicate bytes become one ACTIVE track plus one MERGED track; both files become active members of the surviving track; no source path/content changes.

- [ ] **Step 5: Verify failures**

```bash
pytest tests/unit/test_classifier.py tests/unit/test_group_builder.py tests/unit/test_preferred.py tests/integration/test_duplicate_pipeline.py -v
```

- [ ] **Step 6: Add explicit initial thresholds to config**

```toml
[duplicates.duration]
short_ms = 2000
medium_ms = 3000
long_ms = 5000

[duplicates.chromaprint]
auto_equivalent = 0.985
review_floor = 0.93
```

These are starting values subject to real-library calibration, not immutable truth.

- [ ] **Step 7: Implement classifier order**

```text
binary-equal => EXACT
strong metadata conflict + high audio similarity => CONFLICT
compatible + auto threshold => AUDIO_EQUIVALENT
plausible/intermediate => PROBABLE
otherwise => DIFFERENT
```

- [ ] **Step 8: Implement graph-aware grouping**

Auto-confirm only mutually consistent connected components. Any contradictory pair makes the group `REVIEW_REQUIRED`.

- [ ] **Step 9: Implement preferred choice**

Return explicit reasons and separate `audio_quality_score`, `metadata_quality_score`, future-compatible `historical_value`. Ordered priority: integrity → audio quality → no suspicious transcode → genuine lossless → useful resolution → clipping/anomalies → metadata → history tie-break.

- [ ] **Step 10: Implement duplicate service**

`detect`: candidates/groups.  
`analyze`: BLAKE3 → conditional Chromaprint → quality → classification → preferred proposal.  
`run`: detect + analyze + safe auto-consolidation.  
`stats`: counts by group/pair status.

- [ ] **Step 11: Add CLI commands**

```text
djlib duplicates detect
djlib duplicates analyze
djlib duplicates run
djlib duplicates stats
```

`duplicates run` does not generate HTML.

- [ ] **Step 12: Run tests and commit**

```bash
pytest tests/unit/test_classifier.py tests/unit/test_group_builder.py tests/unit/test_preferred.py tests/integration/test_duplicate_pipeline.py -v
git add src/djlib/duplicates src/djlib/config.py config.example.toml src/djlib/cli.py tests
git commit -m "feat: classify duplicates and select preferred masters"
```

---

### Task 11: Add human track overrides and immutable MERGE/SPLIT history

**Files:**
- Create: `src/djlib/curation/events.py`
- Modify: `src/djlib/catalog/service.py`
- Test: `tests/integration/test_track_curation.py`

**Interfaces:**
- `CatalogService.set_override(track_public_id, field, value)`
- `CatalogService.merge_tracks(source_public_id, target_public_id)`
- `CatalogService.split_track(source_public_id, file_public_ids) -> Track`

- [ ] **Step 1: Write override-rescan test**

Create a human artist override, modify source metadata, rescan, assert effective artist remains override while new raw/resolved source values remain visible.

- [ ] **Step 2: Write immutable merge test**

Source becomes `MERGED`, target remains ACTIVE, public IDs remain unchanged, no ID reuse, MERGE identity event exists.

- [ ] **Step 3: Write split test**

Move selected file relationship to a newly created public track ID, retain original ID for remaining files, create SPLIT identity event.

- [ ] **Step 4: Verify failures**

```bash
pytest tests/integration/test_track_curation.py -v
```

- [ ] **Step 5: Implement append-only override semantics**

Supersede previous active override with `superseded_at`; never delete historical override rows.

- [ ] **Step 6: Implement transactional merge/split**

Track statuses, relationships, preferred-file adjustment and `TrackIdentityEvent` are committed atomically.

- [ ] **Step 7: Run tests and commit**

```bash
pytest tests/integration/test_track_curation.py -v
git add src/djlib/curation/events.py src/djlib/catalog/service.py tests/integration/test_track_curation.py
git commit -m "feat: preserve human track curation and identity history"
```

---

### Task 12: Generate static HTML review and browser-side decisions JSON

**Files:**
- Create: `src/djlib/report/generator.py`, `decision-schema.json`
- Create: `src/djlib/report/templates/index.html.j2`
- Create: `src/djlib/report/assets/app.js`, `style.css`
- Modify: `src/djlib/cli.py`
- Test: `tests/unit/test_decision_schema.py`, `tests/integration/test_report_decisions.py`

**Interfaces:**
- `ReportGenerator.generate() -> ReportArtifact`
- Output `/data/reports/duplicates-review-YYYYMMDD-HHMMSS/{index.html,manifest.json}`
- Browser export schema version 1.

- [ ] **Step 1: Write JSON Schema tests**

Only these actions are valid:

```text
CONFIRM
CHANGE_PREFERRED
REJECT
DEFER
```

`CHANGE_PREFERRED` requires `preferred_file_id`.

- [ ] **Step 2: Write report artifact integration test**

Seed a REVIEW_REQUIRED group and assert manifest includes report ID, catalog revision, source signatures, group evidence, quality values, proposed preferred file and reasons.

- [ ] **Step 3: Verify failures**

```bash
pytest tests/unit/test_decision_schema.py tests/integration/test_report_decisions.py -v
```

- [ ] **Step 4: Implement deterministic catalog revision**

Revision combines latest completed scan public ID, latest duplicate-analysis run public ID and current max curation sequence.

- [ ] **Step 5: Implement static report UI**

Required:
- filters: classification/reason, confidence, format, decision state,
- sorts: confidence, quality delta, members, path,
- previous/next, unresolved-only, keyboard shortcuts,
- side-by-side file metadata,
- pair evidence,
- proposed preferred + rationale.

No server and no audio previews.

- [ ] **Step 6: Implement reversible in-browser decisions and export**

`app.js` stores page-local decisions and exports:

```json
{
  "schema_version": 1,
  "report_id": "rpt_...",
  "catalog_revision": "...",
  "generated_at": "...",
  "decisions": []
}
```

- [ ] **Step 7: Add CLI and run tests**

```bash
djlib duplicates report
pytest tests/unit/test_decision_schema.py tests/integration/test_report_decisions.py -v
```

- [ ] **Step 8: Commit**

```bash
git add src/djlib/report src/djlib/cli.py tests/unit/test_decision_schema.py tests/integration/test_report_decisions.py
git commit -m "feat: generate static duplicate review reports"
```

---

### Task 13: Import decisions atomically and maintain durable curation JSONL

**Files:**
- Create: `src/djlib/curation/decisions.py`, `journal.py`
- Modify: `src/djlib/cli.py`
- Test: `tests/integration/test_decision_import.py`, `test_curation_journal.py`

**Interfaces:**
- `DecisionImporter.import_file(path: Path) -> ImportSummary`
- `CurationJournal.export_pending() -> int`

- [ ] **Step 1: Write stale-import rejection tests**

Atomically reject unsupported schema, unknown report, changed catalog/group, vanished group, changed affected file signature. Assert zero partial writes.

- [ ] **Step 2: Write journal-gap repair test**

Commit sequences 1 and 2, export only 1, call `export_pending()`, assert sequence 2 is appended exactly once.

- [ ] **Step 3: Verify failures**

```bash
pytest tests/integration/test_decision_import.py tests/integration/test_curation_journal.py -v
```

- [ ] **Step 4: Implement validation before business write**

Order:

```text
JSON Schema
schema_version
report_id
catalog/group revision
IDs
current group state
file signatures
```

No force override.

- [ ] **Step 5: Implement decision semantics**

`CONFIRM`: group CONFIRMED + proposed preferred accepted.  
`CHANGE_PREFERRED`: CONFIRMED + human-selected preferred.  
`REJECT`: REJECTED + durable human negative constraint preventing future silent re-merge.  
`DEFER`: DEFERRED + identities remain separate.

- [ ] **Step 6: Implement SQLite-first monotonic curation events**

Accepted actions insert committed `CurationEvent` rows in the same DB transaction. After COMMIT, append pending events to `/data/curation/events.jsonl`, then advance `last_exported_curation_sequence`.

- [ ] **Step 7: Add CLI and run tests**

```bash
djlib duplicates import-decisions /data/decisions/decisions.json
pytest tests/integration/test_decision_import.py tests/integration/test_curation_journal.py -v
```

- [ ] **Step 8: Commit**

```bash
git add src/djlib/curation src/djlib/cli.py tests/integration/test_decision_import.py tests/integration/test_curation_journal.py
git commit -m "feat: import human decisions and persist curation journal"
```

---

### Task 14: Add operation runs, persistent logging and `djlib doctor`

**Files:**
- Create: `src/djlib/logging.py`, `src/djlib/runs.py`, `src/djlib/doctor.py`
- Modify: `src/djlib/db/models.py`, `src/djlib/cli.py`
- Create: `alembic/versions/0002_operation_runs.py`
- Test: `tests/integration/test_runs.py`, `test_doctor.py`

**Interfaces:**
- operation run IDs: `scan_`, `dup_`, `report_`, `import_`
- CLI `runs show <run-id>`
- `Doctor.run() -> DoctorReport`

- [ ] **Step 1: Write run/logging tests**

Assert operation row timestamps/status/summary and `/data/logs/djlib.log` context contain command + run ID.

- [ ] **Step 2: Write doctor tests**

Cover:
- `/music` missing or writable,
- `/data` not writable,
- SQLite/migration problem,
- missing exiftool/ffprobe/fpcalc/BLAKE3,
- curation sequence gap,
- invalid preferred/membership relationships.

- [ ] **Step 3: Verify failures**

```bash
pytest tests/integration/test_runs.py tests/integration/test_doctor.py -v
```

- [ ] **Step 4: Add OperationRun model/migration and logging**

Use rotating file logging. Support `-v`, `-vv`, `--log-level DEBUG`.

- [ ] **Step 5: Implement safe read-only probe**

Attempt creation of a randomized, guaranteed-nonexistent probe under `/music`. Read-only/permission failure is PASS. If creation unexpectedly succeeds, immediately remove only that probe and return health FAIL. Never touch an existing media file.

- [ ] **Step 6: Implement dependency and DB invariant checks**

Use `shutil.which` for tools and relational queries for active membership/preferred-file invariants.

- [ ] **Step 7: Implement journal repair only on explicit flag**

```text
djlib doctor
djlib doctor --repair-journal
```

Plain doctor reports a sequence gap; repair flag calls `CurationJournal.export_pending()`.

- [ ] **Step 8: Add `runs show` and run tests**

```bash
alembic upgrade head
pytest tests/integration/test_runs.py tests/integration/test_doctor.py -v
```

- [ ] **Step 9: Commit**

```bash
git add src/djlib/logging.py src/djlib/runs.py src/djlib/doctor.py src/djlib/db/models.py src/djlib/cli.py alembic/versions/0002_operation_runs.py tests/integration
git commit -m "feat: add runtime observability and health checks"
```

---

### Task 15: Implement curation replay, rebuild and idempotence guarantees

**Files:**
- Create: `src/djlib/curation/replay.py`
- Modify: `src/djlib/cli.py`, `src/djlib/scan/service.py`, `src/djlib/duplicates/service.py`
- Test: `tests/unit/test_curation_replay.py`
- Test: `tests/integration/test_rebuild.py`, `test_idempotence.py`

**Interfaces:**
- `CurationReplay.replay(path: Path) -> ReplaySummary`
- `RebuildService.rebuild() -> RebuildSummary`
- CLI `djlib rebuild`

- [ ] **Step 1: Write replay test**

After fresh scan, replay must restore stable track public IDs, preferred files, overrides, confirm/reject decisions and merge/split outcomes using stable file path/signature references carried by events.

- [ ] **Step 2: Write full rebuild test**

```text
scan fixture
apply human curation
export journal
snapshot curated projection
delete SQLite
migrate fresh DB
full scan
replay JSONL
compare projection
```

- [ ] **Step 3: Write idempotence tests**

Second unchanged scan: `0 new`, `0 changed`, N unchanged, no derived invalidation.  
Second duplicate run: no unnecessary BLAKE3/fpcalc/quality calls.

- [ ] **Step 4: Verify failures**

```bash
pytest tests/unit/test_curation_replay.py tests/integration/test_rebuild.py tests/integration/test_idempotence.py -v
```

- [ ] **Step 5: Implement strict replay**

If an event cannot safely map a file identity, fail that replay with event ID/reason; never guess among ambiguous candidates.

- [ ] **Step 6: Implement rebuild**

Sequence:

```text
health-check /music
backup catalog.sqlite to catalog.sqlite.pre-rebuild-<timestamp>
create/migrate fresh DB
full scan
replay events.jsonl
run invariants
retain backup until successful completion
```

Never modify `/music`.

- [ ] **Step 7: Fix cache keys to guarantee idempotence**

Derived cache validity requires `(size_bytes, mtime_ns, analyzer_version)` match.

- [ ] **Step 8: Run tests and commit**

```bash
pytest tests/unit/test_curation_replay.py tests/integration/test_rebuild.py tests/integration/test_idempotence.py -v
git add src/djlib/curation/replay.py src/djlib/cli.py src/djlib/scan/service.py src/djlib/duplicates/service.py tests
git commit -m "feat: rebuild catalog and prove incremental idempotence"
```

---

### Task 16: Build deterministic audio fixtures and prove Milestone-1 acceptance end to end

**Files:**
- Create: `tests/fixtures/build_audio_fixtures.py`
- Create generated fixtures under `tests/fixtures/library/`
- Create: `tests/integration/test_end_to_end.py`
- Modify: `README.md`

**Interfaces:**
- Deterministic local fixtures covering exact duplicate, same audio re-encode, version conflict, malformed tags, filename fallback and corrupt file.

- [ ] **Step 1: Write fixture builder**

Generate synthetic audio using ffmpeg only; no copyrighted music in repository. Produce:

```text
exact-copy/
mp3-vs-flac/
remix/
radio-edit/
malformed-tags/
filename-fallback/
corrupt/
```

Use synthetic tone/noise PCM; re-encode same PCM for FLAC/MP3 equivalence; create shortened/extended variants for conflict fixtures.

- [ ] **Step 2: Write end-to-end test before fixture generation**

Flow:

```text
temp /music read-only policy simulation
scan
duplicates run
stats
report
create valid decisions.json
import
catalog inspect
rebuild
compare curated projection
```

Hash every source fixture before/after and assert no source change.

- [ ] **Step 3: Verify missing-fixture failure**

```bash
pytest tests/integration/test_end_to_end.py -v
```

- [ ] **Step 4: Generate fixtures and run full suite**

```bash
python tests/fixtures/build_audio_fixtures.py
pytest -v
pytest --cov=djlib --cov-report=term-missing
```

Critical parser, conflict, decision, replay and rebuild branches must all have explicit behavioral tests.

- [ ] **Step 5: Document operator workflow**

README must contain:

```bash
djlib doctor
djlib scan
djlib duplicates run
djlib duplicates stats
djlib duplicates calibrate
djlib duplicates report
djlib duplicates import-decisions /data/decisions/decisions.json
djlib catalog inspect <public-id>
```

State explicitly that physical cleanup is outside `djlib`.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures tests/integration/test_end_to_end.py README.md
git commit -m "test: validate milestone one end to end"
```

---

## Production acceptance on the real DJ archive

Perform this only after all 16 implementation tasks pass in fixtures.

- [ ] **Acceptance 1: Verify LXC safety**

```bash
djlib doctor
```

Required: `/music` read-only PASS, `/data` writable PASS, dependencies/migrations/invariants PASS.

- [ ] **Acceptance 2: First real scan**

```bash
djlib scan
```

Expected: library-scale catalogue, `SUCCESS` or `SUCCESS_WITH_ERRORS`; individual bad files do not abort the scan.

- [ ] **Acceptance 3: Prove incremental scan immediately**

```bash
djlib scan
```

Expected: 0 new, 0 changed, current files unchanged, no unnecessary metadata extraction.

- [ ] **Acceptance 4: Run duplicate pipeline and inspect stats**

```bash
djlib duplicates run
djlib duplicates stats
```

Confirm expensive evidence is limited to candidates.

- [ ] **Acceptance 5: Calibrate before trusting Chromaprint automation at scale**

```bash
djlib duplicates calibrate
```

Review exact binary duplicates as positive controls, explicit remix/edit conflicts as negative controls, and same-version multi-encoding pairs as real-world positives. If false positives exist at the default threshold, raise the configured threshold and re-run affected analysis. Record the final production threshold in operator documentation.

- [ ] **Acceptance 6: Generate/review static report**

```bash
djlib duplicates report
```

Verify filters/sorting/navigation, explanations and exactly four actions. Confirm no preview-audio feature exists in V1.

- [ ] **Acceptance 7: Import a small human-reviewed batch first**

Review 20–50 mixed groups and export decisions. Then:

```bash
djlib duplicates import-decisions /data/decisions/decisions.json
```

Verify atomic import and JSONL sequence export.

- [ ] **Acceptance 8: Prove human precedence**

```bash
djlib duplicates run
```

Confirmed/rejected/preferred human decisions must not be silently reversed.

- [ ] **Acceptance 9: Prove rebuild**

```bash
djlib rebuild
djlib doctor
djlib catalog stats
```

Verify public IDs, human decisions, overrides and identity history survive replay.

---

## Implementation order / reviewer gates

```text
1  bootstrap + LXC helpers
2  database + migrations
3  incremental scanner
4  metadata extraction
5  resolver
6  provisional catalogue       ← major review gate
7  candidate blocking
8  BLAKE3 + Chromaprint
9  quality analysis
10 classification/grouping/preferred + auto consolidation ← major review gate
11 overrides + merge/split
12 static report
13 decision import + journal    ← major review gate
14 observability + doctor
15 replay/rebuild/idempotence   ← major review gate
16 end-to-end fixtures
production acceptance           ← final safety gate
```

---

## Explicit deferred work

Do not implement in this plan:

- Traktor history/session import,
- Serato history/session import,
- historical play counts,
- transition graph,
- CORE / STRONG / KNOWN tiers,
- narrative/function tags,
- active Engine DJ library generation,
- Internet metadata enrichment,
- automatic cleanup/deletion of `/music`,
- source retagging,
- persistent web application,
- audio previews in duplicate review.

The audio-preview concept remains explicitly parked for a future v2/v3.

---

## Plan self-review

### Spec coverage

Mapped to implementation tasks:

- physical RO LXC/source safety → Tasks 1, 14, production acceptance,
- local `/data` state → Tasks 1–2,
- Typer CLI → Tasks 1, 3, 6, 8, 10, 12–15,
- SQLAlchemy/Alembic/SQLite pragmas → Task 2,
- incremental/full scan → Tasks 3–4, 15,
- ExifTool + ffprobe → Task 4,
- tag→filename conservative resolution → Task 5,
- version/edition/feat separation → Task 5,
- provisional one-file/one-track → Task 6,
- immutable identities/overrides/merge-split → Task 11,
- candidate blocking and pair evidence → Task 7,
- targeted BLAKE3/Chromaprint/calibration → Task 8,
- targeted quality → Task 9,
- conflict-safe classification/grouping/preferred → Task 10,
- static report/four actions/no audio → Task 12,
- stale-safe atomic decision import → Task 13,
- durable JSONL curation → Tasks 13, 15,
- run IDs/logging/doctor → Task 14,
- idempotence/rebuild → Task 15,
- complete fixture/E2E workflow → Task 16,
- real-library acceptance → production acceptance section.

### Placeholder scan

No implementation task relies on an undefined future component. Values intended to be calibrated are given explicit initial configuration defaults and a concrete calibration workflow. Deferred v2/v3 work is excluded rather than left incomplete.

### Type/interface consistency

Public concepts are introduced once and reused consistently:

```text
DjlibConfig
FileRecord
Track
TrackFile
ScanService
MetadataExtractor
MetadataResolver
CatalogService
CandidateBlocker
HashService
ChromaprintService
QualityAnalyzer
PairClassifier
DuplicateGroupBuilder
PreferredFileSelector
DuplicateService
ReportGenerator
DecisionImporter
CurationJournal
CurationReplay
Doctor
```

No later task requires a public service that is not introduced in an earlier task.
