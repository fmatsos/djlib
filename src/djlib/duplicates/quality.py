import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import object_session

from djlib.db.enums import AnalysisStatus, TranscodeSuspicion
from djlib.db.models import FileQualityAnalysis, FileRecord
from djlib.metadata.types import CommandRunner

ANALYZER_VERSION = '1'

_LOSSLESS_CODEC_NAMES = frozenset({'flac', 'alac', 'wavpack', 'ape', 'tta'})
_LOSSY_CODEC_NAMES = frozenset(
    {'mp3', 'aac', 'vorbis', 'opus', 'ac3', 'eac3', 'wmav1', 'wmav2', 'mp2'}
)

_METADATA_FIELDS = ('title_raw', 'artist_raw', 'album_raw', 'genre_raw', 'bpm_raw', 'key_raw')

_CD_SAMPLE_RATE = 44_100
_CD_BIT_DEPTH = 16
_REFERENCE_LOSSY_BITRATE_KBPS = 320.0
# A lossy stream, however clean, never reaches the lossless ceiling (design §21
# priority 4, "lossless over lossy when genuinely lossless") -- 90 is the most
# a >=320kbps lossy encode can score, deliberately short of the 100 a full
# lossless file gets at CD quality or better.
_LOSSY_SCORE_CEILING = 90.0

# astats' "Peak level dB"/"Abs Peak count" fields are the concrete evidence
# design §20 calls "a Peak level near 0 dBFS sustained across many samples":
# a single natural peak sample near full scale is normal, but a peak level at
# (essentially) 0 dBFS reached by a large fraction of all samples means the
# waveform was flattened against the ceiling. Calibrated empirically (see
# quality.py's module docstring companion in the Task 9 report) against a
# hard-clipped 1kHz tone (peak 0.000265 dB, ~20% of samples at the ceiling)
# versus an unclipped full-scale white-noise fixture (same peak dB, but only
# ~2% of samples at the ceiling) and a normal, non-full-scale sine tone
# (peak -18 dB): the dB threshold alone is not sufficient (full-scale but
# unclipped content also touches 0 dBFS), so both conditions are required.
_CLIP_PEAK_DB_THRESHOLD = -0.5
_CLIP_ABS_PEAK_RATIO_THRESHOLD = 0.10

# Spectral-cutoff transcode-suspicion heuristic (design §20: "lossy-to-lossless
# transcode suspicion... an heuristic indication, not a definitive provenance
# claim" -- NEVER treat this as proof a file was transcoded, only as evidence
# worth a human's attention).
#
# The technique: a lossy encoder applies its own lowpass filter (LAME's
# psychoacoustic model rolls off high frequencies harder as bitrate drops), so
# audio that passed through a lossy codec and was then written back to a
# lossless container has abnormally little energy in a high-frequency band
# relative to a safely-in-passband low/mid reference band. Genuine full-
# spectrum lossless audio does not show that gap.
#
# `_SPECTRAL_HIGH_CUTOFF_HZ` is the "candidate cutoff band": everything above
# it, isolated with ffmpeg's `highpass` filter. `_SPECTRAL_REFERENCE_BAND` is a
# low/mid band every codec at every practical bitrate reproduces faithfully,
# used as the "how loud is this signal supposed to be" baseline. The heuristic
# is `attenuation_db = reference_rms_db - high_band_rms_db`: how many dB
# quieter the high band is than the reference band.
#
# Calibrated empirically with real ffmpeg/libmp3lame encodes of a full-
# spectrum white-noise fixture (see tests/unit/test_quality.py and the Task 9
# report for the exact numbers):
#   direct-to-FLAC (no lossy pass):           attenuation ~4.0 dB
#   MP3 320/128/96/80kbps -> FLAC round-trip:  attenuation ~5.8-6.2 dB
#   MP3 64kbps -> FLAC round-trip:             attenuation ~16.4 dB
#   MP3 56/48/32kbps -> FLAC round-trip:       attenuation ~22-41 dB
# Modern LAME keeps content up to ~19-20kHz down to roughly 80kbps, so this
# heuristic only fires for real, harder cutoffs (<=64kbps-class encodes) --
# it is deliberately conservative rather than flagging every lossy pass.
_SPECTRAL_REFERENCE_BAND_HZ = (200, 5_000)
_SPECTRAL_HIGH_CUTOFF_HZ = 19_000
# Below this, the file's own Nyquist frequency is too close to (or below) the
# candidate cutoff band to say anything meaningful -- skip the check rather
# than manufacture a false HIGH suspicion for a legitimately low-sample-rate
# lossless master.
_MIN_SAMPLE_RATE_FOR_SPECTRAL_CHECK_HZ = 40_000

