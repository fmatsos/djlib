from enum import StrEnum


class ScanStatus(StrEnum):
    SUCCESS = 'SUCCESS'
    SUCCESS_WITH_ERRORS = 'SUCCESS_WITH_ERRORS'
    FAILED = 'FAILED'


class TrackStatus(StrEnum):
    PROVISIONAL = 'PROVISIONAL'
    ACTIVE = 'ACTIVE'
    MERGED = 'MERGED'
    SUPERSEDED = 'SUPERSEDED'


class AnalysisStatus(StrEnum):
    PENDING = 'PENDING'
    CURRENT = 'CURRENT'
    STALE = 'STALE'
    ERROR = 'ERROR'


class RelationshipType(StrEnum):
    PRIMARY = 'PRIMARY'
    EXACT_DUPLICATE = 'EXACT_DUPLICATE'
    AUDIO_EQUIVALENT = 'AUDIO_EQUIVALENT'
    PROBABLE = 'PROBABLE'


class DecisionSource(StrEnum):
    AUTOMATIC = 'AUTOMATIC'
    HUMAN = 'HUMAN'


class DuplicateStatus(StrEnum):
    DETECTED = 'DETECTED'
    AUTO_CONFIRMED = 'AUTO_CONFIRMED'
    REVIEW_REQUIRED = 'REVIEW_REQUIRED'
    CONFIRMED = 'CONFIRMED'
    REJECTED = 'REJECTED'
    DEFERRED = 'DEFERRED'


class PairClassification(StrEnum):
    EXACT = 'EXACT'
    AUDIO_EQUIVALENT = 'AUDIO_EQUIVALENT'
    PROBABLE = 'PROBABLE'
    DIFFERENT = 'DIFFERENT'
    CONFLICT = 'CONFLICT'


class TranscodeSuspicion(StrEnum):
    NONE = 'NONE'
    LOW = 'LOW'
    MEDIUM = 'MEDIUM'
    HIGH = 'HIGH'


class IdentityEventType(StrEnum):
    MERGE = 'MERGE'
    SPLIT = 'SPLIT'
    UNMERGE = 'UNMERGE'
    RESOLVE = 'RESOLVE'


class DecisionAction(StrEnum):
    CONFIRM = 'CONFIRM'
    CHANGE_PREFERRED = 'CHANGE_PREFERRED'
    REJECT = 'REJECT'
    DEFER = 'DEFER'


class RunStatus(StrEnum):
    """Outcome of one `OperationRun`-wrapped CLI invocation (Task 14).

    Deliberately separate from `ScanStatus`: `ScanRun` already has its own
    `files_failed` counter for "completed but some files failed" (its
    SUCCESS_WITH_ERRORS), a per-file-failure concept that doesn't generalize
    across `duplicates detect/analyze/run/report/import-decisions` -- for
    those, "completed" (SUCCESS) vs. "raised" (FAILED) is all `OperationRun`
    itself needs to know; anything richer lives in `summary_json` or the
    command's own dedicated tables.
    """

    SUCCESS = 'SUCCESS'
    FAILED = 'FAILED'
