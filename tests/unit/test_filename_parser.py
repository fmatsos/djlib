from djlib.resolve.filename import parse_filename


def test_parses_artist_dash_title() -> None:
    result = parse_filename('Emmanuel Top - Acid Phase.flac')
    assert result.artist == 'Emmanuel Top'
    assert result.title == 'Acid Phase'


def test_parses_artist_endash_title() -> None:
    result = parse_filename('Emmanuel Top – Acid Phase.flac')
    assert result.artist == 'Emmanuel Top'
    assert result.title == 'Acid Phase'


def test_parses_track_number_dash_prefix() -> None:
    result = parse_filename('01 - Emmanuel Top - Acid Phase.flac')
    assert result.artist == 'Emmanuel Top'
    assert result.title == 'Acid Phase'


def test_parses_track_number_dot_prefix() -> None:
    result = parse_filename('01. Emmanuel Top - Acid Phase.flac')
    assert result.artist == 'Emmanuel Top'
    assert result.title == 'Acid Phase'


def test_parses_title_with_version_annotation() -> None:
    result = parse_filename('Digitalism - Idealistic (Extended Mix).flac')
    assert result.artist == 'Digitalism'
    assert result.title == 'Idealistic'
    assert result.version == 'Extended Mix'


def test_ambiguous_filename_does_not_invent_metadata() -> None:
    result = parse_filename('Acid Track Final New 2.flac')
    assert result.artist is None
    assert result.title is None
    assert result.version is None
    assert result.edition is None


def test_filename_with_no_separator_never_invents_title() -> None:
    result = parse_filename('untitled_final_mixdown.wav')
    assert result.artist is None
    assert result.title is None


def test_filename_with_too_many_segments_is_treated_as_ambiguous() -> None:
    result = parse_filename('Artist - Album - Title.flac')
    assert result.artist is None
    assert result.title is None


def test_filename_with_empty_artist_segment_is_ambiguous() -> None:
    result = parse_filename(' - Title.flac')
    assert result.artist is None
    assert result.title is None
