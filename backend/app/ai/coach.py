"""The four GenAI features (trip coach, alert diagnosis, chat, weekly report), all built
the same way (telemetry-grounded RAG):

    1. gather FACTS from our own data   (trip events, live sensor values, trends)  - exact numbers
    2. RETRIEVE guidance passages        (hybrid search over the knowledge base)   - expert advice
    3. GENERATE with Gemini              (facts + passages + strict instructions)  - explanation
    4. LOG + CACHE the answer            (auditability, and saves free-tier quota)

The novelty vs. a normal chatbot: the question is answered with *this* car's real
numbers, and the LLM is told it may not invent any. Without an API key, step 3 is
replaced by a deterministic template, so every feature still works offline.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from ..cache import cache
from ..db import db
from ..engine.health import RULES_BY_CODE, fmt_duration
from ..engine.scoring import EVENT_INFO, penalty_breakdown, score_band
from ..geo import describe_location
from ..services import battery_trend
from ..services.analytics import driving_analysis, period_totals
from .llm import LLMError, llm
from .retriever import hybrid_search, multi_search

log = logging.getLogger("drivesense.coach")

SYSTEM_PROMPT = """You are DriveSense Coach, the AI assistant inside a connected-car app.
You help drivers understand their driving score and their vehicle's health.

Rules:
- Ground every statement in the TELEMETRY FACTS or REFERENCE PASSAGES you are given.
- Never invent numbers, events, places or causes that are not in the facts.
- When you use a reference passage, cite it inline like [S1] or [S2].
- Be specific: point to the actual events (time into trip, speeds, g-forces) from the facts.
- Be warm, concise and practical. Use short markdown headings (###) and bullet points.
  Keep the answer under 200 words unless the task says otherwise.
- Safety first: if something could be unsafe, say so plainly and recommend a qualified technician.
  You are an assistant, not a certified mechanic."""


class NotFound(Exception):
    pass


# One focused retrieval query per driving event (query decomposition)
EVENT_QUERIES = {
    "HB": "harsh braking - how to brake smoothly and early, look ahead, following distance",
    "HA": "harsh acceleration - how to accelerate smoothly and gradually",
    "HT": "harsh turns - cornering speed, sideways force, slow in steady through",
    "ABT": "accelerating before a turn - ease off before the corner",
    "BAT": "braking after a turn - finish braking before steering",
    "OSL": "over the speed limit - stopping distance, how to stay within limits",
    "FAB": "frequent acceleration and braking - keep a steady flow and a bigger gap",
    "eco": "fuel economy - aggressive driving, idling and fuel use",
    "clean": "smooth safe driving habits that keep fuel use low",
}


def _model_tag() -> str:
    return llm.model if llm.enabled else "offline-template"


def _sources_payload(passages: list[dict]) -> list[dict]:
    return [{"tag": p["tag"], "title": p["title"], "section": p["section"], "source": p["source"],
             "collection": p["collection"], "via": p["via"]} for p in passages]


def _passages_block(passages: list[dict]) -> str:
    if not passages:
        return "(no reference passages found)"
    return "\n\n".join(f"[{p['tag']}] {p['title']} > {p['section']}\n{p['content']}" for p in passages)


def _first_sentences(text: str, n: int = 2) -> str:
    clean = " ".join(line.strip("-* ").strip() for line in text.splitlines() if line.strip())
    parts = clean.split(". ")
    return ". ".join(parts[:n]).rstrip(".") + "."


async def _generate(prompt: str, fallback: str) -> tuple[str, bool, str | None]:
    """Returns (answer, used_llm, error)."""
    if not llm.enabled:
        return fallback, False, None
    try:
        return await llm.generate(SYSTEM_PROMPT, prompt), True, None
    except LLMError as exc:
        log.warning("LLM failed, using template answer: %s", exc)
        return fallback, False, str(exc)


async def _log(feature: str, vehicle_id: str, ref_id: str | None, question: str | None, answer: str,
               sources: list[dict], used_llm: bool, started: float) -> int:
    latency = int((time.perf_counter() - started) * 1000)
    await db.pool.execute(
        """INSERT INTO ai_logs (feature, vehicle_id, ref_id, question, answer, sources, model, latency_ms, used_llm)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        feature, vehicle_id, ref_id, question, answer, sources, _model_tag() if used_llm else "offline-template",
        latency, used_llm)
    return latency


