import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import relationship as orm_relationship

from djlib.db.base import Base
from djlib.db.enums import (
    AnalysisStatus,
    DecisionAction,
    DecisionSource,
    DuplicateStatus,
    IdentityEventType,
    PairClassification,
    RelationshipType,
    ScanStatus,
    TrackStatus,
    TranscodeSuspicion,
)


class FileRecord(Base):
    __tablename__ = 'files'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    relative_path: Mapped[str] = mapped_column(String, unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    extension: Mapped[str] = mapped_column(String)
    container_format: Mapped[str | None] = mapped_column(String)
    codec: Mapped[str | None] = mapped_column(String)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    title_raw: Mapped[str | None] = mapped_column(String)
    artist_raw: Mapped[str | None] = mapped_column(String)
    album_raw: Mapped[str | None] = mapped_column(String)
    album_artist_raw: Mapped[str | None] = mapped_column(String)
    genre_raw: Mapped[str | None] = mapped_column(String)
    bpm_raw: Mapped[str | None] = mapped_column(String)
    key_raw: Mapped[str | None] = mapped_column(String)
    comment_raw: Mapped[str | None] = mapped_column(Text)
    raw_metadata_json: Mapped[dict | None] = mapped_column(JSON)

    resolved_artist: Mapped[str | None] = mapped_column(String)
    resolved_title: Mapped[str | None] = mapped_column(String)
    resolved_version: Mapped[str | None] = mapped_column(String)
    resolved_edition: Mapped[str | None] = mapped_column(String)
    artist_source: Mapped[str | None] = mapped_column(String)
    title_source: Mapped[str | None] = mapped_column(String)
    version_source: Mapped[str | None] = mapped_column(String)
    edition_source: Mapped[str | None] = mapped_column(String)

    binary_hash: Mapped[str | None] = mapped_column(String)
    binary_hash_status: Mapped[AnalysisStatus] = mapped_column(
        SAEnum(AnalysisStatus, native_enum=False), default=AnalysisStatus.PENDING
    )
    chromaprint: Mapped[str | None] = mapped_column(Text)
    chromaprint_duration_ms: Mapped[int | None] = mapped_column(Integer)
    chromaprint_status: Mapped[AnalysisStatus] = mapped_column(
        SAEnum(AnalysisStatus, native_enum=False), default=AnalysisStatus.PENDING
    )
    quality_status: Mapped[AnalysisStatus] = mapped_column(
        SAEnum(AnalysisStatus, native_enum=False), default=AnalysisStatus.PENDING
    )

    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    metadata_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    featured_artists: Mapped[list['FileFeaturedArtist']] = orm_relationship(
        back_populates='file'
    )
    track_links: Mapped[list['TrackFile']] = orm_relationship(back_populates='file')


class FileFeaturedArtist(Base):
    __tablename__ = 'file_featured_artists'
    __table_args__ = (UniqueConstraint('file_id', 'position'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)

    file: Mapped['FileRecord'] = orm_relationship(back_populates='featured_artists')


class Track(Base):
    __tablename__ = 'tracks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[TrackStatus] = mapped_column(
        SAEnum(TrackStatus, native_enum=False), default=TrackStatus.PROVISIONAL
    )
    artist: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    edition: Mapped[str | None] = mapped_column(String)
    artist_normalized: Mapped[str | None] = mapped_column(String)
    title_normalized: Mapped[str | None] = mapped_column(String)
    version_normalized: Mapped[str | None] = mapped_column(String)
    edition_normalized: Mapped[str | None] = mapped_column(String)
    duration_reference_ms: Mapped[int | None] = mapped_column(Integer)
    preferred_file_id: Mapped[int | None] = mapped_column(ForeignKey('files.id'))
    identity_confidence: Mapped[float | None] = mapped_column(Float)
    merged_into_track_id: Mapped[int | None] = mapped_column(ForeignKey('tracks.id'))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    featured_artists: Mapped[list['TrackFeaturedArtist']] = orm_relationship(
        back_populates='track'
    )
    file_links: Mapped[list['TrackFile']] = orm_relationship(back_populates='track')


class TrackFeaturedArtist(Base):
    __tablename__ = 'track_featured_artists'
    __table_args__ = (UniqueConstraint('track_id', 'position'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey('tracks.id'), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)

    track: Mapped['Track'] = orm_relationship(back_populates='featured_artists')


class TrackFile(Base):
    __tablename__ = 'track_files'
    __table_args__ = (
        Index(
            'uq_track_files_one_active_per_file',
            'file_id',
            unique=True,
            sqlite_where=text('is_active = 1'),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey('tracks.id'), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    relationship: Mapped[RelationshipType] = mapped_column(
        SAEnum(RelationshipType, native_enum=False)
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    decision_source: Mapped[DecisionSource] = mapped_column(
        SAEnum(DecisionSource, native_enum=False), default=DecisionSource.AUTOMATIC
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    track: Mapped['Track'] = orm_relationship(back_populates='file_links')
    file: Mapped['FileRecord'] = orm_relationship(back_populates='track_links')


class TrackOverride(Base):
    __tablename__ = 'track_overrides'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey('tracks.id'), index=True)
    field: Mapped[str] = mapped_column(String)
    value_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime)


class TrackIdentityEvent(Base):
    __tablename__ = 'track_identity_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uuid: Mapped[str] = mapped_column(String, unique=True, index=True)
    event_type: Mapped[IdentityEventType] = mapped_column(
        SAEnum(IdentityEventType, native_enum=False)
    )
    source_track_public_id: Mapped[str] = mapped_column(String, index=True)
    target_track_public_id: Mapped[str | None] = mapped_column(String, index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class ScanRun(Base):
    __tablename__ = 'scan_runs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    status: Mapped[ScanStatus] = mapped_column(SAEnum(ScanStatus, native_enum=False))
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    files_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    files_missing: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    scanner_version: Mapped[str] = mapped_column(String)
    error_summary: Mapped[str | None] = mapped_column(Text)


class DuplicateGroup(Base):
    __tablename__ = 'duplicate_groups'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[DuplicateStatus] = mapped_column(
        SAEnum(DuplicateStatus, native_enum=False), default=DuplicateStatus.DETECTED
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    proposed_preferred_file_id: Mapped[int | None] = mapped_column(ForeignKey('files.id'))
    matcher_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    members: Mapped[list['DuplicateGroupMember']] = orm_relationship(back_populates='group')


class DuplicateGroupMember(Base):
    __tablename__ = 'duplicate_group_members'
    __table_args__ = (UniqueConstraint('group_id', 'file_id'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('duplicate_groups.id'), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped['DuplicateGroup'] = orm_relationship(back_populates='members')


class DuplicatePairEvidence(Base):
    __tablename__ = 'duplicate_pair_evidence'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('duplicate_groups.id'), index=True)
    left_file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    right_file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    metadata_similarity: Mapped[float | None] = mapped_column(Float)
    artist_similarity: Mapped[float | None] = mapped_column(Float)
    title_similarity: Mapped[float | None] = mapped_column(Float)
    version_compatibility: Mapped[str | None] = mapped_column(String)
    edition_compatibility: Mapped[str | None] = mapped_column(String)
    featured_artist_similarity: Mapped[float | None] = mapped_column(Float)
    duration_delta_ms: Mapped[int | None] = mapped_column(Integer)
    binary_hash_equal: Mapped[bool | None] = mapped_column(Boolean)
    chromaprint_similarity: Mapped[float | None] = mapped_column(Float)
    classification: Mapped[PairClassification] = mapped_column(
        SAEnum(PairClassification, native_enum=False)
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class FileQualityAnalysis(Base):
    __tablename__ = 'file_quality_analyses'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey('files.id'), index=True)
    analyzer_version: Mapped[str] = mapped_column(String)
    integrity_status: Mapped[str] = mapped_column(String)
    lossless_status: Mapped[str] = mapped_column(String)
    transcode_suspicion: Mapped[TranscodeSuspicion] = mapped_column(
        SAEnum(TranscodeSuspicion, native_enum=False), default=TranscodeSuspicion.NONE
    )
    clipping_status: Mapped[str | None] = mapped_column(String)
    quality_score: Mapped[float | None] = mapped_column(Float)
    details_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class DuplicateDecision(Base):
    __tablename__ = 'duplicate_decisions'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('duplicate_groups.id'), index=True)
    decision: Mapped[DecisionAction] = mapped_column(SAEnum(DecisionAction, native_enum=False))
    preferred_file_id: Mapped[int | None] = mapped_column(ForeignKey('files.id'))
    report_id: Mapped[str] = mapped_column(String, index=True)
    catalog_revision: Mapped[str] = mapped_column(String)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    payload_json: Mapped[dict | None] = mapped_column(JSON)


class CurationEvent(Base):
    __tablename__ = 'curation_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    event_uuid: Mapped[str] = mapped_column(String, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String)
    track_public_id: Mapped[str | None] = mapped_column(String, index=True)
    file_public_id: Mapped[str | None] = mapped_column(String, index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class AppState(Base):
    __tablename__ = 'app_state'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_exported_curation_sequence: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
