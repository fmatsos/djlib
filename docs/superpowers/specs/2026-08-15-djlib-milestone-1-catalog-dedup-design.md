# djlib — Milestone 1: Catalogue & Deduplication

**Date:** 2026-08-15  
**Status:** Design approved  
**Scope:** Milestone 1 only  
**Target runtime:** Dedicated Proxmox LXC  
**Primary source:** `/mnt/tank/djing` (mounted read-only as `/music`)

## 1. Objective

Build a local, deterministic Python CLI named `djlib` that catalogs the historical DJ library, resolves metadata conservatively, detects exact and audio-equivalent duplicates, recommends a preferred file per logical track version, and supports human review through a static HTML report.

The tool must never mutate the source library. Its purpose is analysis, classification, recommendation, and persistence of human curation decisions.

Milestone 1 deliberately excludes DJ-history scoring, narrative tagging, Engine DJ export, Internet enrichment, automatic cleanup, and audio previews.

## 2. Non-negotiable invariants

1. `/music` is physically mounted read-only inside the LXC.
2. `djlib` never renames, moves, deletes, or retags source files.
3. Duplicate detection may be automatic; source deletion never is.
4. Human decisions always override automatic decisions.
5. A `track` represents one precise audio version, not merely an artist/title pair.
6. Different remixes, edits, bootlegs, VIPs, radio edits, live versions, etc. remain distinct tracks.
7. The same audio version encoded as FLAC/MP3/M4A/etc. may map to one track with multiple files.
8. Track public identifiers are immutable.
9. The catalogue is incrementally maintainable and fully reconstructible.
10. All automatic conclusions must be explainable through stored evidence.

## 3. Runtime architecture

### 3.1 Proxmox / LXC layout

Host:

```text
/mnt/tank/djing   source DJ archive
/mnt/tank/djlib   djlib state
```

LXC mounts:

```text
/music  -> /mnt/tank/djing   READ ONLY
/data   -> /mnt/tank/djlib   READ/WRITE
```

LXC-local application stack:

```text
Python
Typer
SQLAlchemy 2.x
Alembic
SQLite
ExifTool
ffprobe / FFmpeg
Chromaprint / fpcalc
BLAKE3
```

### 3.2 State layout

```text
/data/
├── catalog.sqlite
├── cache/
│   ├── chromaprint/
│   └── analysis/
├── curation/
│   └── events.jsonl
├── reports/
├── decisions/
└── logs/
    └── djlib.log
```

## 4. Component boundaries

Each component has one responsibility.

### Scanner

Discovers audio files under `/music` and records only cheap filesystem state:

- relative path
- size
- `mtime_ns`
- presence

It does not know about duplicate matching or Chromaprint.

### Metadata extractor

Uses:

- ExifTool for embedded tags
- ffprobe for technical stream/container metadata

It extracts raw metadata only and never writes to source files.

### Resolver

Produces structured, normalized interpretations from raw data:

- artist
- title
- version
- edition
- featured artists

Priority:

```text
valid embedded tag
    -> filename fallback
    -> unknown
```

It never merges tracks.

### Catalogue

Persists files, provisional track identities, curation, scan history, derived-analysis state, and immutable identity history.

### Duplicate detector

Builds candidate groups using metadata blocking and duration compatibility.

### Evidence pipeline

Computes expensive evidence only when justified:

- BLAKE3 binary hash
- Chromaprint
- targeted technical quality analysis

### Decision engine

Classifies pair relationships and selects a preferred file when confidence is sufficient.

### Report generator

Generates a static HTML review report. It has no direct database write path.

### Decision importer

Validates exported `decisions.json`, checks staleness, applies decisions transactionally, and appends durable curation events.

## 5. Incremental scan semantics

Default command:

```bash
djlib scan
```

Rules:

```text
new path
-> insert file
-> extract metadata
-> resolve metadata
-> create provisional track

same path + same size + same mtime_ns
-> unchanged
-> skip metadata extraction
-> keep derived analyses

known path + size or mtime_ns changed
-> changed
-> re-extract metadata
-> re-resolve metadata
-> invalidate binary hash
-> invalidate Chromaprint
-> invalidate quality analysis

known file not observed in completed scan
-> is_present = false
-> keep file record and historical relationships
```

Full scan:

```bash
djlib scan --full
```

Rebuild:

```bash
djlib rebuild
```

A rebuild recreates SQLite from `/music` and replays durable human curation events.

## 6. Scan run model

`scan_runs` records:

- start/end timestamps
- status: `SUCCESS`, `SUCCESS_WITH_ERRORS`, `FAILED`
- files seen/new/changed/unchanged/missing/failed
- scanner version
- error summary

Per-file extraction errors do not abort the entire scan. Infrastructure failures do.

## 7. File model

A file is a physical object under `/music`.

Core fields:

```text
id
public_id
relative_path UNIQUE
size_bytes
mtime_ns
extension
container_format
codec
bitrate
sample_rate
bit_depth
channels
duration_ms
```

Raw embedded metadata:

```text
title_raw
artist_raw
album_raw
album_artist_raw
genre_raw
bpm_raw
key_raw
comment_raw
raw_metadata_json
```

Resolved metadata:

```text
resolved_artist
resolved_title
resolved_version
resolved_edition
artist_source
title_source
version_source
edition_source
```

Derived analysis state:

```text
binary_hash
binary_hash_status
chromaprint
chromaprint_duration_ms
chromaprint_status
quality_status
```

Lifecycle fields:

```text
is_present
first_seen_at
last_seen_at
metadata_updated_at
created_at
updated_at
```

Raw values are immutable representations of the source at the time of extraction. Resolver output may be recomputed; human corrections do not modify raw values.

## 8. Track identity model

A track is one precise audio version.

Examples of distinct tracks:

```text
Track
Track (X Remix)
Track (Radio Edit)
Track (Extended Mix)
Track (Bootleg)
Track (VIP)
```

Examples that may belong to one track:

```text
same Original Mix as FLAC
same Original Mix as MP3 320
same Original Mix as M4A
```

Track fields:

```text
id
public_id UUID/ULID-like stable identifier
status
artist
title
version
edition
artist_normalized
title_normalized
version_normalized
edition_normalized
duration_reference_ms
preferred_file_id
identity_confidence
merged_into_track_id
created_at
updated_at
```

Statuses:

```text
PROVISIONAL
ACTIVE
MERGED
SUPERSEDED
```

Every newly scanned file initially receives its own provisional track. No two files are merged solely because their tags resemble each other.

## 9. Featured artists

Featuring is structured rather than flattened.

File-level parsed values:

```text
file_featured_artists
- file_id
- position
- name
- normalized_name
- source
```

Track-level effective values:

```text
track_featured_artists
- track_id
- position
- name
- normalized_name
- source
```

Recognized markers include `feat.`, `ft.`, and `featuring`.

Featuring is a matching signal, not an absolute exclusion rule:

```text
Artist feat. X - Track
Artist - Track
```

may still be candidates.

Conflicting explicit featured artists reduce confidence and force more conservative matching.

## 10. Metadata resolution

### 10.1 Three layers

```text
RAW
-> source tag value

RESOLVED
-> automatic interpretation

EFFECTIVE
-> human override if present, otherwise resolved value
```

### 10.2 Filename fallback

Conservative recognized patterns include:

```text
Artist - Title.ext
Artist – Title.ext
01 - Artist - Title.ext
01. Artist - Title.ext
Artist - Title (Version).ext
```

Ambiguous filenames do not produce invented metadata.

### 10.3 Normalization

Matching normalization may perform:

- Unicode canonicalization
- case folding
- whitespace normalization
- trim
- normalization of typographic quotes/apostrophes/dashes
- conservative removal of non-significant punctuation

It must not blindly remove tokens such as `&`, `+`, `vs.`, or `pres.`.

### 10.4 Version parsing

Recognized version markers include:

- Remix
- Mix
- Original Mix
- Extended Mix
- Radio Edit
- Club Mix
- Edit / Re-edit
- Rework
- Bootleg
- Mashup
- VIP
- Dub
- Instrumental
- Live

Example:

```text
Meet Her At The Love Parade (Nalin & Kane Remix)
```

becomes:

```text
title   = Meet Her At The Love Parade
version = Nalin & Kane Remix
```

### 10.5 Edition parsing

Edition is distinct from version.

Recognized concepts include:

- Remaster / Remastered
- anniversary edition
- deluxe edition
- reissue

A remaster may remain the same musical track version while representing a different edition/mastering.

## 11. Human overrides

Semantic corrections live at track level, not file level.

`track_overrides`:

```text
id
track_id
field
value_json
created_at
superseded_at
```

Priority:

```text
human override
-> automatically resolved value
-> raw value
```

Overrides survive rescans. A source metadata change may raise a notice but must never silently overwrite human curation.

## 12. Track/file relationships

`track_files` records:

```text
track_id
file_id
relationship
confidence
decision_source
is_active
created_at
updated_at
```

Relationship values:

```text
PRIMARY
EXACT_DUPLICATE
AUDIO_EQUIVALENT
PROBABLE
```

A physical file may belong to only one active logical track.

## 13. Stable identity and manual merge/split

Human `MERGE` and `SPLIT` decisions are persistent and stronger than automatic analysis.

### Merge

```text
trk_A ACTIVE
trk_B ACTIVE

-> human MERGE

trk_A ACTIVE
trk_B MERGED -> trk_A
```

No ID reuse occurs.

### Split

```text
trk_A
- file 1
- file 2
- file 3

-> human SPLIT

trk_A
- file 1
- file 2

trk_C
- file 3
```

### Identity event history

`track_identity_events` records:

```text
id
event_uuid
event_type: MERGE | SPLIT | UNMERGE | RESOLVE
source_track_public_id
target_track_public_id
payload_json
created_at
```

## 14. Duplicate candidate blocking

The detector must not perform all-vs-all Chromaprint comparisons.

Candidate generation uses conservative blocking on:

```text
artist_normalized
title_normalized
version compatibility
duration bucket
```

Allowed tolerance examples:

- exact normalized artist + title + close duration -> strong candidate
- exact artist + fuzzy-close title + close duration -> candidate
- missing artist on one side + matching title + very close duration -> weak candidate
- explicit incompatible versions -> exclude from automatic merge path

Initial duration windows are configurable and later calibrated. A reasonable starting design is:

```text
<= 5 min   : ±2 s
5-10 min   : ±3 s
> 10 min   : ±5 s
```

These values are defaults, not immutable thresholds.

## 15. Pairwise evidence model

Duplicate evidence is stored pairwise, not only at group level.

`duplicate_pair_evidence`:

```text
group_id
left_file_id
right_file_id
metadata_similarity
artist_similarity
title_similarity
version_compatibility
edition_compatibility
featured_artist_similarity
duration_delta_ms
binary_hash_equal
chromaprint_similarity
classification
confidence
evidence_json
created_at
```

Pair classifications:

```text
EXACT
AUDIO_EQUIVALENT
PROBABLE
DIFFERENT
CONFLICT
```

## 16. Duplicate groups

`duplicate_groups`:

```text
id
public_id
status
confidence
proposed_preferred_file_id
matcher_version
created_at
resolved_at
```

Statuses:

```text
DETECTED
AUTO_CONFIRMED
REVIEW_REQUIRED
CONFIRMED
REJECTED
DEFERRED
```

`duplicate_group_members` relates groups to files.

Group construction must not rely on naive transitive closure. If pairwise evidence conflicts, the group becomes `REVIEW_REQUIRED`.

## 17. Binary hashing

Binary hashing is targeted to candidate groups rather than the entire library at initial scan.

Preferred algorithm: BLAKE3.

Rule:

```text
identical binary hash
-> EXACT
-> automatic merge permitted
```