# =================================================================== 1. trip coach
async def explain_trip(trip_id: int, refresh: bool = False) -> dict:
    trip = await db.pool.fetchrow(
        "SELECT t.*, v.name AS vehicle_name, v.model FROM trips t JOIN vehicles v ON v.id = t.vehicle_id WHERE t.id = $1",
        trip_id)
    if not trip:
        raise NotFound("trip not found")
    if trip["coach_summary"] and not refresh and trip["coach_model"] == _model_tag():
        cached = json.loads(trip["coach_summary"])
        return {**cached, "cached": True}

    started = time.perf_counter()
    vid = trip["vehicle_id"]
    events = await db.pool.fetch(
        "SELECT time, type, lat, lon, speed_kph, value, detail FROM driving_events WHERE trip_id = $1 ORDER BY time",
        trip_id)
    base = await db.pool.fetchrow(
        """SELECT count(*) AS n, sum(score * distance_km) / NULLIF(sum(distance_km), 0) AS score,
                  sum(distance_km) / NULLIF(sum(fuel_used_l), 0) AS kmpl
           FROM trips WHERE vehicle_id = $1 AND id <> $2 AND start_time > $3""",
        vid, trip_id, trip["start_time"] - timedelta(days=7))
    breakdown = penalty_breakdown(trip["event_counts"], trip["distance_km"])

    # ---- facts
    lines = [
        f"Vehicle: {trip['vehicle_name']} ({trip['model']})",
        f"Trip: {trip['start_time']:%Y-%m-%d %H:%M} UTC, {trip['start_place']} -> {trip['end_place']}",
        f"Distance {trip['distance_km']:.2f} km, duration {fmt_duration(trip['duration_s'])}, "
        f"avg speed {trip['avg_speed_kph']:.1f} km/h, max speed {trip['max_speed_kph']:.0f} km/h, "
        f"fuel economy {trip['fuel_economy_kmpl'] or 'n/a'} km/l",
        f"Score: {trip['score']}/100 ({score_band(trip['score'])}). Formula: 100 minus weighted events per 10 km.",
    ]
    if breakdown:
        lines.append("Points lost: " + "; ".join(f"{b['name']} x{b['count']} = -{b['points']}" for b in breakdown))
    else:
        lines.append("Points lost: none - a clean trip.")
    if events:
        lines.append("Events (time into trip | type | details):")
        for e in events[:15]:
            offset = int((e["time"] - trip["start_time"]).total_seconds())
            lines.append(f"- {offset // 60:02d}:{offset % 60:02d} | {EVENT_INFO[e['type']]['name']} | "
                         f"{e['detail']} near {describe_location(e['lat'], e['lon'])}")
    if base and base["n"]:
        lines.append(f"Driver's other trips in the previous 7 days: {base['n']} trips, average score "
                     f"{base['score']:.0f}, fuel economy {base['kmpl'] or 0:.1f} km/l")
    facts = "\n".join(lines)

    # ---- retrieve: one focused query per costly event (+ fuel if this trip was thirstier)
    queries = {b["type"]: EVENT_QUERIES[b["type"]] for b in breakdown[:3]} or {"clean": EVENT_QUERIES["clean"]}
    eco_worse = bool(base and base["kmpl"] and trip["fuel_economy_kmpl"]
                     and trip["fuel_economy_kmpl"] < base["kmpl"] * 0.95)
    if eco_worse or len(queries) < 3:
        queries["eco"] = EVENT_QUERIES["eco"]
    passages = await multi_search(queries, ["coach"], per_query=1, k=4)

    prompt = (f"TASK: Explain this trip's driving score to the driver and give the 3 most useful things to "
              f"improve next time.\n\nTELEMETRY FACTS\n{facts}\n\nREFERENCE PASSAGES\n{_passages_block(passages)}"
              f"\n\nAnswer with these sections: ### Why you scored {trip['score']}  ### Top 3 fixes  ### Fuel impact")

    # ---- deterministic fallback
    fb = [f"### Why you scored {trip['score']}"]
    if breakdown:
        fb += [f"- **{b['name']}** x{b['count']} cost you **{b['points']} points**" for b in breakdown]
        first = events[0]
        off = int((first["time"] - trip["start_time"]).total_seconds())
        fb.append(f"- First event at {off // 60:02d}:{off % 60:02d}: {EVENT_INFO[first['type']]['name']} "
                  f"({first['detail']}).")
    else:
        fb.append("- No risky events detected - excellent, smooth driving.")
    fb.append("### Top 3 fixes")
    tips = breakdown[:3]
    for i, b in enumerate(tips):
        match = next((p for p in passages if p.get("for_key") == b["type"]), None)
        advice = f"{_first_sentences(match['content'], 2)} [{match['tag']}]" if match else EVENT_INFO[b["type"]]["tip"]
        fb.append(f"{i + 1}. **{b['name']}** ({b['count']}x): {advice}")
    if not tips:
        fb.append("1. Keep looking far ahead and keep your speed steady - it is working.")
    fb.append("### Fuel impact")
    if trip["fuel_economy_kmpl"] and base and base["kmpl"]:
        diff = trip["fuel_economy_kmpl"] - base["kmpl"]
        cmp = "about the same as" if abs(diff) < 0.1 else ("better than" if diff > 0 else "worse than")
        fb.append(f"This trip: {trip['fuel_economy_kmpl']:.1f} km/l, {cmp} your 7-day average of "
                  f"{base['kmpl']:.1f} km/l. Smooth acceleration and early braking are the biggest fuel savers.")
    else:
        fb.append("Smooth acceleration and early, gentle braking are the biggest fuel savers.")
    fallback = "\n".join(fb)

    answer, used_llm, error = await _generate(prompt, fallback)
    sources = _sources_payload(passages)
    latency = await _log("trip_coach", vid, str(trip_id), None, answer, sources, used_llm, started)
    result = {"answer": answer, "sources": sources, "used_llm": used_llm, "model": _model_tag() if used_llm else
              "offline-template", "facts": facts, "breakdown": breakdown, "latency_ms": latency, "llm_error": error}
    if used_llm or not llm.enabled:   # do not cache a fallback caused by a temporary API error
        await db.pool.execute("UPDATE trips SET coach_summary = $2, coach_model = $3 WHERE id = $1",
                              trip_id, json.dumps(result, default=str), _model_tag())
    return {**result, "cached": False}


