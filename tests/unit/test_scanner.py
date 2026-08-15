from pathlib import Path

from djlib.scan.scanner import discover_audio_files


def test_discovers_only_supported_audio(tmp_path: Path) -> None:
    (tmp_path / 'a.mp3').write_bytes(b'x')
    (tmp_path / 'b.flac').write_bytes(b'x')
    (tmp_path / 'cover.jpg').write_bytes(b'x')
    assert [x.relative_path for x in discover_audio_files(tmp_path)] == ['a.mp3', 'b.flac']
