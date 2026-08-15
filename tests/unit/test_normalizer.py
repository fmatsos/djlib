from djlib.resolve.normalizer import normalize_identity


def test_unicode_nfkc_canonicalization() -> None:
    assert normalize_identity('ﬁlter') == normalize_identity('filter')


def test_casefold_ignores_case() -> None:
    assert normalize_identity('ARTIST NAME') == normalize_identity('artist name')


def test_typographic_dash_normalization() -> None:
    assert normalize_identity('Artist – Title') == normalize_identity('Artist - Title')


def test_typographic_em_dash_normalization() -> None:
    assert normalize_identity('Artist — Title') == normalize_identity('Artist - Title')


def test_typographic_apostrophe_normalization() -> None:
    assert normalize_identity('Don’t Stop') == normalize_identity("Don't Stop")


def test_typographic_quote_normalization() -> None:
    assert normalize_identity('“Title”') == normalize_identity('"Title"')


def test_whitespace_collapse_and_trim() -> None:
    assert normalize_identity('  Artist   Name  ') == 'artist name'


def test_tab_and_newline_collapse_as_whitespace() -> None:
    assert normalize_identity('Artist\tName\n') == 'artist name'


def test_retains_ampersand() -> None:
    assert '&' in normalize_identity('Artist A & Artist B')


def test_retains_plus() -> None:
    assert '+' in normalize_identity('Artist A + Artist B')


def test_retains_vs_marker() -> None:
    assert 'vs' in normalize_identity('Artist A vs. Artist B')


def test_retains_pres_marker() -> None:
    assert 'pres' in normalize_identity('Someone pres. Artist B')


def test_ampersand_makes_duo_distinguishable_from_either_member_alone() -> None:
    duo = normalize_identity('Artist A & Artist B')
    solo = normalize_identity('Artist A')
    assert duo != solo


def test_idempotent_on_already_normalized_input() -> None:
    once = normalize_identity('Artist A & Artist B')
    twice = normalize_identity(once)
    assert once == twice
