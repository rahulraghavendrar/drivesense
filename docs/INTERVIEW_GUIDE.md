# DriveSense - Interview Guide

How to explain this project simply, demo it, and defend every decision.

---

## 1. The 30-second pitch

> "DriveSense is a website - it also installs as a phone app - for monitoring my car. The car
> streams sensor data every second, and the site shows its live status, location, trips,
> a driving score and the car's health. The key part is alerts: the backend **predicts**
> problems by fitting a trend to the sensor data, so it warns me *before* a limit is crossed,
> like 'coolant will hit 110 °C in about a minute'. I choose on the website what else should
> alert me - speeding, low fuel, the car being used at night, harsh driving - and alerts reach
> the browser in about a second as a banner, a sound and a desktop notification. A **GenAI
> coach** uses RAG grounded in my car's real numbers to explain my score, diagnose alerts and
> write a weekly report. It's React, FastAPI, PostgreSQL on Supabase with pgvector, a
> low-latency live layer (in-process, or Redis when scaled out) and Gemini. Since I don't have
> hardware in the car, a sensor simulator plays the car through the same API a real
> telematics unit would use."

## 2. The 2-minute walkthrough (whiteboard)

```
 [sensor simulator = my car] --HTTP 1/s--> [FastAPI ingest] --> stream processor
                                               |                 - trip engine
                                               |                 - 7 event detectors -> score
                                               |                 - health monitor (predictive)
                                               |                 - MY alert rules (speed, fuel, night)
                                               |                 - geofences
                                  hot path     v                  cold path
                  live layer: in-process or Redis   PostgreSQL on Supabase (telemetry with
                  (latest state, pub/sub, commands,  BRIN index, trips, alerts) + pgvector (RAG)
                   leaderboard, AI cache)            [locally: TimescaleDB hypertable]
                                   |                                |
                  WebSocket push -> website: banner + sound + OS notification
                                   website <-- REST --> AI (facts + RAG + Gemini)
```

Say it in this order:

1. **Data in.** The car POSTs a reading every second. Pydantic validates every field.
2. **Processing.** A small stream processor for the car:
   - ignition on starts a trip, ignition off ends it;
   - accelerations computed from speed and heading changes give 7 driving events and a score;
   - each sensor is checked against its healthy band and trend;
   - my alert rules and geofences are checked.
3. **Two storage paths.**
   - **Hot:** latest state into the live layer (in-process or Redis), pushed over WebSocket. No database on this path.
   - **Cold:** raw rows buffered and written to PostgreSQL (Supabase) with one `COPY` every 0.5 s.
4. **Alerts.** A rule hit becomes a notification: stored in Postgres, published on the live layer's channel, pushed to the browser. The website turns it into a banner, a tone and an OS notification.
5. **AI.** The backend computes exact facts, retrieves the right guide sections with hybrid search, and asks Gemini to explain using only those, with citations.

## 3. How each part works (simple explanations)

### Driving events - two physics formulas
- **Acceleration** = change in speed ÷ time.
  - −3.5 m/s² (losing 12.6 km/h in one second) is **harsh braking**;
  - +3.0 m/s² is **harsh acceleration**.
- **Sideways force** = speed × yaw rate (how fast the heading turns, in rad/s). Above 4 m/s² (about 0.4 g) it is a **harsh turn**.
- **Over speed limit:** more than 10% (+2 km/h) over for at least 5 s, one event per episode.
- **Turns** are detected when the heading changes by 45° or more.
  - Accelerating in the 4 s before a turn is **ABT**.
  - Braking during or just after a turn is **BAT**.
- **FAB:** 6 or more switches between accelerating and braking within 60 s.

### The score
`score = 100 − Σ (weight × count) × 10 / max(km, 5)`

That is weighted events per 10 km.
- Weights: harsh braking 5, harsh turn 4, harsh acceleration, over speed and FAB 3, ABT and BAT 2.
- The weekly score is distance-weighted.

**Why a formula, not ML?** It is explainable: the AI can say exactly which event cost how many points. I also have no labelled crash data.