Hashes are cached until source signature changes.

## 18. Chromaprint strategy

Chromaprint is calculated only when:

```text
binary hash differs
AND metadata/duration remain plausible
```

Fingerprints are cached and invalidated when the file changes.

Classification uses multiple signals. A high Chromaprint similarity alone is not sufficient for automatic merge when metadata strongly conflicts.

Conceptual zones:

```text
very high similarity + compatible duration + compatible metadata
-> AUDIO_EQUIVALENT

intermediate similarity
-> PROBABLE / review

low similarity
-> DIFFERENT
```

Numeric thresholds must be calibrated against the real library rather than hard-coded blindly.

Planned command:

```bash
djlib duplicates calibrate
```

Calibration samples should include:

- exact binary duplicates
- same version in different encodings
- remixes
- radio/extended edits
- bootlegs
- plausible false positives

## 19. Metadata conflict rules

Explicitly different versions are strong negative evidence.

Examples requiring conservative treatment:

```text
Original Mix vs X Remix
Radio Edit vs Extended Mix
Live vs Studio
Instrumental vs Vocal
Bootleg vs Original
X Remix vs Y Remix
```

If audio evidence is unexpectedly similar while version metadata conflicts, classification is `CONFLICT`, not silent auto-merge.

`Original Mix` versus an empty version remains potentially compatible but with reduced confidence.

## 20. Targeted quality analysis

Detailed quality analysis runs only for duplicate candidate groups, never across the full catalogue by default.

Signals include:

- decode/integrity check
- codec and effective bitrate
- lossless/lossy
- sample rate
- bit depth
- channels
- peak
- loudness
- clipping indication
- lossy-to-lossless transcode suspicion
- metadata completeness

Transcode suspicion values:

```text
NONE
LOW
MEDIUM
HIGH
```

This is an heuristic indication, not a definitive provenance claim.

Versioned analysis is stored in `file_quality_analyses`:

```text
id
file_id
analyzer_version
integrity_status
lossless_status
transcode_suspicion
clipping_status
quality_score
details_json
created_at
```

## 21. Preferred-file selection

Selection priority:

1. file integrity
2. intrinsic audio quality
3. absence of suspicious transcode
4. lossless over lossy when genuinely lossless
5. useful technical resolution
6. absence of severe clipping/anomalies
7. metadata completeness
8. provenance/history only as tiebreaker

Historical DJ usage must never allow a technically inferior file to beat a clearly superior master merely because it was played more often.

The system stores distinct dimensions rather than one opaque number:

```text
audio_quality_score
metadata_quality_score
historical_value   # unused until later milestone, but schema-compatible
```

Automatic preferred selection is allowed when the group is confidently confirmed and the recommendation is sufficiently clear.

No file is ever deleted as a consequence.

## 22. Human duplicate review

Milestone 1 uses a static HTML report, not a web application.

Command:

```bash
djlib duplicates report
```

Output:

```text
/data/reports/duplicates-review-YYYYMMDD-HHMMSS/
├── index.html
└── manifest.json
```

No HTTP server and no database connection from the browser.

### Required report capabilities

Filters:

- classification/review reason
- confidence
- format
- decision state

Sorting:

- confidence
- quality delta
- number of files
- path

Navigation:

- previous/next group
- unresolved only
- keyboard shortcuts

Per-group display:

- effective artist/title/version
- each file path
- format/codec
- bitrate
- sample rate/bit depth
- duration
- size
- metadata completeness
- technical quality
- transcode suspicion
- pairwise evidence
- proposed classification
- proposed preferred file
- reasons

### V1 review actions

Exactly four actions:

```text
CONFIRM
CHANGE_PREFERRED
REJECT
DEFER
```

MERGE/SPLIT and metadata editing are separate workflows and are not added to this report in milestone 1.

### Deferred feature

Audio A/B previews are explicitly deferred to a future v2/v3. A future implementation may generate short local preview assets only for `review_required` groups while keeping the report serverless.

