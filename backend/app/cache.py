"""The low-latency "hot" layer.

  * latest vehicle state   -> one JSON document per vehicle (O(1) read)
  * live fan-out           -> publish/subscribe channel feeding the WebSocket hub
  * device command queue   -> one list per vehicle (remote lock, demo commands ...)
  * leaderboard            -> sorted sets (rank of 1 driver among N)
  * small JSON cache       -> AI weekly reports (15 min)

Two interchangeable backends behind one interface:
  RedisStore   (REDIS_URL=redis://...)  sub-millisecond, shared by every API replica,
               Pub/Sub lets ingest on replica A reach a browser on replica B.
  MemoryStore  (REDIS_URL=memory://)    the same data structures inside the API process.
               Even faster (no network hop), no extra service to run, but only works with
               a single API process - perfect for one personal car.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

PREFIX = "ds:"
LIVE_CHANNEL = PREFIX + "live"
MODELS = ["Ranger", "Everest", "Transit", "F-150", "Bronco", "Escape"]


def _latest_key(vid: str) -> str:
    return f"{PREFIX}latest:{vid}"


def _cmd_key(vid: str) -> str:
    return f"{PREFIX}cmd:{vid}"


def _lb_key(scope: str) -> str:
    return f"{PREFIX}lb:{scope}"


def _population(count: int, extra_models: list[str] | None) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Synthetic 'community drivers' so ranks look like a real deployment. Each drives one of
    the common models (plus the owner's model), so 'same model' rankings have company too."""
    rng = random.Random(42)
    models = list(dict.fromkeys(MODELS + list(extra_models or [])))
    all_scores: dict[str, float] = {}
    per_model: dict[str, dict[str, float]] = {m: {} for m in models}
    for i in range(count):
        member = f"c:{i}"
        all_scores[member] = round(max(5.0, rng.betavariate(8, 2) * 100), 1)   # skewed to good drivers, mean ~80
        per_model[models[i % len(models)]][member] = all_scores[member]
    return all_scores, per_model


def _lb_result(scope, rank0, score, total, top, around_start, around, updated) -> dict[str, Any]:
    rank = rank0 + 1 if rank0 is not None else None
    return {
        "scope": scope,
        "rank": rank,
        "score": round(score, 1) if score is not None else None,
        "total": total,
        "ahead_of_pct": round(100 * (total - rank) / total, 2) if rank and total else None,
        "top": [{"member": m, "score": round(s, 1), "rank": i + 1} for i, (m, s) in enumerate(top)],
        "around": [{"member": m, "score": round(s, 1), "rank": around_start + i + 1} for i, (m, s) in enumerate(around)],
        "updated_at": updated,
    }