### Predictive health
Each signal has a healthy band, a warning limit and a critical limit.
1. **Threshold check:** crossed the warning or critical limit? Raise an alert.
2. **Prediction:** outside the healthy band *and* trending toward the limit? Fit a least-squares line over 60 s.
   `ETA = (limit − value) / slope`. Under 15 minutes means a *predicted* alert with a countdown.
3. **Resolution:** back in the band for 10 readings? Resolve.

The **healthy-band gate** prevents a false alarm on every cold start (20 to 90 °C is steep but normal). A unit test covers this.

**Battery, over days:** a `date_bin('1 day')` query (`time_bucket` on TimescaleDB) gives the average resting voltage per day. A line fit through those averages projects the date it drops below 12.0 V, the "may not start" point.

### Alerts - the website raises them, the owner decides what triggers them
- **Always on:** health alerts (actual and predicted).
- **Owner rules** (Alert settings page, stored in PostgreSQL):
  - speed above X km/h for 3 s, one alert per episode, re-armed once 5 km/h below;
  - low fuel below X %, re-armed after refuelling;
  - night-time use: ignition on inside a window like 23:00-05:00, evaluated **in the owner's time zone** and handling windows that cross midnight;
  - every harsh event;
  - geofence enter/exit.
- **Rule engine:** `engine/rules.py` is pure Python and unit-tested. Saving settings updates the live processor, so a new rule applies from the next reading. There is no restart.
- **Delivery:**
  1. notification row in Postgres;
  2. the live layer's publish/subscribe channel (Redis Pub/Sub, or an in-process queue per listener);
  3. WebSocket push;
  4. in the browser:
     - a banner (red for critical, which stays until dismissed; yellow for warnings, which disappear after a minute);
     - a tone (Web Audio; browsers allow sound only after the first click, so it is unlocked then);
     - an **OS notification** through the Notification API when the tab is hidden;
     - the tab title shows the alert.
- **Test alerts** use the same path, so the owner can check that their browser is set up.

### Telemetry-grounded RAG
1. **Knowledge base:** Markdown guides split at `##` sections, so each section is one citable chunk. De-duplicated by hash.
2. **Two indexes on the same rows:** full text (`tsvector`) and Gemini embeddings (768-d, pgvector HNSW).
3. **Hybrid retrieval:** keyword (exact terms like "TPMS") plus semantic (meaning). Merged with **Reciprocal Rank Fusion**: `Σ 1/(60 + rank)`.
4. **Query decomposition:** one focused search per problem (per driving event, per alert). All queries are embedded in **one** batched call.
5. **Grounding:** the prompt holds computed facts and numbered passages. Rules forbid invented numbers and require `[S1]` citations. The UI shows the sources and "what the AI was told".
6. **Four features on the same pattern:** trip coach, alert diagnosis, chat, and the **weekly report** (this week vs last week, plus one focus).
7. **Reliability:**
   - answers are cached (trip, alert, or 15 minutes in the live layer for reports) and logged to `ai_logs`;
   - a deterministic fallback runs if Gemini is down, rate-limited or not configured.

### The sensor simulator (no hardware)
- A separate program using only the public device API, so the backend cannot tell it from a real car.
- **Driver model** on real road routes (OpenStreetMap, cached; a grid when offline):
  - each trip is calm, normal or aggressive at random, so scores vary like a real owner's;
  - it slows for curves (`v = sqrt(grip / curvature)`) and stops at lights, some noticed late (harsh braking);
  - an aggressive driver tailgates and speeds up into turns.
- **Sensor physics:** coolant warm-up, alternator voltage, tyre warm-up, and fuel use that rises with acceleration.
- **Battery:** wears slowly over days, for the prediction demo.
- **Faults:** random, or injected from the website's Simulator panel.
- **Store-and-forward** when the backend is down.
- **On restart** it resumes the car's state (position, fuel, odometer, battery wear) from the backend.