# ============================================================ 2. health diagnosis
SIGNAL_COLUMNS = {"coolant_c", "battery_v", "tire_fl", "tire_fr", "tire_rl", "tire_rr"}


async def diagnose_alert(alert_id: int, refresh: bool = False) -> dict:
    alert = await db.pool.fetchrow(
        "SELECT a.*, v.name AS vehicle_name, v.model FROM health_alerts a JOIN vehicles v ON v.id = a.vehicle_id "
        "WHERE a.id = $1", alert_id)
    if not alert:
        raise NotFound("alert not found")
    if alert["diagnosis"] and not refresh and alert["diagnosis_model"] == _model_tag():
        return {**json.loads(alert["diagnosis"]), "cached": True}

    started = time.perf_counter()
    vid = alert["vehicle_id"]
    col = alert["signal"] if alert["signal"] in SIGNAL_COLUMNS else None
    trend = None
    if col:  # column name comes from a whitelist, never from user input
        trend = await db.pool.fetchrow(
            f"""SELECT min({col}) AS mn, max({col}) AS mx, avg({col}) AS av,
                       regr_slope({col}, extract(epoch FROM time)) * 60 AS per_min, count(*) AS n
                FROM telemetry WHERE vehicle_id = $1 AND time > now() - INTERVAL '3 minutes'""", vid)
    latest = await cache.get_latest(vid) or {}
    rule = RULES_BY_CODE.get(alert["code"])

    lines = [
        f"Vehicle: {alert['vehicle_name']} ({alert['model']})",
        f"Alert: {alert['title']} | severity {alert['severity']} | type {alert['kind']} | status {alert['status']}",
        f"Details: {alert['message']}",
    ]
    if rule:
        lines.append(f"Healthy band for this signal: {rule.band[0]:g}-{rule.band[1]:g} {rule.unit}; "
                     f"warning {rule.warn:g}, critical {rule.crit:g}")
    if trend and trend["n"]:
        lines.append(f"Last 3 minutes: min {trend['mn']:.2f}, max {trend['mx']:.2f}, avg {trend['av']:.2f}, "
                     f"trend {trend['per_min'] or 0:+.3f} per minute ({trend['n']} samples)")
    if latest:
        lines.append(f"Live snapshot: ignition {'on' if latest.get('ignition') else 'off'}, speed "
                     f"{latest.get('speed_kph')} km/h, coolant {latest.get('coolant_c')} °C, battery "
                     f"{latest.get('battery_v')} V, tyres {latest.get('tires')}")
    facts = "\n".join(lines)
    passages = await hybrid_search(f"{alert['title']} {alert['message']}", ["service"], k=4)
    prompt = (f"TASK: The driver tapped 'Diagnose' on a vehicle-health alert. Explain what is happening, the most "
              f"likely causes, whether it is safe to keep driving, and exactly what to do now.\n\n"
              f"TELEMETRY FACTS\n{facts}\n\nREFERENCE PASSAGES\n{_passages_block(passages)}\n\n"
              f"Answer with these sections: ### What is happening  ### Likely causes  ### Safe to drive?  ### Do this now")

    fb = ["### What is happening", alert["message"], "### What the service guide says"]
    for p in passages[:2]:
        fb.append(f"- **{p['section']}:** {_first_sentences(p['content'], 2)} [{p['tag']}]")
    fb += ["### Safe to drive?",
           "Stop safely as soon as possible and do not continue driving." if alert["severity"] == "critical" else
           "Drive gently to a safe place and have it checked soon; watch the gauge.",
           "_Offline answer (no Gemini key configured): built from the alert data and the service guide._"]
    answer, used_llm, error = await _generate(prompt, "\n".join(fb))
    sources = _sources_payload(passages)
    latency = await _log("diagnose", vid, str(alert_id), None, answer, sources, used_llm, started)
    result = {"answer": answer, "sources": sources, "used_llm": used_llm,
              "model": _model_tag() if used_llm else "offline-template", "facts": facts, "latency_ms": latency,
              "llm_error": error}
    if used_llm or not llm.enabled:
        await db.pool.execute("UPDATE health_alerts SET diagnosis = $2, diagnosis_model = $3 WHERE id = $1",
                              alert_id, json.dumps(result, default=str), _model_tag())
    return {**result, "cached": False}


