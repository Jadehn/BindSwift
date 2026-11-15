"""Thin database layer: plain parameterized SQL with psycopg 3.
Never build SQL with f-strings or string concatenation of user input;
always pass values as parameters (%s or %(name)s)."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        s = get_settings()
        url = s.database_url
        local = any(h in url for h in ("@localhost", "@127.0.0.1", "@/"))
        if not s.is_dev and not local and "sslmode=require" not in url and "sslmode=verify" not in url:
            raise RuntimeError("DATABASE_URL must use TLS in production: add ?sslmode=require")
        # min_size=0: serverless instances don't hold idle connections.
        # prepare_threshold=None: works with pooled Postgres (Supabase/Neon/PgBouncer transaction mode).
        _pool = ConnectionPool(url, min_size=0, max_size=s.db_pool_max,
                               kwargs={"row_factory": dict_row, "prepare_threshold": None}, open=True)
    return _pool


@contextmanager
def tx():
    """One transaction: commits on success, rolls back on any exception."""
    with pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


def one(sql: str, params=None):
    with tx() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def all_(sql: str, params=None):
    with tx() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run(sql: str, params=None) -> None:
    with tx() as cur:
        cur.execute(sql, params)


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
