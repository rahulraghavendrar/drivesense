"""The in-process hot layer (REDIS_URL=memory://) must behave like the Redis one."""
import asyncio

from app.cache import MemoryStore
from app.db import _connect_kwargs


def run(coro):
    return asyncio.run(coro)


def test_latest_state_is_a_copy():
    async def go():
        s = MemoryStore()
        state = {"speed_kph": 50}
        await s.set_latest("car", state)
        state["speed_kph"] = 99                     # caller mutates its own dict afterwards
        assert (await s.get_latest("car"))["speed_kph"] == 50
        assert await s.get_latest_many(["car", "other"]) == {"car": {"speed_kph": 50}, "other": None}
    run(go())


def test_commands_are_drained_once():
    async def go():
        s = MemoryStore()
        await s.push_command("car", {"action": "lock"})
        await s.push_command("car", {"action": "alarm_on"})
        assert await s.pop_commands(["car", "x"]) == {"car": [{"action": "lock"}, {"action": "alarm_on"}]}
        assert await s.pop_commands(["car"]) == {}
    run(go())


def test_leaderboard_rank_and_neighbours():
    async def go():
        s = MemoryStore()
        await s.seed_population(1000, ["Ranger"])
        await s.update_driver_score("my-car", "Ranger", 101.0)      # above every synthetic driver
        lb = await s.leaderboard("my-car", "Ranger", "all")
        assert lb["rank"] == 1 and lb["total"] == 1001 and lb["top"][0]["member"] == "my-car"
        await s.update_driver_score("my-car", "Ranger", 0.0)        # re-score: board re-sorts
        lb = await s.leaderboard("my-car", "Ranger", "all")
        assert lb["rank"] == 1001 and lb["ahead_of_pct"] == 0
        assert [r["rank"] for r in lb["around"]] == [999, 1000, 1001]
        model = await s.leaderboard("my-car", "Ranger", "model")
        assert model["total"] < 1001 and model["rank"] == model["total"]
    run(go())


def test_publish_reaches_every_listener():
    async def go():
        s = MemoryStore()
        got: list[list[str]] = [[], []]

        async def listen(i):
            async for msg in s.listen():
                got[i].append(msg)
                if len(got[i]) == 2:
                    return

        tasks = [asyncio.create_task(listen(0)), asyncio.create_task(listen(1))]
        await asyncio.sleep(0)                       # let both subscribe
        await s.publish("telemetry", "car", {"v": 1})
        await s.publish("notification", "car", {"title": "x"})
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert got[0] == got[1] and '"type": "notification"' in got[0][1]
    run(go())


def test_json_cache_expires():
    async def go():
        s = MemoryStore()
        await s.set_json("report", {"a": 1}, ttl_s=60)
        assert await s.get_json("report") == {"a": 1}
        await s.set_json("report", {"a": 1}, ttl_s=-1)
        assert await s.get_json("report") is None
    run(go())


def test_transaction_pooler_disables_prepared_statements():
    assert _connect_kwargs("postgresql://u:p@aws-0-x.pooler.supabase.com:6543/postgres") == {"statement_cache_size": 0}
    assert _connect_kwargs("postgresql://u:p@aws-0-x.pooler.supabase.com:5432/postgres") == {}
