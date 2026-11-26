"""The database behind exuvia's records, as a small pluggable adapter.

exuvia keeps run history, schedules, agent sessions and code revisions in SQL
(``store.py``). Node outputs are files (``artifacts.py``), never rows. Which
database holds the records is configuration:

    EXUVIA_DB unset              SQLite at ``.exuvia/exuvia.db`` inside each
                                 project (the default; nothing to set up)
    EXUVIA_DB=sqlite:///<path>   one SQLite file shared by every project
    EXUVIA_DB=postgresql://...   Postgres (``pip install 'exuvia[postgres]'``)
    EXUVIA_DB_SCOPE              prefix for project scopes in a shared
                                 database, e.g. a user id, so several hubs can
                                 share one database without seeing each other

A database that belongs to one project stores its rows under the empty scope.
In a shared database each project's rows carry ``EXUVIA_DB_SCOPE`` + the
project id. One process at a time serves a given project.

An adapter speaks one SQL dialect that SQLite and Postgres both accept:
``?`` placeholders, ``on conflict ... do`` upserts, window functions and the
column types ``text``, ``integer`` and ``double precision``. A new backend (an
HTTP SQL service such as Cloudflare D1, say) implements ``Database``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

Row = dict[str, Any]


class Database(Protocol):
    """What ``Store`` needs from a database."""

    dialect: str
    """``sqlite`` or ``postgres``: only migrations look at it."""

    file: Path | None
    """The SQLite file when this database is one file that a project export
    can carry; None when the records live somewhere else."""

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[Row]:
        """Run one statement; return its rows (empty for writes)."""
        ...

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Statements inside run atomically and no other thread interleaves."""
        ...

    def columns(self, table: str) -> set[str]:
        """Column names of ``table`` (empty when it does not exist)."""
        ...

    def backup_to(self, target: Path) -> None:
        """A consistent copy as a SQLite file (only when ``file`` is set)."""
        ...

    def close(self) -> None: ...


class SQLiteDatabase:
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.file = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.file, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.execute("pragma busy_timeout=5000")

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[Row]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args))]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                yield
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")

    def columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.query(f"pragma table_info({table})")}

    def backup_to(self, target: Path) -> None:
        with self._lock:
            dest = sqlite3.connect(target)
            try:
                self._db.backup(dest)
            finally:
                dest.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class PostgresDatabase:
    dialect = "postgres"
    file = None

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:  # pragma: no cover - depends on the install
            raise RuntimeError("EXUVIA_DB is a Postgres URL: install exuvia[postgres] (psycopg)") from e
        self._psycopg = psycopg
        self._row_factory = dict_row
        self._url = url
        self._lock = threading.RLock()
        self._in_tx = False
        self._conn = self._connect()

    def _connect(self) -> Any:
        return self._psycopg.connect(self._url, autocommit=True, row_factory=self._row_factory)

    def _dropped(self) -> bool:
        return bool(self._conn.closed or getattr(self._conn, "broken", False))

    def _live(self) -> Any:
        """The connection, reopened if the server dropped it (idle timeouts,
        restarts). Never inside a transaction: that has to fail instead."""
        if self._dropped() and not self._in_tx:
            self._conn = self._connect()
        return self._conn

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[Row]:
        with self._lock:
            try:
                return self._run(sql, args)
            except self._psycopg.OperationalError:
                if self._in_tx or not self._dropped():
                    raise
                return self._run(sql, args)  # the connection dropped; _live reconnects

    def _run(self, sql: str, args: Sequence[Any]) -> list[Row]:
        with self._live().cursor() as cur:
            cur.execute(_pg_placeholders(sql), tuple(args))
            return list(cur.fetchall()) if cur.description else []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            conn = self._live()
            self._in_tx = True
            try:
                with conn.transaction():
                    yield
            finally:
                self._in_tx = False

    def columns(self, table: str) -> set[str]:
        rows = self.query(
            "select column_name from information_schema.columns "
            "where table_schema = current_schema() and table_name = ?",
            (table,),
        )
        return {r["column_name"] for r in rows}

    def backup_to(self, target: Path) -> None:
        raise RuntimeError("a Postgres database is not copied into a project export")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _pg_placeholders(sql: str) -> str:
    """``?`` placeholders to psycopg's ``%s``. exuvia's SQL never has a ``?``
    or ``%`` inside a string literal, so a plain replace is exact."""
    return sql.replace("%", "%%").replace("?", "%s")


# -- configuration -------------------------------------------------------------
_shared: dict[str, Database] = {}
_shared_lock = threading.Lock()


def database_url() -> str:
    return os.environ.get("EXUVIA_DB", "").strip()