Personas were calibrated by running 60 simulated trips each through the real detector: calm about 99, normal about 93, aggressive about 34.

## 4. Tech-stack questions

**Why FastAPI over Flask?**
I/O-bound workload: a stream of readings, open WebSockets, and LLM calls that take seconds. FastAPI is async-native. It also gives Pydantic validation (bad sensor data and bad alert settings are rejected with a 422), built-in WebSockets and auto-generated docs. Flask would need gevent and Flask-SocketIO.

**Why PostgreSQL on Supabase?**
- One database for relational data, time-series telemetry and RAG vectors: one connection pool, one backup, one security model.
- Supabase manages it (backups, upgrades) and has pgvector built in, so there is nothing to run locally.

**Telemetry is time-series. Why not TimescaleDB?**
I designed for TimescaleDB first (hypertable, daily chunks, compression, retention policy), and it still runs that way in local Docker. TimescaleDB is deprecated on Supabase's Postgres 17, so the backend detects the server and switches to *plain PostgreSQL mode*:
- **BRIN index on `time`.** It stores only the min and max time of each block of rows. Telemetry arrives in time order, so it still skips everything outside a time range, while staying tiny. Measured on 20,000 rows: 24 KB, vs 464 KB for a B-tree on `time`. The B-tree grows with every row (about 23 MB per million rows); the BRIN barely grows.
- A **B-tree on `(vehicle_id, time DESC)`** for "latest rows of this car".
- **`date_bin()`** (built into Postgres 14+) instead of `time_bucket()` for daily averages.
- **Retention** as an hourly `DELETE` of telemetry older than N days, instead of a retention policy.

The queries are the same; `db.bucket()` emits the right SQL for each mode. At one car's volume, plain Postgres is plenty; TimescaleDB would earn its place at fleet scale (compression, chunk pruning).

**Why pgvector in the same Postgres?**
Fewer moving parts: one backup, one pool, one security model. Hybrid search is one database. A dedicated vector database earns its place at far larger scale.

**Why a separate live layer, and why in-process instead of Redis?**
The live screen and alerts must not wait for a cloud database round trip. The live layer holds:
- the latest state (one lookup);
- the publish/subscribe channel that feeds the WebSocket hub;
- the remote-command queue;
- the sorted-set leaderboard (rank among 20,000 drivers in about 1-2 ms);
- the AI report cache.

It has one interface with two implementations, chosen by `REDIS_URL`:
- **`memory://`:** Python dictionaries and an `asyncio.Queue` per listener inside the API process. No extra service, no network hop. On restart it is rebuilt from PostgreSQL (last reading, leaderboard score). Right for one car and one API process.
- **Redis:** needed once there are several API processes, because Pub/Sub lets ingest on one process reach a browser connected to another, and the state is shared.

Unit tests check that the in-process version behaves like Redis (rank order, command draining, fan-out, expiry).

**Why WebSocket instead of polling?**
Alerts must be immediate. Polling every second from every open tab wastes requests and still adds up to a second of delay. A WebSocket pushes the moment the rule fires.

**Why HTTP ingest and not MQTT?**
HTTP is simple to build and demo, and commands piggyback on the reply. Production cars use MQTT, built for flaky mobile networks, usually bridged into Kafka. The processor is per car and per stream, so it would become a Kafka consumer unchanged.

## 5. Likely interview questions

**How do alerts reach you if the website is closed?**
Today: OS notifications work while the site is open in any tab, even in the background.

The next step is **Web Push**:
- a service worker plus VAPID keys;
- the backend sends to the browser's push service, which wakes the service worker even when the browser is closed;
- the same notification row would also go to email or SMS.

The pipeline already funnels every alert through one `notify()` function, so that is one new channel, not a redesign.

**How do you avoid alert spam?**
- Every rule is **edge-triggered with hysteresis**:
  - a speed alert fires once per episode and re-arms only below limit − 5 km/h;
  - low fuel re-arms only after refuelling;
  - health alerts have a lifecycle (opened, escalated, resolved), so ETA updates refresh the alert instead of creating new ones.
