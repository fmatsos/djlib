from djlib.ids import new_public_id


def test_public_ids_are_prefixed_and_unique() -> None:
    a = new_public_id('trk')
    b = new_public_id('trk')
    assert a.startswith('trk_')
    assert a != b
