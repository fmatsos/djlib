from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from djlib.config import DjlibConfig
from djlib.db.enums import TranscodeSuspicion
from djlib.db.models import FileQualityAnalysis, FileRecord
from djlib.db.session import session_factory
from djlib.duplicates.rationale import preferred_choice_from_persisted


def _write_file(session: Session, relative_path: str) -> FileRecord:
    file = FileRecord(
        public_id=f'fil_{relative_path}',
        relative_path=relative_path,
        size_bytes=1,
        mtime_ns=1,
        extension='.flac',
    )
    session.add(file)
    session.flush()
    return file


def _write_quality_row(session: Session, file: FileRecord, quality_score: float) -> None:
    session.add(
        FileQualityAnalysis(
            file_id=file.id,
            analyzer_version='1',
            integrity_status='OK',
            lossless_status='LOSSLESS',
            transcode_suspicion=TranscodeSuspicion.NONE,
            clipping_status='CLEAN',
            quality_score=quality_score,
            details_json={'audio_quality_score': quality_score, 'metadata_completeness': 1.0},
        )
    )


def test_returns_none_with_no_persisted_quality_rows(config: DjlibConfig, engine: Engine) -> None:
    session_maker = session_factory(engine)
    with session_maker() as session:
        file = _write_file(session, 'a.flac')
        session.commit()
        choice = preferred_choice_from_persisted(session, [file])
    assert choice is None


def test_chooses_higher_quality_score_from_persisted_rows(
    config: DjlibConfig, engine: Engine
) -> None:
    session_maker = session_factory(engine)
    with session_maker() as session:
        low = _write_file(session, 'low.mp3')
        high = _write_file(session, 'high.flac')
        _write_quality_row(session, low, quality_score=40.0)
        _write_quality_row(session, high, quality_score=95.0)
        session.commit()

        choice = preferred_choice_from_persisted(session, [low, high])
        assert choice is not None
        assert choice.file_id == high.id
        assert choice.reasons
