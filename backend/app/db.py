"""Database plumbing.

Sync SQLAlchemy only: ``create_engine`` + ``sessionmaker`` + sync ``Session``,
matching the existing sync FastAPI app. The engine is created LAZILY on first
use so unit tests and import-time code never touch the database.

One transaction per unit of work: ``get_db`` yields a session, the command
commits once at the end, and rolls back on any exception (BE spec §9).

Durability before acknowledgement (``EINV-11a``): ``get_db`` also *publishes*
its session on the request, and ``commit_before_response`` — declared once,
app-level, ``scope="function"`` in ``create_app`` — commits that same session
before the first response byte. A route-level dependency is torn down after the
response is sent, so without this a 2xx could acknowledge a write whose commit
then failed. See ``commit_before_response`` for the two rules it depends on.

Employee-fact serialization: every writer of one employee's facts (worktime,
OCD/overtime, shift, travel, disclosure, identity schedule) takes
``lock_employee_facts`` as its FIRST statement, before reading that employee's
current-version/overlap state. One key per employee — never a per-operation or
per-table key — is what makes the currentness, overlap and ``max(version)+1``
read-then-write sequences mutually exclusive. Re-acquiring the same key inside
one transaction is free, so a command may safely call a locking sub-command.
"""

import re
import uuid
from collections.abc import Iterator
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    # Readiness must not wait on an indefinitely stalled database connection.
    engine = create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    return engine


def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    session = get_session_factory()()
    # Publish the request's session so the app-level durability dependency
    # (`commit_before_response`) can commit THIS transaction before the
    # response is sent. The try/yield/except/else/finally below is unchanged:
    # its commit remains a backstop for callers that never go through a route.
    request.state.db_session = session
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()
    finally:
        session.close()


def commit_before_response(request: Request) -> Iterator[None]:
    """Commit the request's transaction BEFORE the response is sent.

    Declared once, app-level and ``scope="function"`` in ``create_app``, so
    EVERY present and future route inherits it and no route list has to be
    maintained (``EINV-11a``).

    Why it is needed: a route-level ``Depends(get_db)`` is closed on FastAPI's
    *inner* dependency stack, which exits after ``await response(...)`` — so
    ``get_db``'s own commit runs after the first byte has already left. A
    mutating route could therefore acknowledge a durable write whose commit
    then failed. A ``scope="function"`` dependency is closed on the function
    stack, which exits *before* the response is sent.

    Two rules make this correct rather than merely earlier:

    1. it must NOT commit on the failure path — it re-raises so ``get_db``
       still rolls back. Committing a failed request's partial writes would
       make this worse than the defect it fixes;
    2. it commits only the session ``get_db`` published, so there is exactly
       one transaction authority per request.
    """
    try:
        yield
    except Exception:
        raise
    session = getattr(request.state, "db_session", None)
    if session is not None:
        session.commit()


EMPLOYEE_FACTS_LOCK_NAMESPACE = "chronobeam:employee-facts"


def lock_employee_facts(session: Session, employee_id: uuid.UUID) -> None:
    """Serialize employee-fact mutations for one employee (transaction-scoped).

    ``pg_advisory_xact_lock`` is released by the request transaction's
    commit/rollback (``get_db``). Re-acquiring the same key inside one
    transaction is free, so a locking command may call a locking sub-command
    safely (``split_interval``, ``save_deviation``, ``patch_call``).

    Two rules the primitive's correctness depends on:

    1. acquire it BEFORE any read of that employee's current-version, overlap
       or ``max(version)+1`` state — a lock taken after the read proves nothing;
    2. one key per employee, everywhere. Only one lock is ever acquired, so
       deadlock is impossible; a ``hashtext`` collision between two employees
       only over-serializes.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{EMPLOYEE_FACTS_LOCK_NAMESPACE}:{employee_id}"},
    )


CALENDAR_COORDINATION_LOCK_NAMESPACE = "chronobeam:calendar-coordination"


def lock_calendar_coordination(session: Session, calendar_key: str = "primary") -> None:
    """Serialize a calendar publication against every ``REVIEWED`` transition.

    A calendar publication mutates a *global* calculation input, so no
    transaction may commit a work period as ``REVIEWED`` against calendar state
    the publication has already superseded (``EINV-15``). One shared advisory
    lock makes that impossible instead of documenting it as a residual.

    **Lock order rule: acquire this BEFORE ``lock_employee_facts``. Never the
    reverse.** ``publish_calendar_edition`` is the only other caller and takes
    no employee lock at all, so the global order is total and a
    calendar↔employee inversion is unreachable rather than merely unlikely.

    Transaction-scoped (``pg_advisory_xact_lock``), released by the request
    transaction's commit/rollback; re-acquiring the same key inside one
    transaction is free. The key is namespaced per calendar key, so a future
    second calendar does not over-serialize against the first.

    ceiling: one global lock held for one transaction; if a review ever became
    long enough for the wait to matter, a calendar-generation check taken under
    the employee lock is the upgrade path. Not now — publications are rare.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{CALENDAR_COORDINATION_LOCK_NAMESPACE}:{calendar_key}"},
    )


def constraint_name(exc: IntegrityError) -> str | None:
    """Name of the constraint an ``IntegrityError`` violated, else ``None``.

    psycopg3 exposes it on ``exc.orig.diag``. Returning ``None`` when the
    driver does not say is deliberate: callers compare against one specific
    constraint name, so an unidentifiable violation is re-raised rather than
    guessed at.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) or None


#: PostgreSQL plain-identifier shape. Unquoted names fold to lower case, so
#: this accepts exactly what a hand-written identifier may contain.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def quote_identifier(name: str) -> str:
    """Return ``name`` as a double-quoted SQL identifier, or refuse it.

    ``CREATE DATABASE`` and ``DROP DATABASE`` cannot take a bound parameter, so
    the name is interpolated. A name carrying a double quote would close the
    quoted identifier and inject SQL, and the name comes from a configured
    database URL — configuration is not a trust boundary for SQL text. The
    pattern is the plain-identifier shape PostgreSQL accepts (letters, digits,
    ``_``, ``$``, not starting with a digit), so a legitimate name always passes
    and anything else fails loudly instead of being quoted into the statement.
    """
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"unsafe database identifier {name!r}: expected letters, digits, '_' or '$'"
        )
    return f'"{name}"'


def ensure_database(url: str) -> None:
    """Create the database named in ``url`` if it does not exist yet.

    Connects to the same server (``postgres`` maintenance database) with
    autocommit and issues CREATE DATABASE. Used for the test database and by
    ``python -m app.db_admin``; never touches tables/data.
    """
    split = urlsplit(url)
    server = urlunsplit((split.scheme, split.netloc, "/postgres", "", ""))
    engine = create_engine(server, isolation_level="AUTOCOMMIT")
    db_name = split.path.lstrip("/")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            ).scalar()
            if not exists:
                # CREATE DATABASE cannot be parameterized, so the name is quoted
                # and validated as an identifier rather than trusted.
                connection.execute(
                    # nosemgrep: avoid-sqlalchemy-text - DDL; name validated
                    text(f"CREATE DATABASE {quote_identifier(db_name)}")
                )
    finally:
        engine.dispose()