def scope_prefix() -> str:
    return os.environ.get("EXUVIA_DB_SCOPE", "")


def open_database(url: str) -> Database:
    """A new connection to the database ``url`` names."""
    if url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///"):])
        path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteDatabase(path)
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresDatabase(url)
    raise ValueError(f"EXUVIA_DB must be sqlite:///<path> or postgresql://..., not {url.split(':', 1)[0]!r}")


def shared_database(url: str) -> Database:
    """One connection per URL per process, shared by every project's store."""
    with _shared_lock:
        db = _shared.get(url)
        if db is None:
            db = _shared[url] = open_database(url)
        return db


def close_shared() -> None:
    """Close every shared connection (tests, shutdown)."""
    with _shared_lock:
        for db in _shared.values():
            db.close()
        _shared.clear()


# Postgres advisory lock key that serializes schema creation and upgrades
# across processes. Any fixed 64-bit value works; this one is "exuvia" in hex.
SCHEMA_LOCK_KEY = 0x657875766961


class HubRecordStore:
    """What a hub's record stores (users, sharing, groups, data keys) share:
    their tables in a database under one ``scope``, created once per
    database, and copied to and from a hub export's ``hub.db``.

    A store declares ``TABLES`` (name -> ``create table``), ``INDEXES``,
    ``ARCHIVE_WHAT`` (what its tables hold, for errors) and ``OPTIONAL``,
    the tables an archive from an older hub may lack (None: any of them,
    which then reads as empty). ``_replaced`` runs after an import."""

    TABLES: dict[str, str] = {}
    INDEXES: tuple[str, ...] = ()
    ARCHIVE_WHAT = "records"
    OPTIONAL: frozenset[str] | None = None
    _prepared: weakref.WeakSet[Any]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._prepared = weakref.WeakSet()

    def __init__(self, db: Database, scope: str = "", owns_db: bool = False):
        self.db = db
        self.scope = scope
        self._owns_db = owns_db
        self.prepare(db)

    @classmethod
    def prepare(cls, db: Database) -> None:
        """Create the tables once per database. In Postgres even ``create ...
        if not exists`` locks the table, which can deadlock against a sign-in
        on another replica, so once the tables exist no DDL runs at all.
        Creating them takes the schema lock store.py uses, so replicas
        starting together on a new database wait for the first one."""
        if db in cls._prepared:
            return
        if not all(db.columns(t) for t in cls.TABLES):
            with db.transaction():
                if db.dialect == "postgres":
                    db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
                for ddl in (*cls.TABLES.values(), *cls.INDEXES):
                    db.query(ddl)
        cls._prepared.add(db)

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, args)

    # -- hub export / import --------------------------------------------------------
    def dump_to(self, target: Path) -> None:
        """This scope's rows as tables in a SQLite file (the hub export's
        ``hub.db``, which every store writes its own tables into)."""
        con = sqlite3.connect(target)
        try:
            for ddl in (*self.TABLES.values(), *self.INDEXES):
                con.execute(ddl)
            for table in self.TABLES:
                con.execute(f"delete from {table}")
                for r in self._q(f"select * from {table} where scope=?", (self.scope,)):
                    con.execute(f"insert into {table} ({', '.join(r)}) values ({', '.join('?' * len(r))})", tuple(r.values()))
            con.commit()
        finally:
            con.close()

    def replace_from(self, source: Path) -> Any:
        """Replace this scope's rows with those in a file ``dump_to`` wrote
        (hub import). ValueError when the file cannot be read. Returns what
        ``_replaced`` does."""
        con = sqlite3.connect(source)
        con.row_factory = sqlite3.Row
        try:
            present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            optional = self.TABLES.keys() if self.OPTIONAL is None else self.OPTIONAL
            data = {
                t: [dict(r) for r in con.execute(f"select * from {t}")] if t in present or t not in optional else []
                for t in self.TABLES
            }
        except sqlite3.OperationalError as e:
            raise ValueError(f"the archive's {self.ARCHIVE_WHAT} are unreadable: {e}") from e
        finally:
            con.close()
        with self.db.transaction():
            for table, rows in data.items():
                self._q(f"delete from {table} where scope=?", (self.scope,))
                for r in rows:
                    r["scope"] = self.scope
                    self._q(f"insert into {table} ({', '.join(r)}) values ({', '.join('?' * len(r))})", tuple(r.values()))
        return self._replaced(data)

    def _replaced(self, data: dict[str, list[dict[str, Any]]]) -> Any:
        """After an import: drop what the store keeps in memory."""
        return None


# Every store a hub export carries in its hub.db (Hub.export / Hub.restore),
# by the attribute that holds it on the hub, in the order they are written.
HUB_STORES = ("users", "sharing", "groups", "data_keys")
