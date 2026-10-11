# Sensor simulator - plays your car

`fake_sensors.py` stands in for the telematics hardware of **your car**. It generates random
but physically plausible sensor data once per second and sends it to the backend's device API
(`POST /api/ingest`), exactly as a real in-car unit would. It uses only the Python standard
library, so there is nothing to install.

```bash
python fake_sensors.py --reset --seed-days 14            # wipe data, back-fill two weeks, then stream live
python fake_sensors.py                                    # stream live, continuing from the last known state
python fake_sensors.py --driver Sam --model Escape --plate "AB 123 CD"   # describe your car
python fake_sensors.py --help                             # all options
```

## What it simulates

**Sensors**
- GPS, heading, speed, posted speed limit, RPM;
- fuel %, odometer;
- battery voltage, coolant temperature, four tyre pressures;
- headlights, door locks and alarm.

**Driving**
- Each trip is driven in a random style: 25% calm, 55% normal, 20% aggressive. Scores, fuel economy and coaching therefore vary from trip to trip, like a real owner's.
- The driver model follows the speed limit (or ignores it), slows for curves (`v = sqrt(grip / curvature)`), and stops at random lights. Some lights are noticed late, which causes harsh braking.
- An aggressive driver tailgates, speeds up before turns and corners faster.

**Battery wear**
- By default the 12 V battery loses 0.025 V per day, so the website's multi-day battery prediction has something to find.
- Turn this off with `--no-battery-wear`.

**Faults**

| Fault | When it shows | What happens |
|---|---|---|
| Coolant leak | while driving | temperature climbs |
| Alternator fault | while driving | charging voltage drops |
| Slow puncture | parked or driving | one tyre loses pressure |
| Weak battery | while parked | resting voltage drops |

Faults happen at random (3% of trips) or are injected from the website's **Simulator** panel.

**Commands:** the backend's reply to each upload carries queued commands (remote lock, alarm, headlights, demo trips and faults). The simulator applies them within a second.

**Store-and-forward:** if the backend is unreachable, readings are buffered and re-sent.

**Resume:** on restart it continues from the backend's last known state: position, fuel, odometer and battery wear.

**Routes:** real roads around Dearborn, Michigan, fetched once from the public OSRM (OpenStreetMap) router and cached in `.route_cache.json`. When offline it uses a synthetic street grid. To use your own area, edit the `PLACES` list at the top of the file (and the matching list in `backend/app/geo.py`).
