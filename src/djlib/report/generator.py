"""Static duplicate-review HTML report generation (design §22, Task 12).

`ReportGenerator.generate()` is read-only from the database's perspective: it
only ever `SELECT`s. It never mutates a `DuplicateGroup`/`FileRecord`/
`FileQualityAnalysis`/etc. row, and it never invokes ffmpeg or any other
external tool -- all technical/quality evidence it displays was already
computed and persisted by `DuplicateService.analyze()` (Tasks 9-10). This
keeps report generation cheap and repeatable, and matches design §4's "no
direct database write path" for this component and `.claude/rules/
source-read-only.md`'s spirit (this module never even touches `music_root`).

Output layout (design §22):

    /data/reports/duplicates-review-YYYYMMDD-HHMMSS/
        index.html   -- the report; the manifest JSON is embedded inline so
                         the page works when opened directly via file://,
                         with no HTTP server and no database connection from
                         the browser.
        manifest.json -- the same data, also written as a sibling file (the
                         plan's literal file list requires both artifacts).

Design decisions made explicit here (see also the Task 12 report handed back
to the orchestrator):

* **Already-decided groups still appear.** `generate()` includes every
  `DuplicateGroup` row regardless of status, not just `REVIEW_REQUIRED` ones.
  This gives a fresh report full audit visibility (a human can see what was
  already `CONFIRMED`/`REJECTED`/`DEFERRED` in a prior import round, per
  design §24) and makes design §22's required "decision state" *filter* and
  "unresolved only" *toggle* meaningful (there would be nothing to filter
  otherwise). "Unresolved" is defined as `status == REVIEW_REQUIRED` -- the
  only status that is actually awaiting a human decision in this design;
  `DETECTED`/`AUTO_CONFIRMED` groups are still automatic-pipeline territory,
  not yet handed to a human.
* **Catalog revision formula** (`compute_catalog_revision`): combines (a) the
  most recent `ScanRun.public_id` (every persisted `ScanRun` row already
  represents a completed run -- see `scan/service.py`, there is no
  in-progress status), (b) a SHA-256 digest over the sorted set of
  `(group.public_id, group.status, group.confidence)` tuples -- a proxy for
  "current state of duplicate analysis" standing in for the not-yet-built
  `OperationRun` concept, and (c) `AppState.last_exported_curation_sequence`.
  Deterministic and sensitive to every input that matters for later
  staleness detection (Task 13): a changed scan, a changed/added/removed
  duplicate group, or a newly-exported curation sequence all change the
  revision string.
* **Proposed-preferred-file reasons are recomputed, not re-run.** `analyze()`
  only persists `DuplicateGroup.proposed_preferred_file_id`, not
  `PreferredChoice.reasons`. Rather than re-invoking `QualityAnalyzer`
  (which would call ffmpeg and insert new `FileQualityAnalysis` rows --
  exactly the DB write and filesystem access this module must not do), the
  generator reconstructs each file's latest persisted `FileQualityAnalysis`
  row into a `QualityResult` (mirroring `quality.py`'s own private
  `_cached_result` helper) and replays `PreferredFileSelector.choose()`
  against already-stored evidence only.
"""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService, EffectiveIdentity
from djlib.config import DjlibConfig
from djlib.db.enums import PairClassification
from djlib.db.models import (
    AppState,
    DuplicateGroup,
    DuplicateGroupMember,
    DuplicatePairEvidence,
    FileQualityAnalysis,
    FileRecord,
    ScanRun,
)
from djlib.duplicates.preferred import PreferredCandidate, PreferredChoice, PreferredFileSelector
from djlib.duplicates.quality import QualityResult
from djlib.ids import new_public_id

_TEMPLATE_DIR = Path(__file__).parent / 'templates'
_ASSETS_DIR = Path(__file__).parent / 'assets'
_METADATA_FIELDS = ('title_raw', 'artist_raw', 'album_raw', 'genre_raw', 'bpm_raw', 'key_raw')

# design §16 (`DuplicateGroupBuilder._draft`), mirrored read-only for display
# rather than re-run: a group's own status already IS the classification
# outcome, but a human reviewer also needs the *rationale* for that status,
# which was never itself persisted as a column on `DuplicateGroup`.
_INCONSISTENT = frozenset({PairClassification.DIFFERENT, PairClassification.CONFLICT})


@dataclass(frozen=True)
class ReportArtifact:
    report_id: str
    catalog_revision: str
    output_dir: Path
    index_path: Path
    manifest_path: Path
    group_count: int


def compute_catalog_revision(session: Session) -> str:
    """Deterministic revision string sensitive to every input Task 13 will
    need to detect staleness against. See the module docstring for the
    formula and why each ingredient was chosen.
    """
    latest_scan_public_id = session.execute(
        select(ScanRun.public_id).order_by(ScanRun.started_at.desc(), ScanRun.id.desc()).limit(1)
    ).scalar_one_or_none() or 'scan_none'

    group_rows = session.execute(
        select(DuplicateGroup.public_id, DuplicateGroup.status, DuplicateGroup.confidence)
    ).all()
    group_state = '|'.join(
        f'{public_id}:{status.value}:{confidence if confidence is not None else "-"}'
        for public_id, status, confidence in sorted(group_rows, key=lambda row: row[0])
    )

    app_state = session.execute(select(AppState).order_by(AppState.id).limit(1)).scalar_one_or_none()
    last_exported_sequence = app_state.last_exported_curation_sequence if app_state else 0

    digest_input = f'{latest_scan_public_id}\n{group_state}\n{last_exported_sequence}'
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    return f'rev_{digest[:24]}'