_SUSPICION_NONE_MAX_DB = 8.0
_SUSPICION_LOW_MAX_DB = 12.0
_SUSPICION_MEDIUM_MAX_DB = 16.0

_TRANSCODE_PENALTY = {
    TranscodeSuspicion.NONE: 0.0,
    TranscodeSuspicion.LOW: 8.0,
    TranscodeSuspicion.MEDIUM: 25.0,
    TranscodeSuspicion.HIGH: 55.0,
}
_CLIPPING_PENALTY = 8.0
# Metadata completeness is a tiebreaker (design §21 priority 7, last before
# provenance): capped low enough that it can never close a real audio-quality
# gap, only decide between two files that are otherwise equivalent.
_METADATA_BONUS_MAX = 3.0

_ASTATS_LINE = re.compile(r'^\[Parsed_astats[^\]]*\]\s*(?P<key>[^:]+):\s*(?P<value>.+)$')


class QualityAnalysisError(Exception):
    """Quality analysis could not be performed at all for a file (e.g. it is missing)."""


@dataclass(frozen=True)
class QualityResult:
    """Composed quality signals for one file (design §20).

    Deliberately more than one number: `integrity_ok`, `lossless`, and
    `transcode_suspicion` are kept as separate fields (not folded into
    `quality_score`) so a later caller (Task 10's `PreferredFileSelector`) can
    check "is this file even viable" and "is it genuinely lossless" and "is it
    under transcode suspicion" *before* looking at the single composite --
    exactly the priority order design §21 describes (integrity, then
    intrinsic audio quality, then absence of suspicious transcode, then
    lossless-over-lossy, ...). `quality_score` is provided as the single
    ordering key for cases that don't need that dimension-by-dimension
    reasoning, per this task's own composite formula (see `_composite_score`).
    """

    integrity_ok: bool
    lossless: bool
    transcode_suspicion: TranscodeSuspicion
    clipping_detected: bool
    audio_quality_score: float
    metadata_completeness: float
    quality_score: float
    details: dict[str, Any]


def _is_lossless(file: FileRecord) -> bool:
    codec = (file.codec or '').lower()
    if codec.startswith('pcm_'):
        return True
    if codec in _LOSSLESS_CODEC_NAMES:
        return True
    if codec in _LOSSY_CODEC_NAMES:
        return False
    container = (file.container_format or '').lower()
    # No recognized codec name (e.g. extraction never populated it) -- fall
    # back to container hints, but never *claim* lossless without evidence:
    # an unrecognized codec defaults to the lossy (non-bonus) scoring path.
    return 'wav' in container or 'aiff' in container or 'flac' in container


def _metadata_completeness(file: FileRecord) -> float:
    filled = sum(1 for field in _METADATA_FIELDS if getattr(file, field))
    return filled / len(_METADATA_FIELDS)


def _resolution_score(file: FileRecord, lossless: bool) -> float:
    if lossless:
        sample_rate = file.sample_rate or _CD_SAMPLE_RATE
        bit_depth = file.bit_depth or _CD_BIT_DEPTH
        sample_ratio = min(1.0, sample_rate / _CD_SAMPLE_RATE)
        depth_ratio = min(1.0, bit_depth / _CD_BIT_DEPTH)
        # Bottlenecked by whichever dimension is weaker so a lossless file
        # downsampled/reduced below CD quality doesn't get full marks.
        return 100.0 * min(sample_ratio, depth_ratio)
    bitrate_kbps = (file.bitrate or 0) / 1000.0
    return min(_LOSSY_SCORE_CEILING, (bitrate_kbps / _REFERENCE_LOSSY_BITRATE_KBPS) * _LOSSY_SCORE_CEILING)