## 23. Decision export

The browser exports `decisions.json`.

Conceptual schema:

```json
{
  "schema_version": 1,
  "report_id": "rpt_...",
  "catalog_revision": "...",
  "generated_at": "...",
  "decisions": [
    {
      "group_id": "dup_...",
      "decision": "CONFIRM",
      "preferred_file_id": "fil_...",
      "reviewed_at": "..."
    }
  ]
}
```

The report itself performs no persistence beyond exporting this file.

## 24. Decision import

Command:

```bash
djlib duplicates import-decisions decisions.json
```

Validation before write:

1. JSON Schema
2. schema version
3. report ID
4. catalogue revision
5. group/file IDs
6. current group state
7. source-file signatures where relevant

Stale decisions are rejected if:

- a group changed
- a group disappeared
- an affected file changed

No `--force` override in milestone 1.

Import is atomic: either all decisions are applied or none are.

## 25. Durable curation journal

SQLite is the immediate transactional source for accepted curation operations. Human decisions must additionally be exportable/replayable independently of SQLite.

Append-only file:

```text
/data/curation/events.jsonl
```

Each event has:

- monotonic sequence
- stable event ID
- event type
- stable track/file references
- enough file identity/signature information to replay safely
- timestamp
- payload

SQLite stores the last exported curation sequence.

If SQLite commits but the process dies before JSONL export, `djlib doctor` detects the sequence gap and repairs the journal from the committed SQLite events.

Rebuild flow:

```text
scan /music
-> recreate derived catalogue
-> replay events.jsonl
-> restore human overrides/duplicate decisions/merge-split history/public IDs
```

## 26. Observability

Persistent log:

```text
/data/logs/djlib.log
```

Log context includes:

- timestamp
- level
- command
- run ID
- file/group ID when applicable
- message
- exception details

CLI verbosity:

```bash
-v
-vv
--log-level DEBUG
```

Major operations receive stable run IDs:

```text
scan_...
dup_...
report_...
import_...
```

Planned inspection:

```bash
djlib runs show <run-id>
```

## 27. Health checks

Command:

```bash
djlib doctor
```

Checks:

- `/music` exists
- `/music` is actually read-only
- controlled write attempt to `/music` fails
- `/data` is writable
- SQLite is readable
- migrations are current
- ExifTool exists
- ffprobe exists
- fpcalc/Chromaprint exists
- BLAKE3 support exists
- curation JSONL sequence matches SQLite
- preferred files still exist or are properly marked missing
- no active file belongs to multiple active tracks
- no invalid active-track relationships exist

## 28. CLI surface — milestone 1

Primary commands:

```bash
djlib doctor

djlib scan
djlib scan --full
djlib rebuild

djlib catalog stats
djlib catalog inspect <id>

djlib duplicates detect
djlib duplicates analyze
djlib duplicates run
djlib duplicates calibrate
djlib duplicates stats
djlib duplicates report
djlib duplicates import-decisions <file>

djlib runs show <run-id>
```

`duplicates run` performs:

```text
candidate detection
-> targeted binary hashing
-> targeted Chromaprint
-> targeted quality analysis
-> classification
-> preferred-file recommendation
```

It does not generate a report automatically.

Useful commands may expose `--json` for scripting.

## 29. Database technology

Persistence stack:

```text
SQLAlchemy 2.x
Alembic
SQLite
```

SQLite configuration includes at least:

```text
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
busy_timeout
```

All business writes use explicit transactions.

No unnecessary artist/album/genre reference model is introduced in milestone 1.

## 30. Offline requirement

Milestone 1 is fully local and deterministic.

Allowed inputs:

- embedded tags
- filename
- historical path/provenance
- duration/technical metadata
- binary hash
- Chromaprint
- targeted technical analysis
- human decisions

Explicitly excluded:

- MusicBrainz lookup
- Discogs lookup
- Beatport lookup
- web search
- automatic Internet metadata correction

Future enrichment may be added as an optional layer that never defines core track identity by itself.

