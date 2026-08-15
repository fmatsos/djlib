import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from djlib.metadata.exiftool import ExifToolClient
from djlib.metadata.ffprobe import FfprobeClient
from djlib.metadata.types import MetadataExtractionError


class FakeCommandRunner:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = '') -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> CompletedProcess[str]:
        self.calls.append(list(argv))
        return CompletedProcess(
            args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


EXIFTOOL_MOCK_JSON = '[{"SourceFile":"x","Title":"Acid Phase","Artist":"Emmanuel Top","BPM":"145"}]'


def test_exiftool_extracts_title_artist_bpm() -> None:
    runner = FakeCommandRunner(stdout=EXIFTOOL_MOCK_JSON)
    client = ExifToolClient(runner)

    raw = client.extract_raw(Path('x'))

    assert raw.title == 'Acid Phase'
    assert raw.artist == 'Emmanuel Top'
    assert raw.bpm == '145'


def test_exiftool_preserves_complete_raw_json() -> None:
    runner = FakeCommandRunner(stdout=EXIFTOOL_MOCK_JSON)
    client = ExifToolClient(runner)

    raw = client.extract_raw(Path('x'))

    assert raw.raw_json == json.loads(EXIFTOOL_MOCK_JSON)[0]


def test_exiftool_invokes_expected_command() -> None:
    runner = FakeCommandRunner(stdout=EXIFTOOL_MOCK_JSON)
    client = ExifToolClient(runner)

    client.extract_raw(Path('/music/x.mp3'))

    assert runner.calls == [
        [
            'exiftool', '-j', '-n',
            '-Title', '-Artist', '-Album', '-AlbumArtist', '-Genre',
            '-BPM', '-InitialKey', '-Comment', '/music/x.mp3',
        ]
    ]


def test_exiftool_tolerates_stderr_warnings_with_valid_json() -> None:
    runner = FakeCommandRunner(
        stdout=EXIFTOOL_MOCK_JSON, returncode=0, stderr='Warning: something noisy'
    )
    client = ExifToolClient(runner)

    raw = client.extract_raw(Path('x'))

    assert raw.title == 'Acid Phase'


def test_exiftool_malformed_output_raises_extraction_error() -> None:
    runner = FakeCommandRunner(stdout='not json', returncode=0)
    client = ExifToolClient(runner)

    with pytest.raises(MetadataExtractionError):
        client.extract_raw(Path('x'))


def test_exiftool_empty_output_raises_extraction_error() -> None:
    runner = FakeCommandRunner(stdout='', returncode=1, stderr='No such file')
    client = ExifToolClient(runner)

    with pytest.raises(MetadataExtractionError):
        client.extract_raw(Path('x'))


FFPROBE_MOCK_JSON = json.dumps(
    {
        'streams': [
            {
                'codec_type': 'audio',
                'codec_name': 'flac',
                'sample_rate': '44100',
                'channels': 2,
                'bits_per_sample': 16,
                'duration': '401.250000',
            }
        ],
        'format': {
            'format_name': 'flac',
            'duration': '401.250000',
            'bit_rate': '1000000',
        },
    }
)


def test_ffprobe_normalizes_technical_metadata() -> None:
    runner = FakeCommandRunner(stdout=FFPROBE_MOCK_JSON)
    client = FfprobeClient(runner)

    technical = client.extract_technical(Path('x'))

    assert technical.codec == 'flac'
    assert technical.sample_rate == 44100
    assert technical.channels == 2
    assert technical.bit_depth == 16
    assert technical.duration_ms == 401250
    assert technical.container_format == 'flac'
    assert technical.bitrate == 1000000


def test_ffprobe_invokes_expected_command() -> None:
    runner = FakeCommandRunner(stdout=FFPROBE_MOCK_JSON)
    client = FfprobeClient(runner)

    client.extract_technical(Path('/music/x.flac'))

    assert runner.calls == [
        [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', '/music/x.flac',
        ]
    ]


def test_ffprobe_malformed_output_raises_extraction_error() -> None:
    runner = FakeCommandRunner(stdout='{', returncode=0)
    client = FfprobeClient(runner)

    with pytest.raises(MetadataExtractionError):
        client.extract_technical(Path('x'))


def test_ffprobe_no_audio_stream_raises_extraction_error() -> None:
    runner = FakeCommandRunner(
        stdout=json.dumps({'streams': [], 'format': {}}), returncode=1, stderr='Invalid data'
    )
    client = FfprobeClient(runner)

    with pytest.raises(MetadataExtractionError):
        client.extract_technical(Path('x'))