def _composite_score(
    resolution_score: float,
    transcode_suspicion: TranscodeSuspicion,
    clipping_detected: bool,
    metadata_completeness: float,
) -> float:
    """Plain, documented composite: start from resolution/bitrate, then apply
    penalties for transcode suspicion and clipping, then add a small,
    tie-break-only metadata bonus. Clamped to [0, 100].
    """
    score = resolution_score
    score -= _TRANSCODE_PENALTY[transcode_suspicion]
    if clipping_detected:
        score -= _CLIPPING_PENALTY
    score += metadata_completeness * _METADATA_BONUS_MAX
    return max(0.0, min(100.0, score))


def _parse_astats(stderr: str) -> dict[str, str]:
    stats: dict[str, str] = {}
    for line in stderr.splitlines():
        match = _ASTATS_LINE.match(line)
        if match:
            stats[match.group('key').strip()] = match.group('value').strip()
    return stats


def _safe_float(stats: dict[str, str], key: str) -> float | None:
    raw = stats.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _check_integrity(runner: CommandRunner, path: Path) -> tuple[bool, dict[str, Any]]:
    result = runner.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'])
    stderr = result.stderr.strip()
    ok = result.returncode == 0 and not stderr
    return ok, {'returncode': result.returncode, 'stderr': stderr}


def _run_astats(runner: CommandRunner, path: Path, audio_filter: str) -> dict[str, str]:
    result = runner.run(
        ['ffmpeg', '-hide_banner', '-v', 'info', '-i', str(path), '-af', audio_filter, '-f', 'null', '-']
    )
    return _parse_astats(result.stderr)


def _detect_clipping(stats: dict[str, str]) -> bool:
    peak_db = _safe_float(stats, 'Peak level dB')
    abs_peak_count = _safe_float(stats, 'Abs Peak count')
    num_samples = _safe_float(stats, 'Number of samples')
    if peak_db is None or abs_peak_count is None or not num_samples:
        return False
    return (
        peak_db > _CLIP_PEAK_DB_THRESHOLD
        and (abs_peak_count / num_samples) > _CLIP_ABS_PEAK_RATIO_THRESHOLD
    )


def _spectral_attenuation_db(runner: CommandRunner, path: Path) -> float | None:
    low, high = _SPECTRAL_REFERENCE_BAND_HZ
    reference_stats = _run_astats(runner, path, f'highpass=f={low},lowpass=f={high},astats')
    high_band_stats = _run_astats(runner, path, f'highpass=f={_SPECTRAL_HIGH_CUTOFF_HZ},astats')
    reference_db = _safe_float(reference_stats, 'RMS level dB')
    high_band_db = _safe_float(high_band_stats, 'RMS level dB')
    if reference_db is None or high_band_db is None:
        return None
    return reference_db - high_band_db


def _transcode_suspicion(sample_rate: int | None, attenuation_db: float | None) -> TranscodeSuspicion:
    if sample_rate is None or sample_rate < _MIN_SAMPLE_RATE_FOR_SPECTRAL_CHECK_HZ:
        return TranscodeSuspicion.NONE
    if attenuation_db is None:
        return TranscodeSuspicion.NONE
    if attenuation_db < _SUSPICION_NONE_MAX_DB:
        return TranscodeSuspicion.NONE
    if attenuation_db < _SUSPICION_LOW_MAX_DB:
        return TranscodeSuspicion.LOW
    if attenuation_db < _SUSPICION_MEDIUM_MAX_DB:
        return TranscodeSuspicion.MEDIUM
    return TranscodeSuspicion.HIGH


def _lossless_status_label(lossless: bool) -> str:
    return 'LOSSLESS' if lossless else 'LOSSY'


def _clipping_status_label(clipping_detected: bool) -> str:
    return 'CLIPPED' if clipping_detected else 'CLEAN'


