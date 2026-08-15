from dataclasses import dataclass

from djlib.db.enums import TranscodeSuspicion
from djlib.duplicates.quality import QualityResult

_SUSPICION_RANK = {
    TranscodeSuspicion.NONE: 0,
    TranscodeSuspicion.LOW: 1,
    TranscodeSuspicion.MEDIUM: 2,
    TranscodeSuspicion.HIGH: 3,
}


@dataclass(frozen=True)
class PreferredCandidate:
    """One file's quality evidence for preferred-file selection (design §21).

    `historical_value` is schema-compatible but unused this milestone -- no
    play-history data source exists yet (Milestone 2). It is only ever
    consulted as the final tiebreaker, strictly after every technical
    dimension ties exactly; a lopsided historical value can never override a
    real technical difference (design's explicit wording).
    """

    file_id: int
    quality: QualityResult
    historical_value: float | None = None


@dataclass(frozen=True)
class PreferredChoice:
    file_id: int
    reasons: tuple[str, ...]


def _compare(
    left: PreferredCandidate, right: PreferredCandidate
) -> tuple[PreferredCandidate, str]:
    if left.quality.integrity_ok != right.quality.integrity_ok:
        winner = left if left.quality.integrity_ok else right
        return winner, 'file integrity: the other candidate failed decode/integrity check'

    left_rank = _SUSPICION_RANK[left.quality.transcode_suspicion]
    right_rank = _SUSPICION_RANK[right.quality.transcode_suspicion]
    if left_rank != right_rank:
        winner = left if left_rank < right_rank else right
        return winner, f'lower transcode suspicion ({winner.quality.transcode_suspicion.value})'

    if left.quality.audio_quality_score != right.quality.audio_quality_score:
        winner = (
            left if left.quality.audio_quality_score > right.quality.audio_quality_score else right
        )
        return winner, (
            'higher intrinsic/technical audio quality score '
            f'({winner.quality.audio_quality_score:.1f})'
        )

    if left.quality.lossless != right.quality.lossless:
        winner = left if left.quality.lossless else right
        return winner, 'genuinely lossless beats lossy at an equal quality score'

    if left.quality.clipping_detected != right.quality.clipping_detected:
        winner = left if not left.quality.clipping_detected else right
        return winner, 'absence of detected clipping'

    if left.quality.metadata_completeness != right.quality.metadata_completeness:
        winner = (
            left
            if left.quality.metadata_completeness > right.quality.metadata_completeness
            else right
        )
        return winner, (
            f'more complete metadata ({winner.quality.metadata_completeness:.2f}) -- '
            'tiebreaker only, cannot offset an audio-quality difference'
        )

    if (
        left.historical_value is not None
        and right.historical_value is not None
        and left.historical_value != right.historical_value
    ):
        winner = left if left.historical_value > right.historical_value else right
        return winner, (
            f'historical value tiebreaker ({winner.historical_value}) -- last resort, only '
            'reached because every technical dimension tied exactly'
        )

    return left, 'fully tied on every dimension; kept the first candidate for a stable result'


class PreferredFileSelector:
    """Preferred-master selection as a strict priority comparison (design §21).

    Deliberately not one blended score: each dimension is compared in
    priority order and the *first* difference decides the winner, so a
    lower-priority dimension (metadata completeness, historical value) can
    never override a higher-priority one (integrity, transcode suspicion).

    Deviation from design §21's literal numbering, documented here
    explicitly: design lists "intrinsic audio quality" (priority 2) above
    "absence of suspicious transcode" (priority 3). Task 9's
    `QualityResult.audio_quality_score` is a *nominal* technical-resolution
    figure (sample rate/bit depth for lossless, bitrate for lossy) that does
    not itself account for transcode suspicion -- a HIGH-suspicion lossless
    file can carry the exact same nominal resolution as a genuine one.
    Comparing `audio_quality_score` before `transcode_suspicion` would
    therefore let a transcode-suspicious file's inflated nominal resolution
    outrank a clean file, directly contradicting design §21's own worked
    example ("a lossless file under HIGH transcode suspicion loses to a
    clean MP3 despite nominally being lossless"). This implementation checks
    `transcode_suspicion` before `audio_quality_score` instead, which is the
    only ordering that satisfies that example deterministically rather than
    by accident of fixture numbers. Design's priority 5 ("useful technical
    resolution") and priority 2 ("intrinsic audio quality") also collapse
    into the single `audio_quality_score` comparison below, since Task 9's
    `QualityResult` has only one such field -- there is no separate
    "intrinsic quality" signal distinct from measured resolution/bitrate in
    this codebase.
    """

    def choose(self, candidates: list[PreferredCandidate]) -> PreferredChoice:
        if not candidates:
            raise ValueError('choose() requires at least one candidate file')

        champion = candidates[0]
        reasons: list[str] = []
        for challenger in candidates[1:]:
            winner, reason = _compare(champion, challenger)
            reasons.append(reason)
            champion = winner

        if not reasons:
            reasons = ['only one file in the group']
        return PreferredChoice(file_id=champion.file_id, reasons=tuple(reasons))
