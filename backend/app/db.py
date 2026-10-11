"""PostgreSQL access: connection pool, schema bootstrap, time-series setup.

One PostgreSQL database plays three roles:
  * relational store  (vehicles, trips, events, geofences, alerts, settings)
  * time-series store (telemetry)
  * vector store      (RAG embeddings, via the pgvector extension)

It runs on any PostgreSQL 14+ with pgvector, in two modes:
  * TimescaleDB mode (local Docker image): telemetry is a hypertable with automatic
    daily chunks, compression after 7 days and retention after N days.
  * Plain PostgreSQL mode (e.g. Supabase, where TimescaleDB is deprecated on Postgres 17):
    a BRIN index on time (tiny, ideal for append-only data), built-in date_bin() for
    time buckets, and a housekeeping job that deletes telemetry older than N days.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg

from .config import settings

log = logging.getLogger("drivesense.db")
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
APP_TABLES = ["vehicles", "telemetry", "trips", "driving_events", "geofences", "notifications",
              "alert_settings", "health_alerts", "kb_chunks", "ai_logs"]


def _connect_kwargs(dsn: str) -> dict:
    # Supabase's transaction-mode pooler (Supavisor, port 6543) cannot keep prepared
    # statements between transactions, so asyncpg's statement cache must be off there.
    port = urlparse(dsn).port
    if port == 6543 or settings.database_disable_prepared:
        return {"statement_cache_size": 0}
    return {}


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self.has_timescale = False
        self.server_version = ""
        self.host = ""
        self.pooler: str | None = None

    @property
    def provider(self) -> str:
        return "Supabase" if "supabase." in self.host else "PostgreSQL"

    async def connect(self, dsn: str) -> None:
        kwargs = _connect_kwargs(dsn)
        parsed = urlparse(dsn)
        self.host = parsed.hostname or ""
        self.pooler = ("transaction" if parsed.port == 6543 else "session") if "pooler." in self.host else None
        await self._ensure_database_exists(dsn, kwargs)
        conn = await asyncpg.connect(dsn, **kwargs)
        try:
            await self._bootstrap(conn)
        finally:
            await conn.close()
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=settings.database_pool_max,
                                              init=self._init_connection, **kwargs)
        log.info("%s %s ready (%s%s)", self.provider, self.server_version.split()[0],
                 "TimescaleDB hypertable" if self.has_timescale else "plain PostgreSQL + BRIN time index",
                 f", {self.pooler} pooler" if self.pooler else "")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        # Let asyncpg convert JSONB <-> Python dicts automatically.
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    @staticmethod
    async def _ensure_database_exists(dsn: str, kwargs: dict) -> None:
        try:
            conn = await asyncpg.connect(dsn, **kwargs)
            await conn.close()
            return
        except asyncpg.InvalidCatalogNameError:
            pass
        parsed = urlparse(dsn)
        db_name = parsed.path.lstrip("/")
        admin_dsn = urlunparse(parsed._replace(path="/postgres"))
        conn = await asyncpg.connect(admin_dsn, **kwargs)
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            log.info("Created database %s", db_name)
        finally:
            await conn.close()

    async def _bootstrap(self, conn: asyncpg.Connection) -> None:
        self.server_version = await conn.fetchval("SHOW server_version")
        available = {r["name"] for r in await conn.fetch(
            "SELECT name FROM pg_available_extensions WHERE name IN ('vector', 'timescaledb')")}
        if "vector" not in available:
            raise RuntimeError(
                "The pgvector extension is not available in this PostgreSQL server. On Supabase it is "
                "built in (Dashboard > Database > Extensions > vector). Locally, use `docker compose up -d`.")

        # Supabase keeps extensions in their own `extensions` schema (on the search_path), so they
        # don't clutter `public`. Use it when it exists; schema.sql's plain CREATE is the fallback.
        if await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'"):
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions")
            except asyncpg.PostgresError as exc:
                log.warning("Could not create pgvector in the extensions schema (%s); trying the default", exc)

        sql = SCHEMA_FILE.read_text(encoding="utf-8").replace("__EMBED_DIM__", str(settings.embed_dim))
        try:
            await conn.execute(sql)
        except asyncpg.UndefinedObjectError as exc:
            raise RuntimeError(
                f"{exc}. pgvector is installed in a schema that is not on this connection's search_path. "
                "On Supabase, enable 'vector' in Dashboard > Database > Extensions (schema: extensions).") from exc

        mode = settings.use_timescaledb.strip().lower()
        if "timescaledb" in available and mode not in ("false", "0", "no", "off"):
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                self.has_timescale = True
            except asyncpg.PostgresError as exc:  # e.g. not in shared_preload_libraries
                log.warning("TimescaleDB present but could not be enabled: %s", exc)

        if self.has_timescale:
            await self._setup_timescale(conn)
        else:
            # BRIN = Block Range INdex: stores only min/max time per block of rows. For data that
            # arrives in time order it stays ~24 KB (a B-tree on time grows ~23 bytes per row) and
            # still lets range queries skip every block outside the requested time window.
            await conn.execute("CREATE INDEX IF NOT EXISTS telemetry_time_brin ON telemetry USING brin (time)")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS telemetry_vehicle_time_idx ON telemetry (vehicle_id, time DESC)")

        # Row Level Security with no policies. Supabase auto-publishes every table in the public
        # schema through its REST API (anon / authenticated keys); with RLS on and no policy,
        # that API sees nothing - your car's location history stays private. The backend
        # connects as the tables' owner, to which RLS does not apply, so it is unaffected.
        # (Skipped for the TimescaleDB hypertable: compression does not support RLS.)
        for table in APP_TABLES:
            if table == "telemetry" and self.has_timescale:
                continue
            await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    @staticmethod
    async def _setup_timescale(conn: asyncpg.Connection) -> None:
        # 1-day chunks: queries for "this trip" or "today" only touch one small chunk.
        await conn.execute(
            "SELECT create_hypertable('telemetry', by_range('time', INTERVAL '1 day'), "
            "if_not_exists => TRUE, migrate_data => TRUE)")
        optional = [
            # Columnar compression for data older than 7 days (segment by vehicle).
            "ALTER TABLE telemetry SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'vehicle_id', timescaledb.compress_orderby = 'time DESC')",
            "SELECT add_compression_policy('telemetry', INTERVAL '7 days', if_not_exists => TRUE)",
            # Automatically drop raw telemetry after N days.
            f"SELECT add_retention_policy('telemetry', INTERVAL '{int(settings.telemetry_retention_days)} days', "
            "if_not_exists => TRUE)",
        ]
        for stmt in optional:
            try:
                await conn.execute(stmt)
            except asyncpg.PostgresError as exc:
                log.warning("Timescale policy skipped (%s): %s", stmt.split("(")[0], exc)

    async def apply_retention(self) -> int:
        """Plain-PostgreSQL replacement for TimescaleDB's retention policy."""
        if self.has_timescale:
            return 0
        res = await self.pool.execute(
            "DELETE FROM telemetry WHERE time < now() - make_interval(days => $1)",
            int(settings.telemetry_retention_days))
        deleted = int(res.split()[-1])
        if deleted:
            log.info("Retention: deleted %d telemetry rows older than %d days", deleted,
                     settings.telemetry_retention_days)
        return deleted

    def bucket(self, width: str, column: str = "time") -> str:
        """SQL for a time bucket that works with or without TimescaleDB."""
        if self.has_timescale:
            return f"time_bucket(INTERVAL '{width}', {column})"
        return f"date_bin(INTERVAL '{width}', {column}, TIMESTAMPTZ '2000-01-01')"


db = Database()
