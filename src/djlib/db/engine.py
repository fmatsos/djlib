from sqlalchemy import Engine, create_engine, event

from djlib.config import DjlibConfig


def create_engine_for_config(config: DjlibConfig) -> Engine:
    config.data_root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(config.database_url, future=True)

    @event.listens_for(engine, 'connect')
    def configure(dbapi_connection, _) -> None:
        cur = dbapi_connection.cursor()
        cur.execute('PRAGMA foreign_keys = ON')
        cur.execute('PRAGMA journal_mode = WAL')
        cur.execute('PRAGMA busy_timeout = 5000')
        cur.close()

    return engine
