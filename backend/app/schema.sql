-- DriveSense schema. Every statement is idempotent so it can run on each startup.
-- __EMBED_DIM__ is replaced with settings.embed_dim before execution.

CREATE EXTENSION IF NOT EXISTS vector;

-- Vehicles registered by devices (here: the fake sensor feeder)
CREATE TABLE IF NOT EXISTS vehicles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    model       TEXT NOT NULL,
    plate       TEXT,
    driver      TEXT,
    tank_l      REAL NOT NULL DEFAULT 50,
    home_lat    DOUBLE PRECISION,
    home_lon    DOUBLE PRECISION,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw 1 Hz sensor stream. Becomes a TimescaleDB hypertable when the extension exists.
-- Wide table (one column per signal): the signal set is fixed and known, so this is
-- smaller and faster to scan than a key/value (EAV) layout.
CREATE TABLE IF NOT EXISTS telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      TEXT NOT NULL,
    ignition        BOOLEAN NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    heading         REAL,
    speed_kph       REAL NOT NULL,
    speed_limit_kph REAL,
    rpm             REAL,
    fuel_pct        REAL,
    odometer_km     DOUBLE PRECISION,
    battery_v       REAL,
    coolant_c       REAL,
    tire_fl         REAL,
    tire_fr         REAL,
    tire_rl         REAL,
    tire_rr         REAL,
    headlights      BOOLEAN,
    doors_locked    BOOLEAN,
    alarm_on        BOOLEAN
);

-- One row per completed trip (computed by the trip engine, not by the device)
CREATE TABLE IF NOT EXISTS trips (
    id                 BIGSERIAL PRIMARY KEY,
    vehicle_id         TEXT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    name               TEXT,
    start_time         TIMESTAMPTZ NOT NULL,
    end_time           TIMESTAMPTZ NOT NULL,
    start_lat          DOUBLE PRECISION,
    start_lon          DOUBLE PRECISION,
    end_lat            DOUBLE PRECISION,
    end_lon            DOUBLE PRECISION,
    start_place        TEXT,
    end_place          TEXT,
    distance_km        DOUBLE PRECISION NOT NULL,
    duration_s         INTEGER NOT NULL,
    max_speed_kph      DOUBLE PRECISION,
    avg_speed_kph      DOUBLE PRECISION,
    fuel_used_l        DOUBLE PRECISION,
    fuel_economy_kmpl  DOUBLE PRECISION,
    score              INTEGER NOT NULL,
    event_counts       JSONB NOT NULL DEFAULT '{}'::jsonb,
    route_preview      JSONB NOT NULL DEFAULT '[]'::jsonb,
    coach_summary      TEXT,
    coach_model        TEXT
);
CREATE INDEX IF NOT EXISTS trips_vehicle_start_idx ON trips (vehicle_id, start_time DESC);

-- The 7 driving-behaviour events (OSL, ABT, BAT, FAB, HA, HB, HT)
CREATE TABLE IF NOT EXISTS driving_events (
    id          BIGSERIAL PRIMARY KEY,
    trip_id     BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    vehicle_id  TEXT NOT NULL,
    time        TIMESTAMPTZ NOT NULL,
    type        TEXT NOT NULL,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    speed_kph   DOUBLE PRECISION,
    value       DOUBLE PRECISION,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS driving_events_trip_idx ON driving_events (trip_id);
CREATE INDEX IF NOT EXISTS driving_events_vehicle_time_idx ON driving_events (vehicle_id, time DESC);

CREATE TABLE IF NOT EXISTS geofences (
    id          BIGSERIAL PRIMARY KEY,
    vehicle_id  TEXT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    radius_m    REAL NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    id          BIGSERIAL PRIMARY KEY,
    vehicle_id  TEXT NOT NULL,
    time        TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    title       TEXT NOT NULL,
    body        TEXT,
    is_read     BOOLEAN NOT NULL DEFAULT false,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS notifications_vehicle_time_idx ON notifications (vehicle_id, time DESC);

-- Owner-defined alert rules (set on the website's Alert settings page)
CREATE TABLE IF NOT EXISTS alert_settings (
    vehicle_id       TEXT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
    speed_alert_kph  DOUBLE PRECISION,               -- NULL = off
    low_fuel_pct     DOUBLE PRECISION NOT NULL DEFAULT 15,
    night_start      TEXT,                           -- 'HH:MM', NULL = off
    night_end        TEXT,
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    harsh_driving    BOOLEAN NOT NULL DEFAULT false,
    geofence         BOOLEAN NOT NULL DEFAULT true,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vehicle-health alerts with a lifecycle (active -> resolved)
CREATE TABLE IF NOT EXISTS health_alerts (
    id               BIGSERIAL PRIMARY KEY,
    vehicle_id       TEXT NOT NULL,
    code             TEXT NOT NULL,
    signal           TEXT NOT NULL,
    kind             TEXT NOT NULL,         -- predictive | threshold | trend
    severity         TEXT NOT NULL,         -- watch | warning | critical
    title            TEXT NOT NULL,
    message          TEXT NOT NULL,
    value            DOUBLE PRECISION,
    eta_seconds      DOUBLE PRECISION,
    status           TEXT NOT NULL DEFAULT 'active',
    opened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    diagnosis        TEXT,
    diagnosis_model  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS health_alerts_one_active
    ON health_alerts (vehicle_id, code) WHERE status = 'active';

-- RAG knowledge base: keyword index (tsvector) + semantic index (pgvector) on the same rows
CREATE TABLE IF NOT EXISTS kb_chunks (
    id            BIGSERIAL PRIMARY KEY,
    collection    TEXT NOT NULL,            -- coach | service
    source        TEXT NOT NULL,
    title         TEXT,
    section       TEXT,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    tsv           tsvector GENERATED ALWAYS AS (
                      to_tsvector('english', coalesce(title, '') || ' ' || coalesce(section, '') || ' ' || content)
                  ) STORED,
    embedding     vector(__EMBED_DIM__),
    embed_model   TEXT
);
CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- Every AI answer is logged with what it was grounded on (auditability)
CREATE TABLE IF NOT EXISTS ai_logs (
    id          BIGSERIAL PRIMARY KEY,
    time        TIMESTAMPTZ NOT NULL DEFAULT now(),
    feature     TEXT NOT NULL,
    vehicle_id  TEXT,
    ref_id      TEXT,
    question    TEXT,
    answer      TEXT,
    sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
    model       TEXT,
    latency_ms  INTEGER,
    used_llm    BOOLEAN NOT NULL DEFAULT false
);