class QualityAnalyzer:
    """Targeted technical quality analysis for duplicate-candidate files (design §20).

    Like `HashService`/`ChromaprintService`, this never runs library-wide --
    `analyze` is only ever meant to be called for files a caller has already
    identified as duplicate candidates (Task 10's `duplicates analyze`). A
    plain `djlib scan` must never call this.

    Unlike those two services, `FileQualityAnalysis` *is* versioned
    (`analyzer_version`): every successful analysis inserts a new row rather
    than updating one in place. There is still no dedicated
    `size_bytes_at_analysis`/`mtime_ns_at_analysis` column pair on
    `FileQualityAnalysis` (that would need a migration, out of scope for this
    task) -- so cache validity reuses the exact same `FileRecord.quality_status`
    STALE/CURRENT/PENDING/ERROR signal `ScanService` already flips to STALE on
    any content change (see `scan/service.py::_mark_analysis_stale`), the same
    contract `HashService`/`ChromaprintService` rely on. `quality_status ==
    CURRENT` is necessary but not sufficient on its own: this service also
    confirms a `FileQualityAnalysis` row exists for the file at the *current*
    `ANALYZER_VERSION` before trusting it, so that a future analyzer-version
    bump can't be silently masked by a stale CURRENT flag.
    """

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def analyze(self, path: Path, file: FileRecord) -> QualityResult:
        session = object_session(file)

        if file.quality_status == AnalysisStatus.CURRENT:
            cached = _cached_result(session, file)
            if cached is not None:
                return cached

        if not path.exists():
            file.quality_status = AnalysisStatus.ERROR
            raise QualityAnalysisError(
                f'file not found for quality analysis: {file.relative_path}'
            )

        result, details = self._compute(path, file)

        if session is not None and file.id is not None:
            session.add(
                FileQualityAnalysis(
                    file_id=file.id,
                    analyzer_version=ANALYZER_VERSION,
                    integrity_status='OK' if result.integrity_ok else 'FAILED',
                    lossless_status=_lossless_status_label(result.lossless),
                    transcode_suspicion=result.transcode_suspicion,
                    clipping_status=_clipping_status_label(result.clipping_detected)
                    if result.integrity_ok
                    else 'UNKNOWN',
                    quality_score=result.quality_score,
                    details_json=details,
                )
            )
        file.quality_status = AnalysisStatus.CURRENT
        return result

    def _compute(self, path: Path, file: FileRecord) -> tuple[QualityResult, dict[str, Any]]:
        integrity_ok, integrity_details = _check_integrity(self._runner, path)
        lossless = _is_lossless(file)
        metadata_completeness = _metadata_completeness(file)

        base_details: dict[str, Any] = {
            'analyzer_version': ANALYZER_VERSION,
            'integrity': integrity_details,
            'codec': file.codec,
            'container_format': file.container_format,
            'bitrate': file.bitrate,
            'sample_rate': file.sample_rate,
            'bit_depth': file.bit_depth,
            'channels': file.channels,
            'metadata_completeness': metadata_completeness,
        }

        if not integrity_ok:
            base_details['skipped_reason'] = 'decode/integrity check failed; deeper measurements skipped'
            return (
                QualityResult(
                    integrity_ok=False,
                    lossless=lossless,
                    transcode_suspicion=TranscodeSuspicion.NONE,
                    clipping_detected=False,
                    audio_quality_score=0.0,
                    metadata_completeness=metadata_completeness,
                    quality_score=0.0,
                    details=base_details,
                ),
                base_details,
            )

        overall_stats = _run_astats(self._runner, path, 'astats')
        clipping_detected = _detect_clipping(overall_stats)

        if lossless:
            attenuation_db = _spectral_attenuation_db(self._runner, path)
            transcode_suspicion = _transcode_suspicion(file.sample_rate, attenuation_db)
        else:
            attenuation_db = None
            transcode_suspicion = TranscodeSuspicion.NONE

        resolution_score = _resolution_score(file, lossless)
        quality_score = _composite_score(
            resolution_score, transcode_suspicion, clipping_detected, metadata_completeness
        )

        base_details['astats_overall'] = overall_stats
        base_details['spectral_attenuation_db'] = attenuation_db
        base_details['audio_quality_score'] = resolution_score

        return (
            QualityResult(
                integrity_ok=True,
                lossless=lossless,
                transcode_suspicion=transcode_suspicion,
                clipping_detected=clipping_detected,
                audio_quality_score=resolution_score,
                metadata_completeness=metadata_completeness,
                quality_score=quality_score,
                details=base_details,
            ),
            base_details,
        )


def _cached_result(session: Any, file: FileRecord) -> QualityResult | None:
    if session is None or file.id is None:
        return None
    row = session.execute(
        select(FileQualityAnalysis)
        .where(
            FileQualityAnalysis.file_id == file.id,
            FileQualityAnalysis.analyzer_version == ANALYZER_VERSION,
        )
        .order_by(FileQualityAnalysis.id.desc())
    ).scalars().first()
    if row is None:
        return None
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