def _script_safe(json_text: str) -> str:
    """Defangs a literal `</` inside JSON text so it cannot prematurely close
    the `<script type="application/json">` block it is inlined into. `<` and
    `>` never occur in JSON *structure* (only inside string values), and
    `\\/` is a valid JSON escape for `/`, so this is a lossless, JSON-safe
    transform -- `JSON.parse` reads the result back identically.
    """
    return json_text.replace('</', '<\\/')


def _metadata_completeness(file: FileRecord) -> float:
    filled = sum(1 for field in _METADATA_FIELDS if getattr(file, field))
    return filled / len(_METADATA_FIELDS)


def _quality_result_from_row(row: FileQualityAnalysis) -> QualityResult:
    """Reconstructs a `QualityResult` from an already-persisted
    `FileQualityAnalysis` row -- mirrors `duplicates/quality.py`'s private
    `_cached_result` helper, kept as a small separate copy here rather than
    importing a private function across modules or widening `quality.py`'s
    public surface for a use case outside this task's scope.
    """
    details = row.details_json or {}
    return QualityResult(
        integrity_ok=row.integrity_status == 'OK',
        lossless=row.lossless_status == 'LOSSLESS',
        transcode_suspicion=row.transcode_suspicion,
        clipping_detected=row.clipping_status == 'CLIPPED',
        audio_quality_score=details.get('audio_quality_score', 0.0),
        metadata_completeness=details.get('metadata_completeness', 0.0),
        quality_score=row.quality_score if row.quality_score is not None else 0.0,
        details=details,
    )


def _quality_dict(row: FileQualityAnalysis | None) -> dict | None:
    if row is None:
        return None
    return {
        'analyzer_version': row.analyzer_version,
        'integrity_status': row.integrity_status,
        'lossless_status': row.lossless_status,
        'transcode_suspicion': row.transcode_suspicion.value,
        'clipping_status': row.clipping_status,
        'quality_score': row.quality_score,
    }


def _identity_dict(identity: EffectiveIdentity) -> dict:
    return {
        'artist': identity.artist,
        'title': identity.title,
        'version': identity.version,
        'edition': identity.edition,
        'featured_artists': [
            {'name': fa.name, 'source': fa.source} for fa in identity.featured_artists
        ],
    }


def _group_reasons(pair_rows: list[DuplicatePairEvidence]) -> list[str]:
    classifications = {row.classification for row in pair_rows}
    if classifications & _INCONSISTENT:
        return [
            'conflicting or contradictory pairwise evidence within this group '
            '(design §16: never rely on naive transitive closure)'
        ]
    if PairClassification.PROBABLE in classifications:
        return [
            'at least one PROBABLE pair -- plausible but not confident enough '
            'for automatic consolidation'
        ]
    if classifications:
        return ['every pairwise classification in this group is EXACT or AUDIO_EQUIVALENT']
    return ['no pairwise evidence available (fewer than two analyzed files)']