## 31. Error handling

Per-file errors:

```text
metadata extraction failure
corrupt file
ffprobe failure on one file
```

-> record error state  
-> increment failed counter  
-> continue scan

Global failures:

```text
source mount missing
source unexpectedly writable when policy requires RO
/data unavailable
SQLite inaccessible
invalid configuration
required executable missing
```

-> fail command immediately

## 32. Testing strategy

### Unit tests

Must cover:

- filename parser
- Unicode/name normalizer
- version parser
- edition parser
- featured-artist parser
- duration compatibility
- candidate blocking
- metadata similarity
- version compatibility
- decision thresholds
- preferred-file scoring
- report decision JSON validation
- curation event replay

### Anti-false-positive fixtures

Must explicitly verify non-merging of:

```text
Original Mix vs Radio Edit
Original Mix vs Extended Mix
X Remix vs Y Remix
Live vs Studio
Instrumental vs Vocal
Bootleg vs Original
```

### Integration fixture library

```text
fixtures/library/
├── exact-copy/
├── mp3-vs-flac/
├── remix/
├── radio-edit/
├── malformed-tags/
├── filename-fallback/
└── corrupt/
```

End-to-end integration flow:

```text
scan
-> detect
-> analyze
-> report
-> import decisions
-> rebuild
```

### Idempotence

Running two scans with no source changes must produce:

```text
0 new
0 changed
N unchanged
0 unnecessary derived-analysis invalidations
```

Running duplicate analysis twice without relevant changes must not unnecessarily recompute Chromaprint or quality analysis.

### Rebuild test

After deleting the SQLite catalogue and rebuilding from `/music` plus `events.jsonl`, the following must be restored:

- same active track identities
- same public IDs
- same preferred-file decisions
- same human overrides
- same duplicate confirmations/rejections
- same merge/split outcomes

## 33. Milestone 1 acceptance criteria

Milestone 1 is accepted when the real library can successfully execute:

```bash
djlib doctor
djlib scan
djlib duplicates run
djlib duplicates stats
djlib duplicates report
```

After human review in the report:

```bash
djlib duplicates import-decisions decisions.json
```

and:

```bash
djlib catalog inspect <track-or-file-id>
```

must expose the effective identity, source metadata, duplicate relationships, evidence, preferred-file rationale, and human decision provenance.

Required invariants at acceptance:

```text
/music physically read-only
no source mutation
no source deletion
no source retagging
incremental scans work
Chromaprint is targeted/cached
automatic decisions are explainable
ambiguous cases reach human review
human decisions override automation
track public IDs remain stable
catalogue rebuild is proven
curation survives SQLite loss
```

## 34. Explicitly out of scope for milestone 1

- Traktor history import
- Serato history import
- DJ-history scoring
- CORE / STRONG / KNOWN tiers
- transition graph
- narrative/function tags
- active Engine DJ library generation
- Internet metadata enrichment
- automatic cleanup or deletion of `/music`
- source retagging
- audio previews in duplicate review
- full web application

## 35. Future milestones

### Milestone 2 — DJ history & scoring

Planned concepts:

- Traktor history/session import
- Serato crate/session import
- historical play counts
- transition graph
- recurring sequence detection
- historical tiers such as CORE / STRONG / KNOWN

### Later milestone — active DJ library

Planned concepts:

- human listening/qualification workflow
- narrative roles
- energy/texture tags
- READY/CORE selection
- stable copied active-library files
- Engine DJ workflow

### Deferred v2/v3 report feature

Static duplicate-review report with local audio previews generated only for ambiguous groups, without requiring a persistent web server.

## 36. Design principles summary

The milestone follows four explicit boundaries:

```text
SCAN
= observe what physically exists

RESOLVE
= interpret metadata without merging identities

DEDUPLICATE
= establish identity using progressively stronger evidence

CURATE
= let humans override ambiguity without modifying the archive
```

The original DJ archive remains immutable. `djlib` creates a structured, explainable and recoverable analytical layer above it.