- In the browser, warnings fade after a minute, only critical alerts stay, and the banner list is capped at 4.

**How would this scale to many cars?**
- MQTT into Kafka, partitioned by vehicle (ordered per car).
- Stateless consumers each own a set of cars (the same processor class), checkpointing open-trip state to Redis.
- Telemetry in TimescaleDB (already supported) or a dedicated time-series store, with downsampling.
- The live layer on Redis (one config change), then Redis Cluster.
- Horizontally scaled API: works once `REDIS_URL` points at Redis, because live push goes through Redis Pub/Sub.

**How do you stop the LLM from hallucinating?**
- It only sees computed facts and retrieved passages.
- Rules forbid inventing numbers and require citations.
- The important numbers (points lost, ETAs) are computed in Python.
- The UI shows the exact context.
- There is a deterministic fallback.

Next: automatically check that every number in the answer exists in the facts.

**How did you test it?**
- **24 unit tests:**
  - every event detector;
  - the score;
  - the trip engine;
  - predictive health (no cold-start false alarm; predicted before the warning limit);
  - the alert rules (once per episode, re-arm, night window across midnight in a real time zone);
  - the in-process live layer against Redis behaviour.
- Simulator calibration runs.
- Both database modes end to end: a Supabase-like Postgres (non-superuser owner, pgvector in an `extensions` schema, no TimescaleDB) with the in-process live layer, and TimescaleDB with Redis.
- End-to-end runs with screenshots on desktop and phone.
- The Gemini path tested with a mocked client.

**Bugs you found?**
1. **History seeding stamped some readings minutes in the future.** The backend correctly rejected later live data as out of order. Fix: the seeder never steps past "now", and the backend rejects timestamps more than 2 minutes in the future.
2. **Restarting the simulator re-anchored the slowly dying battery**, so it looked suddenly healthy and the trend alert resolved. Fix: on restart the simulator resumes battery wear from the backend's own daily trend.

**Security and privacy?**
- Device key header (production: per-device certificates).
- Pydantic validation, parameterised SQL.
- API key only in a git-ignored `.env`.
- Location is personal data:
  - raw telemetry retention is enforced (21 days on Supabase, 90 locally);
  - on Supabase every table has **Row Level Security on with no policies**, so Supabase's auto-generated REST API (anon key) returns nothing, while the backend, as the tables' owner, is unaffected. I checked this by querying as the `anon` role: zero rows.
  - The next steps are login (OAuth2/JWT) and never sending personal data to the free LLM tier.

**Why is this relevant to an automaker?**
- Owner apps (status, remote lock, location, trip logs).
- Usage-based insurance (driving score).
- Predictive maintenance (fix before failure).
- Safety alerts.

This project covers all of them end to end.

## 6. What I'd build next
1. Web Push (alerts with the browser closed) and email/SMS channels.
2. MQTT + Kafka ingest with per-device mutual TLS.
3. Service reminders from the odometer, and an EV mode (state of charge, charging, range from driving style).
4. ML anomaly detection to *complement* the explainable rules.
5. Map-matching for real speed limits.
6. Automatic fact-checking of AI answers, and an evaluation set.

## 7. Numbers worth remembering
- 1 car, a reading every second, about 11,000 telemetry rows for a seeded fortnight.
- Alert latency about 1 s end to end; server-side ingest about 4 ms per reading.
- Leaderboard rank among 20,000 drivers in about 1-2 ms.
- BRIN time index: 24 KB, vs 464 KB for a B-tree on `time`, on 20,000 rows.
- Event thresholds: harsh braking −3.5 m/s², harsh acceleration +3.0 m/s², harsh turn 4.0 m/s².
- Health limits: coolant healthy up to 102 °C, warning 105 °C, critical 110 °C; tyres warning 30 psi, critical 27 psi.
- Battery: 12.0 V is the "may not start" point.
- RAG: 46 knowledge chunks, 768-d embeddings, HNSW index, RRF k = 60.
- 24 unit tests.