# =================================================================== 3. coach chat
async def chat(vehicle_id: str, question: str, history: list[dict]) -> dict:
    veh = await db.pool.fetchrow("SELECT * FROM vehicles WHERE id = $1", vehicle_id)
    if not veh:
        raise NotFound("vehicle not found")
    started = time.perf_counter()
    analysis = await driving_analysis(vehicle_id, 7)
    trips = await db.pool.fetch(
        """SELECT id, start_time, distance_km, duration_s, score, fuel_economy_kmpl, event_counts
           FROM trips WHERE vehicle_id = $1 ORDER BY start_time DESC LIMIT 5""", vehicle_id)
    latest = await cache.get_latest(vehicle_id) or {}
    alerts = (latest.get("health") or {}).get("alerts", [])

    s = analysis["stats"]
    lines = [f"Vehicle: {veh['name']} ({veh['model']}), driver {veh['driver'] or 'unknown'}",
             f"Last 7 days: {s['trips']} trips, {s['distance_km']} km, score {analysis['score']} "
             f"({analysis['band']}), fuel economy {s['fuel_economy_kmpl']} km/l, max speed {s['max_speed_kph']} km/h",
             "Event counts (7 days): " + ", ".join(f"{e['name']} {e['count']}" for e in analysis["events"])]
    for t in trips:
        lines.append(f"- Trip #{t['id']} {t['start_time']:%a %d %b %H:%M}: {t['distance_km']:.1f} km, score "
                     f"{t['score']}, {t['fuel_economy_kmpl'] or 'n/a'} km/l, events {t['event_counts'] or 'none'}")
    if latest:
        lines.append(f"Right now: {'driving' if latest.get('ignition') else 'parked'} at {latest.get('place')}, "
                     f"fuel {latest.get('fuel_pct')}%, range {latest.get('range_km')} km, battery "
                     f"{latest.get('battery_v')} V")
    lines.append("Active health alerts: " + ("; ".join(f"{a['title']} - {a['message']}" for a in alerts)
                                             if alerts else "none"))
    facts = "\n".join(lines)
    frequent = [e["name"] for e in sorted(analysis["events"], key=lambda e: -e["count"]) if e["count"]][:2]
    # The question gets its own search (2 passages) so context terms cannot drown it out;
    # active alerts and the most frequent event add one passage each.
    queries = {"question": question}
    for a in alerts[:2]:
        queries[f"alert:{a['code']}"] = a["title"]
    for name in frequent[:1]:
        queries[f"event:{name}"] = name
    passages = await multi_search(queries, ["coach", "service"], per_query={"question": 2}, k=4)
    convo = "\n".join(f"{h['role'].upper()}: {h['text']}" for h in history[-6:])
    prompt = (f"TELEMETRY FACTS\n{facts}\n\nREFERENCE PASSAGES\n{_passages_block(passages)}\n\n"
              f"CONVERSATION SO FAR\n{convo or '(none)'}\n\nDRIVER QUESTION: {question}\n\n"
              f"Answer the question directly using the facts above.")
    fb = ["Here is what your data shows:",
          f"- Last 7 days: **{s['trips']} trips, {s['distance_km']} km, score {analysis['score']} "
          f"({analysis['band']})**, fuel economy {s['fuel_economy_kmpl']} km/l."]
    worst = max(analysis["events"], key=lambda e: e["count"])
    if worst["count"]:
        fb.append(f"- Your most frequent event is **{worst['name']}** ({worst['count']}x). {worst['message']}")
    if passages:
        fb.append(f"- From the guide: {_first_sentences(passages[0]['content'], 2)} [{passages[0]['tag']}]")
    fb.append("_Offline answer - add a Gemini API key for full conversational answers._")
    answer, used_llm, error = await _generate(prompt, "\n".join(fb))
    sources = _sources_payload(passages)
    latency = await _log("chat", vehicle_id, None, question, answer, sources, used_llm, started)
    return {"answer": answer, "sources": sources, "used_llm": used_llm,
            "model": _model_tag() if used_llm else "offline-template", "latency_ms": latency, "llm_error": error}



