from djlib.resolve.parser import parse_title_annotations


def test_remix_marker_with_remixer_name() -> None:
    parsed = parse_title_annotations('Meet Her At The Love Parade (Nalin & Kane Remix)')
    assert parsed.title == 'Meet Her At The Love Parade'
    assert parsed.version == 'Nalin & Kane Remix'
    assert parsed.edition is None


def test_generic_mix_marker() -> None:
    assert parse_title_annotations('Track (Mix)').version == 'Mix'


def test_original_mix_marker() -> None:
    assert parse_title_annotations('Track (Original Mix)').version == 'Original Mix'


def test_extended_mix_marker() -> None:
    assert parse_title_annotations('Track (Extended Mix)').version == 'Extended Mix'


def test_radio_edit_marker() -> None:
    assert parse_title_annotations('Track (Radio Edit)').version == 'Radio Edit'


def test_club_mix_marker() -> None:
    assert parse_title_annotations('Track (Club Mix)').version == 'Club Mix'


def test_edit_marker() -> None:
    assert parse_title_annotations('Track (Edit)').version == 'Edit'


def test_re_edit_marker() -> None:
    assert parse_title_annotations('Track (Re-Edit)').version == 'Re-Edit'


def test_rework_marker() -> None:
    assert parse_title_annotations('Track (Rework)').version == 'Rework'


def test_bootleg_marker() -> None:
    assert parse_title_annotations('Track (Bootleg)').version == 'Bootleg'


def test_mashup_marker() -> None:
    assert parse_title_annotations('Track (Mashup)').version == 'Mashup'


def test_vip_marker() -> None:
    assert parse_title_annotations('Track (VIP)').version == 'VIP'


def test_dub_marker() -> None:
    assert parse_title_annotations('Track (Dub)').version == 'Dub'


def test_instrumental_marker() -> None:
    assert parse_title_annotations('Track (Instrumental)').version == 'Instrumental'


def test_live_marker() -> None:
    assert parse_title_annotations('Track (Live)').version == 'Live'


def test_original_mix_and_remix_produce_different_version_values() -> None:
    original = parse_title_annotations('Track (Original Mix)')
    remix = parse_title_annotations('Track (Nalin & Kane Remix)')
    assert original.version is not None
    assert remix.version is not None
    assert original.version != remix.version


def test_radio_edit_and_extended_mix_stay_distinct() -> None:
    radio_edit = parse_title_annotations('Track (Radio Edit)')
    extended_mix = parse_title_annotations('Track (Extended Mix)')
    assert radio_edit.version is not None
    assert extended_mix.version is not None
    assert radio_edit.version != extended_mix.version


def test_two_different_remixers_stay_distinct() -> None:
    left = parse_title_annotations('Track (Nalin & Kane Remix)')
    right = parse_title_annotations('Track (Sasha Remix)')
    assert left.version != right.version


def test_remaster_marker_is_edition_not_version() -> None:
    parsed = parse_title_annotations('Track (Remaster)')
    assert parsed.edition == 'Remaster'
    assert parsed.version is None


def test_remastered_marker_is_edition() -> None:
    parsed = parse_title_annotations('Track (Remastered)')
    assert parsed.edition == 'Remastered'
    assert parsed.version is None


def test_anniversary_edition_marker() -> None:
    parsed = parse_title_annotations('Track (20th Anniversary Edition)')
    assert parsed.edition == '20th Anniversary Edition'
    assert parsed.version is None


def test_deluxe_edition_marker() -> None:
    parsed = parse_title_annotations('Track (Deluxe Edition)')
    assert parsed.edition == 'Deluxe Edition'
    assert parsed.version is None


def test_reissue_marker() -> None:
    parsed = parse_title_annotations('Track (Reissue)')
    assert parsed.edition == 'Reissue'
    assert parsed.version is None


def test_edition_and_version_never_collide() -> None:
    remaster = parse_title_annotations('Track (Remaster)')
    remix = parse_title_annotations('Track (Nalin & Kane Remix)')
    assert remaster.version is None
    assert remix.edition is None
    assert remaster.edition != remix.version


def test_unrecognized_parenthetical_is_left_in_title() -> None:
    parsed = parse_title_annotations('Track (Unknown Session)')
    assert parsed.version is None
    assert parsed.edition is None
    assert '(Unknown Session)' in parsed.title


def test_version_and_edition_both_present() -> None:
    parsed = parse_title_annotations('Track (Extended Mix) (Remaster)')
    assert parsed.version == 'Extended Mix'
    assert parsed.edition == 'Remaster'
    assert parsed.title == 'Track'


def test_title_without_annotations_is_unchanged() -> None:
    parsed = parse_title_annotations('Plain Title')
    assert parsed.title == 'Plain Title'
    assert parsed.version is None
    assert parsed.edition is None
