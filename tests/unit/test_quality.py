import random
import subprocess
from pathlib import Path

from djlib.db.enums import TranscodeSuspicion
from djlib.db.models import FileRecord
from djlib.duplicates.quality import QualityAnalyzer
from djlib.metadata.types import SubprocessCommandRunner


def _file_record(relative_path: str, **overrides: object) -> FileRecord:
    defaults: dict[str, object] = dict(
        public_id='fil_test',
        relative_path=relative_path,
        size_bytes=1,
        mtime_ns=1,
        extension=Path(relative_path).suffix,
    )
    defaults.update(overrides)
    return FileRecord(**defaults)  # type: ignore[arg-type]


def _encode_noise_wav(path: Path, seed: int, duration: float = 4.0) -> None:
    """A real, full-spectrum (white noise) PCM source -- no mocks."""
    subprocess.run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            '-ar', '44100', '-sample_fmt', 's16', str(path),
        ],
        check=True,
    )


def _encode(src: Path, dst: Path, *codec_args: str) -> None:
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(src), *codec_args, str(dst)], check=True)


def _corrupt_copy_of(source_mp3: Path, dest: Path, seed: int) -> None:
    """Scramble the middle third and truncate -- real ffmpeg then genuinely
    fails to decode this cleanly (confirmed manually: rc stays 0 but `ffmpeg
    -v error` emits "Header missing" / "Error submitting packet to decoder"
    to stderr, which is exactly the non-empty-stderr signal design §20/Task 9
    Step 3 says to treat as an integrity failure)."""
    data = bytearray(source_mp3.read_bytes())
    n = len(data)
    rnd = random.Random(seed)
    for i in range(n // 3, 2 * n // 3):
        data[i] = rnd.randint(0, 255)
    dest.write_bytes(bytes(data[: int(n * 0.5)]))


def test_corrupt_file_scores_lower_than_a_valid_file(tmp_path: Path) -> None:
    wav = tmp_path / 'source.wav'
    _encode_noise_wav(wav, seed=1)
    valid = tmp_path / 'valid.mp3'
    _encode(wav, valid, '-codec:a', 'libmp3lame', '-b:a', '192k')
    corrupt = tmp_path / 'corrupt.mp3'
    _corrupt_copy_of(valid, corrupt, seed=1)

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    valid_file = _file_record(
        'valid.mp3', codec='mp3', container_format='mp3', bitrate=192_000,
        sample_rate=44_100, channels=1, duration_ms=4000,
    )
    corrupt_file = _file_record(
        'corrupt.mp3', codec='mp3', container_format='mp3', bitrate=192_000,
        sample_rate=44_100, channels=1, duration_ms=4000,
    )

    valid_result = analyzer.analyze(valid, valid_file)
    corrupt_result = analyzer.analyze(corrupt, corrupt_file)

    assert valid_result.integrity_ok is True
    assert corrupt_result.integrity_ok is False
    assert corrupt_result.quality_score < valid_result.quality_score
    assert corrupt_result.quality_score == 0.0


def test_genuinely_lossless_flac_scores_higher_than_clean_mp3_320_for_equivalent_audio(
    tmp_path: Path,
) -> None:
    wav = tmp_path / 'source.wav'
    _encode_noise_wav(wav, seed=2)
    flac_path = tmp_path / 'track.flac'
    mp3_path = tmp_path / 'track.mp3'
    _encode(wav, flac_path, '-codec:a', 'flac')
    _encode(wav, mp3_path, '-codec:a', 'libmp3lame', '-b:a', '320k')

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    flac_file = _file_record(
        'track.flac', codec='flac', container_format='flac',
        sample_rate=44_100, bit_depth=16, channels=1, duration_ms=4000,
    )
    mp3_file = _file_record(
        'track.mp3', codec='mp3', container_format='mp3', bitrate=320_000,
        sample_rate=44_100, channels=1, duration_ms=4000,
    )

    flac_result = analyzer.analyze(flac_path, flac_file)
    mp3_result = analyzer.analyze(mp3_path, mp3_file)

    assert flac_result.integrity_ok is True
    assert mp3_result.integrity_ok is True
    assert flac_result.lossless is True
    assert mp3_result.lossless is False
    assert flac_result.quality_score > mp3_result.quality_score


def test_high_transcode_suspicion_strongly_penalizes_a_nominally_lossless_file(
    tmp_path: Path,
) -> None:
    wav = tmp_path / 'source.wav'
    _encode_noise_wav(wav, seed=3)

    genuine_flac = tmp_path / 'genuine.flac'
    _encode(wav, genuine_flac, '-codec:a', 'flac')

    # A real lossy pass at a bitrate low enough for LAME's own lowpass filter
    # to bite (see quality.py's module-level calibration comment: modern LAME
    # keeps near-full bandwidth down to ~80kbps, so 64kbps is used here as the
    # concrete "genuinely lossy source" case), then decoded back to lossless.
    lossy_intermediate = tmp_path / 'intermediate.mp3'
    _encode(wav, lossy_intermediate, '-codec:a', 'libmp3lame', '-b:a', '64k')
    suspicious_flac = tmp_path / 'suspicious.flac'
    _encode(lossy_intermediate, suspicious_flac, '-codec:a', 'flac')

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    genuine_file = _file_record(
        'genuine.flac', codec='flac', container_format='flac',
        sample_rate=44_100, bit_depth=16, channels=1, duration_ms=4000,
    )
    suspicious_file = _file_record(
        'suspicious.flac', codec='flac', container_format='flac',
        sample_rate=44_100, bit_depth=16, channels=1, duration_ms=4000,
    )

    genuine_result = analyzer.analyze(genuine_flac, genuine_file)
    suspicious_result = analyzer.analyze(suspicious_flac, suspicious_file)

    assert genuine_result.transcode_suspicion == TranscodeSuspicion.NONE
    assert suspicious_result.transcode_suspicion == TranscodeSuspicion.HIGH
    # "Strongly penalizes": not just lower, but a large, deliberate gap (see
    # _TRANSCODE_PENALTY[HIGH] == 55 points out of 100).
    assert suspicious_result.quality_score < genuine_result.quality_score - 30


def test_metadata_completeness_breaks_close_ties_but_cannot_offset_a_large_audio_deficit(
    tmp_path: Path,
) -> None:
    wav = tmp_path / 'tie_source.wav'
    _encode_noise_wav(wav, seed=4)
    tie_a = tmp_path / 'tie_a.mp3'
    _encode(wav, tie_a, '-codec:a', 'libmp3lame', '-b:a', '192k')
    tie_b = tmp_path / 'tie_b.mp3'
    tie_b.write_bytes(tie_a.read_bytes())

    analyzer = QualityAnalyzer(SubprocessCommandRunner())
    tag_kwargs: dict[str, object] = dict(
        title_raw='Title', artist_raw='Artist', album_raw='Album',
        genre_raw='Genre', bpm_raw='128', key_raw='Am',
    )
    tagged = _file_record(
        'tie_a.mp3', codec='mp3', container_format='mp3', bitrate=192_000,
        sample_rate=44_100, channels=1, duration_ms=4000, **tag_kwargs,
    )
    untagged = _file_record(
        'tie_b.mp3', codec='mp3', container_format='mp3', bitrate=192_000,
        sample_rate=44_100, channels=1, duration_ms=4000,
    )

    tagged_result = analyzer.analyze(tie_a, tagged)
    untagged_result = analyzer.analyze(tie_b, untagged)

    assert tagged_result.audio_quality_score == untagged_result.audio_quality_score
    assert tagged_result.quality_score > untagged_result.quality_score
    tie_margin = tagged_result.quality_score - untagged_result.quality_score

    # Same underlying (poor-quality) source, once encoded very low-bitrate
    # (file A, fully tagged) and once kept lossless (file B, no tags at all).
    poor_source = tmp_path / 'poor_source.wav'
    _encode_noise_wav(poor_source, seed=5)
    poor_audio = tmp_path / 'poor.mp3'
    _encode(poor_source, poor_audio, '-codec:a', 'libmp3lame', '-b:a', '48k')
    excellent_audio = tmp_path / 'excellent.flac'
    _encode(poor_source, excellent_audio, '-codec:a', 'flac')

    poor_quality_fully_tagged = _file_record(
        'poor.mp3', codec='mp3', container_format='mp3', bitrate=48_000,
        sample_rate=44_100, channels=1, duration_ms=4000, **tag_kwargs,
    )
    excellent_quality_untagged = _file_record(
        'excellent.flac', codec='flac', container_format='flac',
        sample_rate=44_100, bit_depth=16, channels=1, duration_ms=4000,
    )

    poor_result = analyzer.analyze(poor_audio, poor_quality_fully_tagged)
    excellent_result = analyzer.analyze(excellent_audio, excellent_quality_untagged)

    assert excellent_result.quality_score > poor_result.quality_score
    audio_gap = excellent_result.quality_score - poor_result.quality_score
    # The metadata-only tie margin above must be far smaller than a genuine
    # audio-quality gap -- complete tags on the poor file could never have
    # closed this gap (max possible metadata swing is _METADATA_BONUS_MAX).
    assert tie_margin < audio_gap
