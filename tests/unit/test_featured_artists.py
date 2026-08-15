from djlib.resolve.parser import split_featured_artists


def test_feat_dot_marker() -> None:
    result = split_featured_artists('Artist A feat. Artist B')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_feat_without_trailing_dot() -> None:
    result = split_featured_artists('Artist A feat Artist B')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_ft_dot_marker() -> None:
    result = split_featured_artists('Artist A ft. Artist B')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_featuring_marker() -> None:
    result = split_featured_artists('Artist A featuring Artist B')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_ordered_multiple_featured_artists_with_ampersand() -> None:
    result = split_featured_artists('Artist A feat. Artist B & Artist C')
    assert result.featured_artists == ('Artist B', 'Artist C')


def test_ordered_multiple_featured_artists_with_comma() -> None:
    result = split_featured_artists('Artist A feat. Artist B, Artist C')
    assert result.featured_artists[0] == 'Artist B'
    assert result.featured_artists[1] == 'Artist C'


def test_position_zero_is_first_featured_artist() -> None:
    result = split_featured_artists('Artist A feat. Artist B, Artist C, Artist D')
    assert result.featured_artists[0] == 'Artist B'
    assert result.featured_artists[1] == 'Artist C'
    assert result.featured_artists[2] == 'Artist D'


def test_no_marker_returns_full_text_as_primary_with_no_featured_artists() -> None:
    result = split_featured_artists('Artist A & Artist B')
    assert result.primary == 'Artist A & Artist B'
    assert result.featured_artists == ()


def test_marker_inside_parentheses() -> None:
    result = split_featured_artists('Artist A (feat. Artist B)')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_marker_is_case_insensitive() -> None:
    result = split_featured_artists('Artist A FEAT. Artist B')
    assert result.primary == 'Artist A'
    assert result.featured_artists == ('Artist B',)


def test_does_not_split_word_containing_marker_substring() -> None:
    result = split_featured_artists('Software Artist')
    assert result.primary == 'Software Artist'
    assert result.featured_artists == ()


def test_does_not_split_word_starting_with_marker_substring() -> None:
    # "feat"/"ft" must not match as a mere prefix of a longer word: the leading \b is
    # satisfied at the start of "Feathers"/"Features" too, so the marker needs a trailing
    # boundary check to avoid corrupting a real artist/title containing these words.
    result = split_featured_artists('Feathers')
    assert result.primary == 'Feathers'
    assert result.featured_artists == ()

    result = split_featured_artists('Great Features Collective')
    assert result.primary == 'Great Features Collective'
    assert result.featured_artists == ()
