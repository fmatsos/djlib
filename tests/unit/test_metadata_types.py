import sys

from djlib.metadata.types import SubprocessCommandRunner


def test_run_survives_non_utf8_bytes_on_stderr() -> None:
    """Real-world ffmpeg output can contain non-UTF-8 bytes (e.g. a filename
    embedded in a foreign encoding), which must not crash a duplicate
    analysis run -- see the astats/integrity callers in duplicates/quality.py."""
    runner = SubprocessCommandRunner()

    result = runner.run(
        [
            sys.executable,
            '-c',
            'import sys; sys.stderr.buffer.write(bytes([0x41, 0xde, 0x42])); sys.stderr.buffer.flush()',
        ]
    )

    assert 'A' in result.stderr
    assert 'B' in result.stderr
