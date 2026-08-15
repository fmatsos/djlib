from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True)