# ============================================================================== Redis
class RedisStore:
    kind = "redis"

    def __init__(self) -> None:
        self.r = None

    async def connect(self, url: str) -> None:
        import redis.asyncio as aioredis
        self.r = aioredis.from_url(url, decode_responses=True)
        await self.r.ping()

    async def close(self) -> None:
        if self.r:
            await self.r.aclose()

    async def set_latest(self, vid: str, state: dict[str, Any]) -> None:
        await self.r.set(_latest_key(vid), json.dumps(state, default=str))

    async def get_latest(self, vid: str) -> dict[str, Any] | None:
        raw = await self.r.get(_latest_key(vid))
        return json.loads(raw) if raw else None

    async def get_latest_many(self, vids: list[str]) -> dict[str, dict[str, Any] | None]:
        if not vids:
            return {}
        raws = await self.r.mget([_latest_key(v) for v in vids])
        return {v: (json.loads(raw) if raw else None) for v, raw in zip(vids, raws)}

    async def publish(self, msg_type: str, vehicle_id: str, data: Any) -> None:
        payload = json.dumps({"type": msg_type, "vehicle_id": vehicle_id, "data": data}, default=str)
        await self.r.publish(LIVE_CHANNEL, payload)

    async def listen(self) -> AsyncIterator[str]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(LIVE_CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield msg["data"]
        finally:
            await pubsub.aclose()

    async def push_command(self, vid: str, command: dict[str, Any]) -> None:
        await self.r.rpush(_cmd_key(vid), json.dumps(command))

    async def pop_commands(self, vids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Atomically drain the queued commands for each vehicle."""
        if not vids:
            return {}
        async with self.r.pipeline(transaction=True) as pipe:
            for v in vids:
                pipe.lrange(_cmd_key(v), 0, -1)
                pipe.delete(_cmd_key(v))
            res = await pipe.execute()
        return {v: [json.loads(x) for x in res[i * 2]] for i, v in enumerate(vids) if res[i * 2]}

    async def update_driver_score(self, vid: str, model: str, score: float) -> None:
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.zadd(_lb_key("all"), {vid: score})
            pipe.zadd(_lb_key(f"model:{model}"), {vid: score})
            pipe.hset(_lb_key("meta"), "updated_at", datetime.now(timezone.utc).isoformat())
            await pipe.execute()

    async def leaderboard(self, vid: str, model: str, scope: str, top_n: int = 10) -> dict[str, Any]:
        key = _lb_key("all") if scope == "all" else _lb_key(f"model:{model}")
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.zrevrank(key, vid)
            pipe.zscore(key, vid)
            pipe.zcard(key)
            pipe.zrevrange(key, 0, top_n - 1, withscores=True)
            pipe.hget(_lb_key("meta"), "updated_at")
            rank0, score, total, top, updated = await pipe.execute()
        start, around = 0, []
        if rank0 is not None:
            start = max(rank0 - 2, 0)
            around = await self.r.zrevrange(key, start, rank0 + 2, withscores=True)
        return _lb_result(scope, rank0, score, total, top, start, around, updated)

    async def seed_population(self, count: int, extra_models: list[str] | None = None) -> int:
        all_scores, per_model = _population(count, extra_models)
        async with self.r.pipeline(transaction=False) as pipe:
            if all_scores:
                pipe.zadd(_lb_key("all"), all_scores)
            for m, scores in per_model.items():
                if scores:
                    pipe.zadd(_lb_key(f"model:{m}"), scores)
            await pipe.execute()
        return count

    async def get_json(self, key: str):
        raw = await self.r.get(PREFIX + key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value, ttl_s: int) -> None:
        await self.r.set(PREFIX + key, json.dumps(value, default=str), ex=ttl_s)

    async def reset(self) -> None:
        keys = [k async for k in self.r.scan_iter(match=PREFIX + "*", count=500)]
        if keys:
            await self.r.delete(*keys)


# ============================================================================= memory
class MemoryStore:
    """Same behaviour as RedisStore, kept in this process's memory."""
    kind = "memory"

    def __init__(self) -> None:
        self._latest: dict[str, str] = {}
        self._commands: dict[str, list[dict]] = {}
        self._boards: dict[str, dict[str, float]] = {}
        self._sorted: dict[str, tuple[list[tuple[str, float]], dict[str, int]]] = {}
        self._json: dict[str, tuple[float, str]] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._updated_at: str | None = None

    async def connect(self, url: str) -> None:
        return None

    async def close(self) -> None:
        return None

    async def set_latest(self, vid: str, state: dict[str, Any]) -> None:
        self._latest[vid] = json.dumps(state, default=str)   # store a copy, like Redis would

    async def get_latest(self, vid: str) -> dict[str, Any] | None:
        raw = self._latest.get(vid)
        return json.loads(raw) if raw else None

    async def get_latest_many(self, vids: list[str]) -> dict[str, dict[str, Any] | None]:
        return {v: await self.get_latest(v) for v in vids}

    async def publish(self, msg_type: str, vehicle_id: str, data: Any) -> None:
        payload = json.dumps({"type": msg_type, "vehicle_id": vehicle_id, "data": data}, default=str)
        for q in list(self._subscribers):
            if q.qsize() < 1000:          # a stuck consumer must never block ingest
                q.put_nowait(payload)

    async def listen(self) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    async def push_command(self, vid: str, command: dict[str, Any]) -> None:
        self._commands.setdefault(vid, []).append(command)

    async def pop_commands(self, vids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {v: self._commands.pop(v) for v in vids if self._commands.get(v)}

    def _zadd(self, key: str, scores: dict[str, float]) -> None:
        self._boards.setdefault(key, {}).update(scores)
        self._sorted.pop(key, None)            # re-sorted lazily on the next read

    def _ranked(self, key: str) -> tuple[list[tuple[str, float]], dict[str, int]]:
        if key not in self._sorted:
            # same order as Redis ZREVRANGE: score descending, then member descending
            items = sorted(self._boards.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
            self._sorted[key] = (items, {m: i for i, (m, _) in enumerate(items)})
        return self._sorted[key]

    async def update_driver_score(self, vid: str, model: str, score: float) -> None:
        self._zadd(_lb_key("all"), {vid: score})
        self._zadd(_lb_key(f"model:{model}"), {vid: score})
        self._updated_at = datetime.now(timezone.utc).isoformat()

    async def leaderboard(self, vid: str, model: str, scope: str, top_n: int = 10) -> dict[str, Any]:
        key = _lb_key("all") if scope == "all" else _lb_key(f"model:{model}")
        items, index = self._ranked(key)
        rank0 = index.get(vid)
        score = self._boards.get(key, {}).get(vid)
        start, around = 0, []
        if rank0 is not None:
            start = max(rank0 - 2, 0)
            around = items[start:rank0 + 3]
        return _lb_result(scope, rank0, score, len(items), items[:top_n], start, around, self._updated_at)

    async def seed_population(self, count: int, extra_models: list[str] | None = None) -> int:
        all_scores, per_model = _population(count, extra_models)
        self._zadd(_lb_key("all"), all_scores)
        for m, scores in per_model.items():
            if scores:
                self._zadd(_lb_key(f"model:{m}"), scores)
        return count

    async def get_json(self, key: str):
        hit = self._json.get(key)
        if not hit or hit[0] < time.monotonic():
            self._json.pop(key, None)
            return None
        return json.loads(hit[1])

    async def set_json(self, key: str, value, ttl_s: int) -> None:
        self._json[key] = (time.monotonic() + ttl_s, json.dumps(value, default=str))

    async def reset(self) -> None:
        self._latest.clear()
        self._commands.clear()
        self._boards.clear()
        self._sorted.clear()
        self._json.clear()


# ============================================================================= facade
class Cache:
    """Picks the backend from the URL; everything else in the app just uses `cache.<method>`."""

    def __init__(self) -> None:
        self._impl: RedisStore | MemoryStore | None = None

    async def connect(self, url: str) -> None:
        self._impl = MemoryStore() if url.strip().lower().startswith("memory") else RedisStore()
        await self._impl.connect(url)

    @property
    def kind(self) -> str:
        return self._impl.kind if self._impl else "none"

    def __getattr__(self, name: str):
        impl = self.__dict__.get("_impl")
        if impl is None:
            raise RuntimeError("cache not connected")
        return getattr(impl, name)


cache = Cache()
