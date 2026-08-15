---
paths:
  - "src/djlib/duplicates/**/*.py"
  - "src/djlib/catalog/**/*.py"
description: Conservative track identity and targeted, explainable duplicate evidence
---

# Track identity and duplicate detection (djlib)

## The invariant
A `track` is one precise audio version, not an artist/title pair. Different remixes, edits,
bootlegs, VIPs, radio edits, and live versions are distinct tracks and must never be silently
merged (design §2, §8, §19). Multiple encodings (FLAC/MP3/M4A) of the *same* precise version may
share one track.

## Rules
- Every newly scanned file starts as its own `PROVISIONAL` track (Task 6). Never merge two files
  into one track just because their tags resemble each other — merging requires evidence from the
  duplicate pipeline (Tasks 7-10) or an explicit human `MERGE` decision (Task 11).
- Explicit version/edition conflicts (`Original Mix` vs `X Remix`, `Radio Edit` vs `Extended Mix`,
  `Live` vs `Studio`, `Instrumental` vs `Vocal`, `Bootleg` vs `Original`, `X Remix` vs `Y Remix`) are
  strong negative evidence. If audio evidence is unexpectedly similar while metadata conflicts like
  this, the pairwise classification is `CONFLICT`, and the group status is `REVIEW_REQUIRED` — never
  auto-merge on audio similarity alone against conflicting explicit metadata.
- Group construction is not naive transitive closure: if pairwise evidence is inconsistent across a
  candidate group (e.g. A-B `AUDIO_EQUIVALENT`, B-C `PROBABLE`, A-C `DIFFERENT`), the group becomes
  `REVIEW_REQUIRED`, not auto-confirmed.
- Expensive evidence (BLAKE3 binary hash, Chromaprint fingerprint, deep quality analysis) is
  computed only for duplicate-candidate groups produced by conservative metadata blocking — never
  run library-wide by default. A plain `djlib scan` must never trigger hashing, fingerprinting, or
  quality analysis.
- Cache derived evidence keyed on `(size_bytes, mtime_ns, analyzer_version)`; invalidate and
  recompute only when one of those changes, to keep repeated `duplicates analyze` calls idempotent.
- Every automatic classification and preferred-file choice must store its evidence/reasons so it is
  explainable later via `catalog inspect` — never leave a bare confidence number with no rationale.
- Preferred-file selection priority is: integrity → intrinsic audio quality → absence of suspicious
  transcode → genuine lossless over lossy → useful technical resolution → absence of
  clipping/anomalies → metadata completeness → historical value only as a last tiebreaker. Play
  history must never let a technically inferior file win over a clearly superior master.
- No file is ever deleted or moved as a consequence of duplicate detection or preferred-file
  selection (see `source-read-only.md`).