class ReportGenerator:
    def __init__(
        self,
        config: DjlibConfig,
        session: Session,
        clock: 'type[dt.datetime] | None' = None,
    ) -> None:
        self._config = config
        self._session = session
        self._now = (clock or dt.datetime).now(dt.UTC)
        self._catalog_service = CatalogService(session)
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(enabled_extensions=('j2',)),
        )

    def generate(self) -> ReportArtifact:
        report_id = new_public_id('rpt')
        catalog_revision = compute_catalog_revision(self._session)
        generated_at = self._now.isoformat()

        groups = list(
            self._session.execute(
                select(DuplicateGroup).order_by(DuplicateGroup.id)
            ).scalars()
        )
        group_entries = [self._build_group_entry(group) for group in groups]

        manifest = {
            'report_id': report_id,
            'catalog_revision': catalog_revision,
            'generated_at': generated_at,
            'groups': group_entries,
        }

        output_dir = (
            self._config.data_root
            / 'reports'
            / f'duplicates-review-{self._now:%Y%m%d-%H%M%S}'
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
        manifest_path = output_dir / 'manifest.json'
        manifest_path.write_text(manifest_json, encoding='utf-8')

        template = self._env.get_template('index.html.j2')
        app_js = (_ASSETS_DIR / 'app.js').read_text(encoding='utf-8')
        style_css = (_ASSETS_DIR / 'style.css').read_text(encoding='utf-8')
        html = template.render(
            # The manifest is embedded twice on purpose: `manifest.json` is
            # written byte-for-byte as its own sibling artifact (the plan's
            # literal file list requires both files), while the copy inlined
            # into `index.html` is defanged against a "</script>" substring
            # inside some string value (e.g. an unusual file path) breaking
            # out of its enclosing <script> element -- see `_script_safe`.
            manifest_json_escaped=_script_safe(manifest_json),
            app_js=app_js,
            style_css=style_css,
        )
        index_path = output_dir / 'index.html'
        index_path.write_text(html, encoding='utf-8')

        return ReportArtifact(
            report_id=report_id,
            catalog_revision=catalog_revision,
            output_dir=output_dir,
            index_path=index_path,
            manifest_path=manifest_path,
            group_count=len(groups),
        )

    def _build_group_entry(self, group: DuplicateGroup) -> dict:
        file_ids = list(
            self._session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group.id
                )
            ).scalars()
        )
        files = {
            f.id: f
            for f in self._session.execute(
                select(FileRecord).where(FileRecord.id.in_(file_ids))
            ).scalars()
        }
        pair_rows = list(
            self._session.execute(
                select(DuplicatePairEvidence).where(DuplicatePairEvidence.group_id == group.id)
            ).scalars()
        )

        preferred_choice = self._propose_preferred_choice(list(files.values()))
        proposed_preferred_public_id = (
            files[preferred_choice.file_id].public_id
            if preferred_choice is not None and preferred_choice.file_id in files
            else (
                files[group.proposed_preferred_file_id].public_id
                if group.proposed_preferred_file_id in files
                else None
            )
        )

        file_entries = [self._build_file_entry(files[fid]) for fid in sorted(files)]
        pair_entries = [self._build_pair_entry(row, files) for row in pair_rows]
        quality_scores = [
            entry['quality']['quality_score']
            for entry in file_entries
            if entry['quality'] is not None and entry['quality']['quality_score'] is not None
        ]
        quality_delta = (max(quality_scores) - min(quality_scores)) if quality_scores else None

        return {
            'group_id': group.public_id,
            'status': group.status.value,
            'confidence': group.confidence,
            'matcher_version': group.matcher_version,
            'created_at': group.created_at.isoformat() if group.created_at else None,
            'resolved_at': group.resolved_at.isoformat() if group.resolved_at else None,
            'proposed_preferred_file_id': proposed_preferred_public_id,
            'proposed_preferred_reasons': list(preferred_choice.reasons) if preferred_choice else [],
            'reasons': _group_reasons(pair_rows),
            'file_count': len(file_entries),
            'quality_delta': quality_delta,
            'sort_path': min((f.relative_path for f in files.values()), default=''),
            'files': file_entries,
            'pairs': pair_entries,
        }

    def _propose_preferred_choice(self, files: list[FileRecord]) -> PreferredChoice | None:
        candidates: list[PreferredCandidate] = []
        for file in files:
            row = self._latest_quality_row(file.id)
            if row is None:
                continue
            candidates.append(PreferredCandidate(file_id=file.id, quality=_quality_result_from_row(row)))
        if not candidates:
            return None
        return PreferredFileSelector().choose(candidates)

    def _latest_quality_row(self, file_id: int) -> FileQualityAnalysis | None:
        return self._session.execute(
            select(FileQualityAnalysis)
            .where(FileQualityAnalysis.file_id == file_id)
            .order_by(FileQualityAnalysis.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _build_file_entry(self, file: FileRecord) -> dict:
        quality_row = self._latest_quality_row(file.id)
        track = active_track_for_file(self._session, file.id)
        identity = self._catalog_service.effective_identity(track) if track is not None else None
        details = (quality_row.details_json or {}) if quality_row is not None else {}

        return {
            'file_id': file.public_id,
            'relative_path': file.relative_path,
            'size_bytes': file.size_bytes,
            'mtime_ns': file.mtime_ns,
            'extension': file.extension,
            'container_format': file.container_format,
            'codec': file.codec,
            'bitrate': file.bitrate,
            'sample_rate': file.sample_rate,
            'bit_depth': file.bit_depth,
            'channels': file.channels,
            'duration_ms': file.duration_ms,
            'metadata_completeness': details.get('metadata_completeness', _metadata_completeness(file)),
            'quality': _quality_dict(quality_row),
            'effective_identity': _identity_dict(identity) if identity is not None else None,
        }

    def _build_pair_entry(
        self, row: DuplicatePairEvidence, files: dict[int, FileRecord]
    ) -> dict:
        evidence = row.evidence_json or {}
        return {
            'left_file_id': files[row.left_file_id].public_id,
            'right_file_id': files[row.right_file_id].public_id,
            'classification': row.classification.value,
            'confidence': row.confidence,
            'metadata_similarity': row.metadata_similarity,
            'artist_similarity': row.artist_similarity,
            'title_similarity': row.title_similarity,
            'version_compatibility': row.version_compatibility,
            'edition_compatibility': row.edition_compatibility,
            'featured_artist_similarity': row.featured_artist_similarity,
            'duration_delta_ms': row.duration_delta_ms,
            'binary_hash_equal': row.binary_hash_equal,
            'chromaprint_similarity': row.chromaprint_similarity,
            'reasons': list(evidence.get('reasons', [])),
        }