# ================================================================ 4. weekly report
async def weekly_report(vehicle_id: str, refresh: bool = False) -> dict:
    """One-click AI analysis of the car's week: driving, fuel and health together."""
    veh = await db.pool.fetchrow("SELECT * FROM vehicles WHERE id = $1", vehicle_id)
    if not veh:
        raise NotFound("vehicle not found")
    cache_key = f"report:{vehicle_id}:{_model_tag()}"
    if not refresh:
        cached = await cache.get_json(cache_key)
        if cached:
            return {**cached, "cached": True}

    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    week = await driving_analysis(vehicle_id, 7)
    prev = await period_totals(vehicle_id, now - timedelta(days=14), now - timedelta(days=7))
    best = await db.pool.fetchrow(
        """SELECT id, start_time, score, distance_km, start_place, end_place FROM trips
           WHERE vehicle_id = $1 AND start_time > $2 ORDER BY score DESC, distance_km DESC LIMIT 1""",
        vehicle_id, now - timedelta(days=7))
    worst = await db.pool.fetchrow(
        """SELECT id, start_time, score, distance_km, start_place, end_place, event_counts FROM trips
           WHERE vehicle_id = $1 AND start_time > $2 ORDER BY score ASC, distance_km DESC LIMIT 1""",
        vehicle_id, now - timedelta(days=7))
    resolved = await db.pool.fetchval(
        "SELECT count(*) FROM health_alerts WHERE vehicle_id = $1 AND status = 'resolved' AND resolved_at > $2",
        vehicle_id, now - timedelta(days=7))
    latest = await cache.get_latest(vehicle_id) or {}
    alerts = (latest.get("health") or {}).get("alerts", [])
    trend = battery_trend.evaluate_trend(await battery_trend.daily_resting_voltage(vehicle_id))

    s = week["stats"]
    events = sorted((e for e in week["events"] if e["count"]), key=lambda e: -e["count"])
    lines = [
        f"Vehicle: {veh['name']} ({veh['model']}), odometer {latest.get('odometer_km', 'n/a')} km, "
        f"fuel {latest.get('fuel_pct', 'n/a')}%, range {latest.get('range_km', 'n/a')} km",
        f"This week: {s['trips']} trips, {s['distance_km']} km, driving time {fmt_duration(s['travel_time_s'])}, "
        f"weekly score {week['score']} ({week['band']}), fuel economy {s['fuel_economy_kmpl']} km/l, "
        f"max speed {s['max_speed_kph']} km/h",
        f"Previous week: {prev['trips']} trips, {prev['km']} km, score {prev['score']}, fuel economy {prev['kmpl']} km/l",
        "Events this week (count, per 100 km): " + (", ".join(
            f"{e['name']} {e['count']} ({e['per_100km']})" for e in events) or "none"),
    ]
    if best:
        lines.append(f"Best trip: #{best['id']} {best['start_time']:%a %H:%M}, score {best['score']}, "
                     f"{best['distance_km']:.1f} km, {best['start_place']} -> {best['end_place']}")
    if worst and (not best or worst["id"] != best["id"]):
        lines.append(f"Lowest-scoring trip: #{worst['id']} {worst['start_time']:%a %H:%M}, score {worst['score']}, "
                     f"{worst['distance_km']:.1f} km, events {worst['event_counts'] or 'none'}")
    lines.append("Active health alerts: " + ("; ".join(f"{a['title']} - {a['message']}" for a in alerts)
                                             if alerts else "none"))
    lines.append(f"Health alerts resolved this week: {resolved}")
    if trend:
        lines.append(f"Battery trend prediction: {trend.message}")
    facts = "\n".join(lines)

    queries: dict[str, str] = {}
    for e in events[:2]:
        queries[e["type"]] = EVENT_QUERIES[e["type"]]
    queries["eco"] = EVENT_QUERIES["eco"]
    if trend:
        queries["battery"] = "battery resting voltage slowly falling, battery test and replacement"
    for a in [a for a in alerts if a["code"] != "battery_trend"][:1]:
        queries["alert"] = f"{a['title']} what to do"
    passages = await multi_search(queries, ["coach", "service"], per_query=1, k=5)

    prompt = (f"TASK: Write the owner's weekly report for their car. Cover driving, fuel and the car's health, "
              f"compare with last week where the facts allow, and end with the single most useful thing to focus "
              f"on next week. Up to 250 words.\n\nTELEMETRY FACTS\n{facts}\n\nREFERENCE PASSAGES\n"
              f"{_passages_block(passages)}\n\nAnswer with these sections: ### Your week in one line  ### Driving  "
              f"### Fuel  ### Car health  ### Focus for next week")

    # ---- deterministic fallback
    def delta(a, b, unit=""):
        if a is None or b is None:
            return ""
        d = a - b
        return f" ({'+' if d >= 0 else ''}{d:.1f}{unit} vs last week)"

    fb = ["### Your week in one line",
          f"{s['trips']} trips and {s['distance_km']} km, weekly score **{week['score']} ({week['band']})**"
          f"{delta(week['score'], prev['score'])}.",
          "### Driving"]
    if events:
        top = events[0]
        match = next((p for p in passages if p.get("for_key") == top["type"]), None)
        fb.append(f"- Most frequent event: **{top['name']}** ({top['count']}x, {top['per_100km']} per 100 km).")
        if match:
            fb.append(f"- {_first_sentences(match['content'], 2)} [{match['tag']}]")
    else:
        fb.append("- No risky driving events this week.")
    if worst and best and worst["id"] != best["id"]:
        fb.append(f"- Best trip scored {best['score']}; the lowest scored {worst['score']}.")
    fb.append("### Fuel")
    fb.append(f"- {s['fuel_economy_kmpl']} km/l this week{delta(s['fuel_economy_kmpl'], prev['kmpl'], ' km/l')}.")
    fb.append("### Car health")
    if alerts:
        fb += [f"- **{a['title']}:** {a['message']}" for a in alerts]
    else:
        fb.append("- No active alerts. All monitored systems are in their healthy range.")
    if resolved:
        fb.append(f"- {resolved} alert(s) were resolved during the week.")
    fb.append("### Focus for next week")
    urgent = [a for a in alerts if a["code"] != "battery_trend" and a["severity"] in ("critical", "warning")]
    if urgent:
        fb.append(f"- Deal with **{urgent[0]['title'].lower()}** first: have it checked before long drives.")
    elif trend:
        fb.append("- Get the 12 V battery tested before it leaves you unable to start.")
    elif events:
        fb.append(f"- Work on **{events[0]['name'].lower()}**: {EVENT_INFO[events[0]['type']]['tip']}")
    else:
        fb.append("- Keep it up: smooth, steady driving is saving you fuel.")
    fb.append("_Offline report - add a Gemini API key for a written analysis._")

    answer, used_llm, error = await _generate(prompt, "\n".join(fb))
    sources = _sources_payload(passages)
    latency = await _log("weekly_report", vehicle_id, None, None, answer, sources, used_llm, started)
    result = {"answer": answer, "sources": sources, "used_llm": used_llm,
              "model": _model_tag() if used_llm else "offline-template", "facts": facts,
              "latency_ms": latency, "llm_error": error, "generated_at": now.isoformat()}
    if used_llm or not llm.enabled:
        await cache.set_json(cache_key, result, ttl_s=900)
    return {**result, "cached": False}
