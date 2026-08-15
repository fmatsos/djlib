from sqlalchemy import Engine, text


def test_sqlite_pragmas(engine: Engine) -> None:
    with engine.connect() as c:
        assert c.execute(text('PRAGMA foreign_keys')).scalar_one() == 1
        assert c.execute(text('PRAGMA journal_mode')).scalar_one().lower() == 'wal'
        assert c.execute(text('PRAGMA busy_timeout')).scalar_one() > 0
