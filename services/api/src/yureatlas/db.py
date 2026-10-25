from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_size=3, max_overflow=2, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction():
    with Session.begin() as session:
        yield session
        # Flush domain writes before taking the short control-row locks. The
        # final guard holds worker then job ownership through the actual commit.
        # Import lazily because mapped tables depend on Base from this module.
        from .execution import verify_execution

        session.flush()
        verify_execution(session)
