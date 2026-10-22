# Learning Log — PitWall

Evidence of what was measured, changed and verified.  Every claim below is
backed by command output that was actually run.

---

## Session 2026-10-21 — Track layout, driver tracking, replay and API correctness

### 1. Diagnosis (measured, not assumed)

| # | Symptom | Root cause | Measurement |
|---|---|---|---|
| 1 | Circuit layout garbage | `build_track_outlines.py` selected the longest contiguous run of `/location` samples. A session trace also contains the garage and the pit lane, so that run is not a circuit. | The old outline's closing gap was **11,509 feed units** on a track whose own bounding box spans 18,136 units. Stride sampling amplified inter-sample gaps to **1,394 units** (248 of them above the 450-unit threshold). |
| 2 | Circuit also mirrored | Raw `/location` `y` points **up**; SVG's `y` points **down**, and the old `mapY` never flipped it. | Signed area of the canonical outline in raw coordinates = **+93,965,282** (counter-clockwise when `y` is up). COTA is officially a **counter-clockwise** circuit (Wikipedia). Fitting the rendered map against the official F1 map: `flip_y=True` residual **34.3 px** vs `flip_y=False` **116.5 px**. |
| 3 | Driver dots not on the track | Dots were placed by CSS percentage index arithmetic (`left: 18 + (i*8)%65 %`); the API used OpenF1 `/position`, which has **no coordinates at all**. | `/position` fields = `date, driver_number, meeting_key, position, session_key`. |
| 4 | Replay of an older Austin GP broken | Session **9616 is the 2024 Sprint** (19 laps). The UI claimed 56 laps / 308.405 km, which is the **Race** = session **9617**. | OpenF1 `/sessions` for meeting 1247: `9616 Race Sprint` (19 laps) and `9617 Race Race` (56 laps). |
| 5 | Per-driver API calls unreliable | 20 concurrent per-driver requests trip the free-tier limiter. **One date-windowed request returns the whole field.** | 6 rapid requests → **HTTP 429** on the 5th. A 2 s windowed `/location` query returns **all 20 drivers, 140 rows, 0.73 s**. |
| 6 | OpenF1 query syntax | `date>=` / `date<=` are **rejected**; `date>` / `date<` work through an `httpx` `params` dict. | `date>` → 200 (2,112 rows), `date>=` → **404**, `date<` → 200, `date<=` → **404**. |
| 7 | Whole-session all-driver query | Returns HTTP 422 ("too much data at once"). | `/location?session_key=9617` → **422**; per-driver → 35,971 rows, 2.7 s. |
| 8 | Fake data everywhere | Fabricated telemetry, drivers, positions, chat numbers and a hardcoded `flag: "GREEN"`. | The old `/api/telemetry` fallback computed `speed = 300 + (driver_number * 3) % 35`. |

### 2. Coordinate-system finding (the key one)

OpenF1 `/location` `x`/`y` and FastF1's canonical circuit outline are in the
**same coordinate system**, so a driver dot and the track path can share one
projection:

```
$ python scripts/build_track_outlines.py
Austin (session_key=9617, year=2024)...
  canonical outline: 853 pts, 20 corners
  derived 901 pts from lap 51 (max deviation 19.5 feed units)
Silverstone (session_key=9558, year=2024)...
  derived 901 pts from lap 28 (max deviation 22.6 feed units)
Monza (session_key=9590, year=2024)...
  derived 901 pts from lap 20 (max deviation 23.6 feed units)
Spa (session_key=9574, year=2024)...
  derived 901 pts from lap 36 (max deviation 22.3 feed units)
All circuit outlines built and validated.
```

19–24 feed units is ≈ **2 m** on an 18,136-unit (1,842 m) circuit.

### 3. Changes made

| Area | Change |
|---|---|
| `data/track.py` (new) | Canonical outline loader, **shared** `TrackProjection` (one transform for outline *and* dots, `y` flipped for SVG), and `outline_from_session` which isolates a real racing lap via the `/laps` feed and validates it against the canonical outline. |
| `data/openf1.py` (new) | Throttled, disk-cached OpenF1 client: 1 shared `httpx.AsyncClient`, minimum-interval throttle, exponential backoff with jitter on 429/5xx, on-disk cache. Returns `None`/empty — never invented rows. |
| `data/session_pack.py` (new) | Downloads a complete session once (location, car_data, laps, intervals, stints, position, race_control, drivers) into a local cache for replay. |
| `realtime/session_replay.py` (new) | Replays the cached session frame by frame: positions, telemetry, gaps, tyres, lap times and **real race-control flags**. |
| `realtime/live.py` (new) | Live polling for a running session: **two requests per tick** (one windowed `/location`, one windowed `/car_data`), each covering the whole field. |
| `api/app.py` | Rewritten. Every endpoint returns real data or `{"available": false, "reason": ...}`. Paths resolve against the repository root, not the CWD. |
| `api/strategy.py` (new) | Chat answers come from the simulation engine and carry `"source": "simulation"`; without a calibration bundle it says so. |
| `engine/simulator.py` | Pit loss no longer double-counted on the final stint; degradation now comes from a fitted model; `p_beat_rival` is computed from a rival simulation instead of being `0.5`. |
| `engine/calibration.py` | A missing bundle now raises instead of silently returning defaults, so an uncalibrated circuit cannot look calibrated. |
| `engine/championship.py` | Driver numbers corrected against the official 2026 entry list (VER 1→3, NOR 4→1, LIN 14→41, duplicate 14 removed); title odds computed by Monte-Carlo instead of hard-coded. |
| `engine/pit_loss.py` | Reads the column the loader actually writes (`LapTime`), and matches `session_key` against the file stem. |
| `realtime/replay.py` | Parquet filename matching fixed; `NaN` positions/tyre ages no longer raise or become the string `"nan"`. |
| `realtime/triggers.py` | Undercut alert no longer fires for a negative gap; alert de-duplication no longer re-fires on every lap of the window. |
| `llm/provider.py` | Raises on an unparsable model response instead of returning `None` as an `Insight`. |
| `web/src/components/TrackMap.tsx` (new) | Draws the outline and the driver markers **in the same projected coordinate space**. |
| `web/src/App.tsx` | Rewritten on real data: dots positioned from `view_x`/`view_y`, telemetry, timing tower, replay controls, honest unavailable states. |
| Removed | `SupabaseAuthModal.tsx` (faked sign-in, no Supabase call) and `F1CarVisualizer.tsx` (hardcoded car spec). |
| `pyproject.toml` | All runtime dependencies declared; `include = ["pitwall*"]` so `api`, `engine`, `llm` and `eval` are actually packaged (their `__init__.py` files were missing). |

### 4. Verification

```
$ python -m pytest -q
47 passed, 13 warnings in 4.92s

$ python -m ruff check .
All checks passed!

$ cd web && npm run build
✓ 19 modules transformed.
dist/assets/index-BeucJalq.js   245.60 kB │ gzip:  75.75 kB
✓ built in 150ms
```

**API (live server on :8000)**

```
$ curl /api/sessions/current
{"available":true,"source":"openf1.session_pack","mode":"REPLAY","session_key":9617,
 "circuit":"Austin","year":2024,"meeting_name":"Race","session_type":"Race",
 "date_start":"2024-10-20T19:00:00+00:00","total_laps":56,"status":"REPLAY_ACTIVE",
 "next_session":{"session_key":11389,"circuit_short_name":"Austin", ...}}

$ curl /api/circuits/Austin/outline
available=True source=openf1.location points=901 corners=20
viewBox=1000.0x750.0 flip_y=True scale=0.05207182211381334
```

**Driver dots land on the track** (`/api/positions?at=300`, lap 3, YELLOW flag):

```
  P DRV    view_x   view_y  speed gear  thr  brk  drs        gap     tyre  dist_to_path
  1 LEC    304.73   438.64  154.0    4 48.0  0.0    1        0.0 MEDIUM/2         0.85
  2 VER    336.65   422.71  151.0    4  0.0  0.0    1      1.517 MEDIUM/2         0.71
  3 SAI    349.62   403.76  163.0    4 61.0  0.0    1      2.619 MEDIUM/2         1.19
  ...
max distance from any driver dot to the track path: 1.54 view units = 29.6 feed units
(1 view unit = 19.2 feed units = 1.95 m)      ->  max 3.0 m, typical 1.5 m
```

**In the browser** (live view, DOM inspection of the rendered SVG):

```
{ "trackPoints": 901, "driverMarkers": 20,
  "maxDeviation": "7.36", "medianDeviation": "0.95" }
```

**Real race control is used** — the replay shows the genuine YELLOW flag at
lap 3 of the 2024 United States Grand Prix, and GREEN before and after.

**Calibration fitted from real data** (`scripts/calibrate_circuit.py`):

```json
{ "circuit": "Austin", "year": 2024,
  "pit_loss_s": 21.35,            <- published COTA pit loss is ~18-25 s
  "base_pace_s": 98.672,          <- median 10th-percentile lap, real COTA race pace
  "fuel_effect_s_per_kg": 0.00555,
  "tyre_cliff_medium": 31.0,
  "degradation": { "MEDIUM": [0.03843, 0.0, 31.0] },
  "source": "fitted from OpenF1 session 9617" }
```

### 5. Still open

* **D1** — OpenF1 paid real-time tier: not required for the replay, needed for
  true live tracking (the `/api/live/snapshot` endpoint is implemented and
  issues 2 windowed requests per tick).
* **D2** — OmniRoute API key still missing (gateway returns 401), so the LLM
  path is untested; the app runs on the deterministic engine fast path.
* **D3** — Supabase auth was **removed** rather than faked; a real
  implementation can be re-added once a project is provisioned.
* **D4** — Priority circuits for calibration: Austin 2024 is done; Silverstone,
  Monza and Spa 2024 have outlines but no calibration bundle yet.
* `docs/ARCHITECTURE.md`, `docs/DEMO.md` and `README.md` are still to be written.

---

## Session 2026-10-21 (later) — Pit Wall panel, strategy engine, and the
### three defects the user reported from the screenshots

### 6. User-reported defects and their causes

| # | Reported | Cause | Fix |
|---|---|---|---|
| 1 | "the circuits dimensions are big make it fit into the boxes" | The SVG carried `h-full`, but its parent had an **auto** height, so `height: 100%` resolved to `auto` and the element fell back to its intrinsic aspect ratio (1000×750) — the track spilled out of the card. | `TrackMap` now renders `absolute inset-0 w-full h-full` inside a `w-full h-full overflow-hidden` root, so the drawing is always exactly the size of its box. |
| 2 | "give each driver on the circuit their team colour" | OpenF1 reports `team_colour` as a **bare hex string** (`"3671C6"`, no `#`). `fill="3671C6"` is invalid CSS, so the browser ignored it and every marker fell back to the default fill. | The backend normalises the colour to `#RRGGBB` (`_team_colour`) and the frontend normalises again (`teamColour`) as a belt-and-braces measure. |
| 3 | "the world championship table and … the prediction for each driver … and what other result must be also is missing" | The `/api/championship/*` routes had been **dropped** when `app.py` was rewritten, so the whole table 404'd. | The routes are restored, the title odds are computed by a vectorised Monte-Carlo, and the UI now shows a per-driver odds bar, an inline requirement and an expandable full list. |

### 7. Strategy engine on real data

The engine is a **joint fixed-effects regression** over the whole race:

```
lap_time = driver_pace + compound_offset + wear(age) + fuel_effect * fuel_remaining
```

Fitting fuel and wear together is what makes them separable: inside one stint
both are linear in the lap number, but a pit stop resets the fuel while the
tyre keeps ageing.

```
$ python scripts/calibrate_circuit.py --circuit Austin --year 2024 --session 9617
{ "circuit": "Austin", "year": 2024,
  "pit_loss_s": 21.14,               # published COTA pit loss is ~18-25 s
  "base_pace_s": 99.404,             # median driver's best-compound pace
  "fuel_effect_s_per_kg": 0.04886,   # = 0.49 s per 10 kg (published value)
  "degradation": { "MEDIUM": [0.01745, 0.000823, 999.0], "HARD": [...] },
  "compound_offset": { "MEDIUM": 1.6765, "HARD": 0.0 },
  "compound_offset_agreement": 0.789, "compound_offset_samples": 19,
  "degradation_max_age": 39, "fit_residual_rms_s": 0.6401, "fit_laps_used": 963,
  "source": "fitted from OpenF1 session 9617" }
```

**The compound offset is only weakly separable** (the compound a driver runs is
tied to their strategy, and the fuel load differs between stints).  Measured
directly on fresh tyres with the fuel effect removed, the per-driver deltas
range from **−39.5 s to +25.1 s**.  The model therefore refuses to rank a
compound switch unless the drivers agree (agreement ≥ 0.8), and the UI says so
rather than making a confident-looking call.

### 8. The tick loop and the panel widgets

`realtime/tick_loop.py` is the spine: it advances the replay once (not once
per browser tab), evaluates the triggers, broadcasts the state and any **new**
alert, and persists a snapshot so a restart resumes instead of re-firing.

The spec's widgets are now built and all consume engine output:
`RaceTicker`, `TyreWidget`, `StrategyCard` (with the p10–p90 band visible,
spec §12.3), `AlertBanner` and `WeatherStrip`.  The weather strip shows the
**real** race-control broadcasts — the upstream weather feed is not retained
for these sessions, and the strip says so.

### 9. Verification

```
$ python -m pytest -q
58 passed, 9 warnings in 5.89s

$ python -m ruff check .
All checks passed!

$ cd web && npm run build
✓ built in 154ms
```

Every endpoint smoke-tested (`available: true` on all 14):

```
  200  /api/health                                      available=True
  200  /api/sessions/current                            available=True
  200  /api/championship/drivers                        available=True
  200  /api/championship/constructors                   available=True
  200  /api/championship/scenarios                      available=True
  200  /api/circuits/Austin/outline                     available=True
  200  /api/drivers                                     available=True
  200  /api/positions?at=900                            available=True
  200  /api/telemetry?driver_number=1&at=900            available=True
  200  /api/timing?at=900                               available=True
  200  /api/strategies?driver_number=1&at=900           available=True
  200  /api/weather                                     available=True
  200  /api/messages                                    available=True
  200  /api/replay/control?action=status                available=True
```

Browser (DOM inspection of the rendered app):

```
landing  { fits: true, overflow: { b: -57, r: -25 } }
live    { fits: true, markerCount: 20,
          teamColours: ["#E80020","#3671C6","#FF8000","#0093cc","#B6BABD",
                        "#6692FF","#229971","#52e252","#64C4FF","#27F4D2"] }
champ   { titlePrediction: true, oddsRows: 20, inlineRequirement: true,
          requiredPanel: true, expandedShows: true }
```

Two further bugs were found and fixed by this session's smoke test: a numpy
`int64` leaking into the JSON response (`/api/replay/control` returned 500),
and a `total_lap` / `total_laps` typo that made `/api/chat` return 500.

---

## Session 2026-10-21 (third) — legibility, smooth tracking, live mode and
### automatic circuit handling

### 10. What changed and why

| Request | Cause | Fix |
|---|---|---|
| "this is very bad it can be read by normal fans and make it legible" | The strategy card spoke in engineering shorthand ("Stay on M", "p10 98:52.884", "Pit lap 29 → M +7.4s"). | The card now leads with the **call** (PIT NOW / WAIT / HOLD), the **lap to box on** ("Box on lap 17–21"), the **race time** with a labelled band ("Fastest case 100:02.816 / Slowest case 100:11.527"), "Chance of passing the car ahead", and the alternatives as "Box on lap 21 · Mediums +8.2s". |
| "make the replay option default to 1x" | The default was 10×. | Default is now 1× on both the client and the server. |
| "can the tracking be slowed like it jumps fast" | The server publishes one frame per second and the car covered ~29 view units in that single step. | The server now also publishes each car's position **one tick ahead** (`next_view_x`/`next_view_y`), and the browser interpolates between the two with `requestAnimationFrame` (plus a timer, because rAF is paused in a background tab). Verified: **14 distinct positions in 14 samples 200 ms apart**, i.e. a continuous glide instead of a step. |
| "throttle ... green and brake ... red" | The telemetry showed "THROTTLE / BRAKE 46% / 0%" as plain text. | A `PedalIndicator` component: a green throttle bar and a red brake bar, each with its percentage. |
| "in the main page make the circuit pop like it is a landing page" | The landing page was a document. | It is now a hero: a big three-line display heading in white/red, the **real circuit outline with a Formula 1 car driving around it** (`animateMotion` along the actual path), a light wash, and three "what this does" cards. The car is drawn as SVG (`F1Car.tsx`) so it stays crisp, can be recoloured, and needs no bitmap. |
| "is the simulation over can the site run when the real race begin" | The app was replay-only. | **Yes.** `OpenF1LiveAdapter` serves the same interface as the replayer, and the app asks OpenF1 which session is running *now*; if one is live it follows it instead of the replay. A background watcher re-checks every 60 s so a race that starts while the app is open is picked up with no restart. When the live feed has no data (the free tier does not carry real-time timing) it stays on the replay and says so. |
| "for each race the circuit layout and the tracking changes so how do we change that" | The outline was per-circuit-by-hand. | **It is automatic.** The circuit is resolved by its OpenF1 `circuit_key` for the season — the same key FastF1 uses for its own circuit maps — and the outline is built and cached on first use. Measured: Baku built in 2.4 s and Imola in 0.5 s, both from the calendar alone. `scripts/build_track_outlines.py --auto` does the same for a whole season. No weekly manual work. |

### 11. Verification

```
$ python -m pytest -q
58 passed, 9 warnings in 6.00s

$ python -m ruff check .
All checks passed!

$ cd web && npm run build
✓ built in 224ms
```

Browser (DOM inspection):

```
hero     { hasCarMotion: true, carPathHref: "M 99.94 463.48 L 102.42 465.39 ...", mapFits: true }
strategy { "HOLD", "Stay on M", "Box on lap 17-21", "RACE TIME 100:07.112",
           "Fastest case 100:02.816", "Slowest case 100:11.527",
           "Chance of passing the car ahead 100%",
           "Box on lap 21 - Mediums +8.2s" }
pedals   { "THROTTLE 99%", "BRAKE 0%" }
speed    { "1x": ACTIVE, "5x", "10x", "30x", "60x" }
tracking { 14 distinct positions in 14 samples 200 ms apart }
live     { mode: REPLAY (no session is running), 20 drivers, lookahead present }
circuits { 28 circuits listed from the OpenF1 calendar }
```

---

## Session 2026-10-22 — radio, weather majority, unified alert strip, round-robin ticker

### 12. The two APIs the user asked about

| Source | Real-time? | Radio? | Weather? | Verdict |
|---|---|---|---|---|
| **ocblacktop.com/api** | Yes, on a paid tier | unknown | track info | A commercial REST API covering F1/NASCAR/IndyCar/MotoGP/FE/WRC/ERC/WEC.  **Free tier is 7,500 req/month and REST-only** — no live connections. WebSockets and webhooks are **paid only** (3 connections on Hobby, 25 on Pro, 500 on Commercial). It needs an API key. It could serve the live mode by polling, but it is a paid dependency and the free tier cannot open a live connection. |
| **LiveF1** (GoktugOcal) | Yes, by design | Yes | Yes | An excellent MIT-licensed toolkit that reads the **official** F1 livetiming feed (`CarData.z`, `Position.z`, `WeatherData.z`, `TeamRadio.z`) plus Jolpica. It is the right shape for this project — **but the official feed is now behind authentication**: `livetiming.formula1.com` returns **HTTP 401** for the archive *and* for the live SignalR `negotiate`, and FastF1's own mirror returns 404 for the same paths. Measured, not assumed. It would work on a machine with feed access. |
| **OpenF1** (in use) | Historical only on free tier | **No** | No | Measured: `/radio` returns **404** for every query shape (session, session+driver, year). The free tier is "all historical sessions since 2023"; live data during a session needs the **EUR 9.90/month** tier. |

**Conclusion:** the radio section is built and source-agnostic. It serves
whatever a source provides and states plainly why there is nothing, instead of
an empty box that looks broken. It fills by itself the moment a feed with
radio access is used.

### 13. Two real bugs found while answering the questions

* **The race-control feed is a meeting feed.**  For the 2024 Austin race it
  carried 120 messages, of which several belonged to *Saturday's sprint* —
  including the "RISK OF RAIN FOR F1 RACE IS 0 %" briefing.  The pit wall
  now keeps only the messages inside the session's own data span (the window
  runs from the first GPS sample minus 30 minutes, because that is when the
  pit exit opens and the pre-race briefings are given), which cut the feed
  from 120 to the 12 that belong to the race.
* **The rain risk was read as 10% instead of 0%.**  The briefing reads
  "RISK OF RAIN FOR **F1** RACE IS 0 %" and the parser was collecting every
  digit in the line, so the "1" of "F1" was prepended to the "0".  The
  parser now takes the number next to the percentage sign.

### 14. What the user asked to change

* **One alerts box.**  The separate flag banner was **deleted**; the existing
  "NO ACTIVE ALERTS" panel now carries the **colour of the flag** when one is
  out (yellow, double yellow, red, VSC, safety car, chequered) and stays
  quiet and neutral when the track is clear.  Sector-scoped yellows are tracked,
  so clearing one sector does not drop the flag while another is still out.
* **The weather is decided by the majority.**  The strip now reports the
  **majority** of the race-control surface reports — 7 clear against 2
  slippery for the 2024 race — so a sector that was briefly slippery on lap
  one and cleared seconds later no longer makes the race look wet.  The
  "sector 4 is slippery" message is gone from the weather panel, and the rain
  animation only appears when the risk is genuinely high (it is 0% here, so
  nothing animates).
* **Round-robin ticker.**  The three driver cards now walk through the whole
  field on a timer, each showing the driver's name and the **gap to the car in
  front** (OpenF1's `interval`, which is a different number from the gap to
  the leader).  Measured: 9 distinct orders in 14 samples.
* **Speed defaults to 1x.**  Already the case, kept.
* **Radio section.**  Added, with an honest empty state.

### 15. Verification

```
$ python -m pytest -q
58 passed, 9 warnings in 4.62s

$ python -m ruff check .
All checks passed!

$ cd web && npm run build
✓ built in 184ms
```

```
WEATHER { rain_risk_percent: 0.0, condition: "clear", clear_reports: 7, slippery_reports: 2 }
RADIO   { available: false, reason: "no_radio_feed", detail: "..." }

ticker     { orders: 9, first: "P1 LEC LEADER | P2 VER +3.572 | P3 SAI +1.347",
             last:  "P4 NOR +2.114 | P5 PIA +2.494 | P6 GAS +2.073" }
alerts     { shows "NO ACTIVE ALERTS", separate flag banner removed }
weather   { "TRACK DRY | rain risk 0% | 7 clear / 2 slippery", rain animation elements: 0 }
radio     { "TEAM RADIO - No team-radio feed is available for this session..." }
```

---

## Session 2026-10-22, part 2 — the official F1 live feed is **not** dead

Part 1 concluded that the official F1 livetiming feed was unreachable
(HTTP 401) and that LiveF1 therefore could not deliver real-time data.
**That conclusion was wrong**, and the correction matters more than anything
else in this log.

### 16. Two SignalR endpoints, one gated and one open

F1 has **two** SignalR endpoints on the same host:

| Endpoint | Status | Who uses it |
|---|---|---|
| `/signalr/` | **HTTP 401** | LiveF1 (hardcoded in `livef1/utils/constants.py`: `SIGNALR_ENDPOINT = "/signalr/"`) |
| `/signalrcore` | **open** | F1's own website and app |

The evidence that `/signalrcore` is the real one is in the F1 website's own
embedded configuration:

```
NEXT_PUBLIC_GLOBAL_LIVETIMING_TIMINGHUB = https://livetiming.formula1.com/signalrcore
NEXT_PUBLIC_GLOBAL_EVENTTRACKER_URL    = https://livetiming.formula1.com/signalrcore
NEXT_PUBLIC_GLOBAL_LIVETIMING_STATIC   = https://livetiming.formula1.com/static/
NEXT_PUBLIC_GLOBAL_APIGEE_BASEURL      = https://api.formula1.com
```

So LiveF1 was not broken by F1's authentication: it was pointing at the
**legacy** endpoint that F1 retired, while the site had moved to
`/signalrcore`.

### 17. The handshake, measured

```
POST https://livetiming.formula1.com/signalrcore/negotiate?negotiateVersion=1
     body: []            (Content-Type: application/json)
->  200 {"negotiateVersion":1,"connectionId":"...","connectionToken":"...",
         "availableTransports":[{"transport":"WebSockets",...},...]}

GET  /signalrcore/negotiate        -> 405   (it wants POST, not GET)
wss://livetiming.formula1.com/signalrcore?id=<token>
->  handshake "{}"                   (the SignalR JSON protocol)

{"type":1,"target":"Subscribe","arguments":[...topics...]}   -> accepted
```

Two details that cost time:

* the token **must be URL-encoded**.  SignalR tokens are base64 and can
  contain `+`, `/` and `=`; an unencoded `/` truncates the path and the
  server answers **HTTP 404**, which looks exactly like a wrong endpoint.
* `/connect?transport=webSockets&connectionToken=...` (LiveF1's URL form)
  answers **401**.  The working form is `?id=<token>`.

### 18. What the hub carries, and what it does not

| Topic | Payload |
|---|---|
| `CarData.z` | position, speed, throttle, brake, gear, RPM, DRS, compound, tyre life, lap |
| `Position.z` | the running order |
| `TimingData.z` | lap times, gap to the leader, **interval to the car ahead** |
| `WeatherData.z` | air and track temperature, humidity, pressure, wind, rainfall |
| `TeamRadio.z` | **the radio as text** - `"Captions": "HAMILTON: are you doing a good job gap to max three point five"` |
| `RaceControlMessages.z` | flags, safety car, incidents |
| `SessionInfo.z` | which session is running, and its path |

Two things follow from this:

* **Live radio needs no speech recognition.**  F1's own captions are on the
  topic, so the transcript is already there.  The offline recogniser is only
  needed for the *archived* radio, where OpenF1 gives an MP3 and no text.
* The historical archive (`/static/.../*.jsonStream`) is **still** gated
  (401), and the negotiate token does not unlock it.  The **audio assets**
  (`/static/.../TeamRadio/*.mp3`) are not gated, which is how the replayed
  radio is obtained.  OpenF1 covers the archive.

### 19. Live mode, verified in the app

```
GET /api/live/status
{ "available": true, "mode": "REPLAY", "hub_connected": true, "hub_error": null,
  "session": null, "drivers": 0, "radio_calls": 0, "weather_reports": 0,
  "messages": 0 }
```

The hub is connected and stays connected.  `session` is null because no
session is running - the next is Austin Practice 1 on **23 Oct 17:30 UTC** -
so the pit wall correctly stays in REPLAY.  When a session starts, the hub
reports it and the app switches by itself.

**Live timing is therefore free.**  The EUR 9.90/month OpenF1 tier is now
only a fallback, not a requirement.

### 20. Two more real bugs found along the way

* **`get_intervals()` did not accept `date_gt`.**  The first live poll
  raised a `TypeError`, so `/api/live/snapshot` was an HTTP 500 after 50
  seconds and the app's own live-switcher would have died the moment a
  session started.  Live mode had therefore *never* worked.
* **The rain risk was read as 10% instead of 0%**, because the parser
  collected every digit in `"RISK OF RAIN FOR F1 RACE IS 0 %"` and took the
  "1" of "F1" as part of the figure.  It now reads the number next to the
  percent sign.

---

## Session 2026-10-22, part 3 — the tyre degradation, the radio and the hub's throttle

### 21. The degradation, and why it cannot be fitted

The old fit computed the fuel mass from the lap number:

```python
fuel = max(0.0, FUEL_START_KG - (lap_number - 1) * fuel_burn)
```

so the fuel **never reset at a pit stop** - which is not what happens in a
car, and which contradicted the module's own docstring.  The fitted
degradation collapsed to **0.00216 s/lap** for the 2025 race, about
twenty-five times too small.

The split cannot be fitted, and that is now proved rather than assumed:

| Measurement | Value |
|---|---|
| correlation between tyre age and fuel, within a stint | **-0.9995** (2024), -0.9867 (2025) |
| condition number of the raw joint least-squares fit | **1e16** |
| the coefficients that fit returns | age = +3.5 s/lap, fuel = +3.1 s/kg |
| what a pit-stop level shift tells you | only that the *sum* of the two changed |

Inside a stint the fuel mass and the tyre age are exactly affine in one
another, and a pit stop resets both at once, so the level shift adds no
information about the split.

**What the fit does instead.**  The within-stint slope of lap time on tyre
age is measured directly, with the pit laps removed by a median-absolute-
deviation filter (6 sigma).  The fuel effect is the published Formula 1
figure of 0.05 s/kg - a physical constant, not something this data can
determine - and the degradation follows:

    b1 = measured slope + fuel_effect * burn

| Compound | 2024 slope | 2024 b1 | 2025 slope | 2025 b1 |
|---|---|---|---|---|
| MEDIUM | -0.0485 | **+0.0265** | -0.0082 | **+0.0668** |
| HARD   | -0.0255 | **+0.0495** | -0.0457 | **+0.0293** |
| SOFT   | -      | -        | -0.0045 | **+0.0705** |

All of them land in the 0.03 to 0.07 s/lap band that real Formula 1
degradation occupies.

### 22. The cliff: detected, and honestly absent

A free quadratic term trades off against the linear one along a ridge, so
scanning the cliff age on a joint fit picks whatever it finds there (it
returned b1 = 0 and b2 = 0.64 on the 2024 race).  The cliff is instead
detected from the **residuals**, and only where the residual *rises* and
stays up - one slow lap does not count.

The measured residual profile is **concave**, not convex:

```
2024 HARD   age 1-4  +1.02 s   age 13-16  +0.04 s   age 25-28  -0.67 s
            age 5-8  +0.84 s   age 17-20  -0.25 s   age 33-36  -1.32 s
2025 MEDIUM age 1-4  +1.06 s   age 13-16  +0.08 s   age 25-28  -0.72 s
```

The tyre is slower than a straight line predicts when it is new and faster
than it predicts when it is old.  That is the opposite of a cliff, so **no
cliff is reported** and the bundle says 999 rather than inventing one.  A
compound nobody ran also has no cliff, so the old made-up fallback of 18
laps is gone.

What the tyre widget now shows, on the real data:

```
TYRE  MEDIUM | age 12 L | Degradation +0.32s | 44 laps of stint left
      | no cliff in the fitted range
```

### 23. The radio, transcribed

The endpoint was `/team_radio`, not `/radio`.  The calls are audio on F1's
public static archive, and OpenF1 publishes no text, so the transcript is
produced locally with Vosk.

The two models, on the same clips:

| Clip | `en-us-0.22-lgraph` (128 MB) | `small-en-us-0.15` (40 MB) |
|---|---|---|
| Verstappen, 20:32 | "the stewards are looking at a **max**" | "...looking at a **bucks**" |
| driver 4, 19:2x | "we appear to have a lower **decks** ... how's the **pace**" | "...a lower **tag** ... how's the **pies**" |
| driver 20 | "the box **cat** box you must box now" | "box get **books** you must box now" |

The larger model is clearly better and about six times slower (about ten
seconds a call against one and a half), so it runs in the background and the
panel fills in as the results arrive.  Real transcripts from the 2024 race:

```
18:22  drv  4  "the okay as a bit nervous into to nineteen of this laugh he
                  should be able to see that don't break direction and launch"
18:25  drv 44  "set your right hand down today"
```

### 24. The hub throttles

Six or seven negotiate-and-connect cycles in quick succession are answered;
after that the websocket upgrade answers **HTTP 404** until the budget
refills.  The 404 means "too many connections", **not** "wrong address",
which is why it looks exactly like a broken URL.  The client now backs off
exponentially (15 s, 30 s, 60 s, 120 s, 300 s) and the app's watcher
re-tries every 60 s.  Verified: the app reconnected on its own once the
budget refilled.

### 25. The replay now ends with the racing, not the cool-down

The clock used to end at the last GPS sample, which is after the flag when
the cars come round one more time at 60 km/h.  It now ends with the last
racing lap.

### 26. The race clock

`0:27:25` reads ambiguously, so it is now spelled out: `27m 25s`, and
`1h 04m 12s` once it passes an hour.

---

## Session 2026-10-22, part 4 — the strategy answers the question that is actually asked

### 27. "If I stop now, do I come out in front of that car or behind it?"

The strategy card was answering a question nobody asked.  It showed a race
time, a p10-p90 band and a ranked list of compounds.  The forecast now
answers the real question.

The projection works on the **real gaps**, not on the model: the strategy's
time difference is applied to the gap the driver has to the leader, the
whole field is sorted on the gaps, and the driver's place in that order -
and the cars either side of them - follow from it.

The first implementation walked the field once and kept the first car it
met rather than sorting it, so it named the wrong cars.  The three cases are
now pinned by tests:

| Situation | Result |
|---|---|
| staying out | unchanged position, the car ahead and the car behind named |
| gaining 12 s | takes the lead; the car immediately behind is the one that was leading |
| losing 20 s | drops a place; the car that comes through is named |

### 28. The compound was flip-flopping

The card said "Box on lap 26 onto Softs" for one driver and "Mediums" for
the next.  The cause: when the measured compound offset is **not**
separable from strategy choice - the agreement was 0.00 across two drivers
- the engine fell back to offering *every* compound, and the recommendation
was then decided by noise.

Now only the compound the driver is actually on is projected, so the call
is about **when** to stop rather than **what** to stop on, and the label
says "Box on lap 26, stay on the Mediums".  The unreliable offsets are also
no longer used to shift anybody's pace, which was tilting the ranking.

### 29. OpenF1's data has holes, and the pit wall now admits it

OpenF1 has **two laps** for Hamilton in the 2024 United States Grand Prix.
Verified against the API directly, so it is not a broken download:

```
driver 44: laps=2  position=11  intervals=39  stints=1
driver  1: laps=56 position=12  intervals=1170 stints=2
```

His position and gap rows stop at 19:07, seven minutes into the race, so
the pit wall was showing him at **P20 with a 9.9 second gap to the leader**
while the rest of the field ran behind him, and the forecast was built on
that.  The pit wall now notices when a driver's timing entries stop, marks
them **out**, and leaves them out of the forecast.

### 30. The simulation chrome is gone

The 1x/5x/10x/30x/60x control, the pause button, the "REPLAY" badges and
the technical text are all removed:

* "Session 9617 - REPLAY - Lap 19/56 - Real OpenF1 telemetry"
* "AUSTIN 2024 - REPLAY - SESSION 9617"
* "154 readings | rain risk 0%"
* "Tyre choice is not ranked: ... (0.00 agreement across 2 drivers)"

The conversational assistant is removed for now: it was taking the space
the strategy, tyre and radio cards need and it was answering with the same
numbers the cards already show.

### 31. The radio plays out with the race

Only the calls that have already happened by the current position are
served, newest at the top, and the transcription runs newest-first so the
calls on screen are the ones that get ready first.  Measured on the 2024
race: 81 calls shown, 81 transcribed, none pending, with 147 transcripts
cached on disk.

---

## Session 2026-10-22, part 5 — is the degradation from the API or from us?

### 32. The honest answer

**It is a calculation, and it is a per-lap quantity by construction.**

OpenF1 does not publish "tyre degradation" as a field.  What it publishes is
the **tyre age** and the **compound** (from the `/stints` feed) and the
**lap times**.  The degradation curve is fitted here from those real lap
times - it is `b1 x tyre_age`, where `b1` is the fitted wear rate for that
compound at that circuit (0.0265 s/lap on the mediums at Austin 2024,
0.0668 s/lap in 2025).

Because `tyre_age` is a **whole number of laps**, the number shown steps
once per lap and cannot wobble in between.  Measured over 55 seconds of the
2024 race:

```
  0s  lap 24  MEDIUM  age 23  deg 1.537  WAIT  Box on lap 28, stay on Mediums
 55s  lap 25  MEDIUM  age 24  deg 1.603  WAIT  Box on lap 27, stay on Mediums
```

Two changes in 55 seconds, both at a lap boundary, and the degradation
moved by exactly `b1` (0.066 s).  Nothing moves within a lap.

### 33. The strategy could still change its mind on noise

The model is re-run every second.  Two candidate stops that are within the
model's own noise can swap places between one run and the next, so the card
could say "box on lap 27" and then "box on lap 28" without the race having
moved a metre.

So the call is now **sticky**: the engine remembers the pit lap it called,
per driver, and keeps it until the lap changes.  And the call itself is
made against the model's own noise - a stop that beats staying out by less
than `noise_sigma` (0.5 s) is a hold, because inside that band the two are
the same answer.

The first implementation of the hysteresis was wrong in two ways and the
tests caught both:

* the sticky option was computed but the call was still made against
  `options[0]`, so it did nothing;
* the noise test had the sign backwards - a stop that is *faster* has a
  negative cost, so `cost <= noise_sigma` was holding on a stop that was
  ten seconds quicker.

`tests/engine/test_hysteresis.py` now pins all of it: the called pit lap is
kept within a lap, a new lap may change it, a stop inside the noise is a
hold, a clear gain is a stop, and two drivers get their own calls.

### 34. A driver whose data stops is not given an invented tyre

Hamilton has one stint row in the 2024 race, covering lap one.  The lookup
used to find nothing and show him with no compound at all.  Now the tyre a
driver is on falls back to the stint whose start is the most recent before
the lap being run - which is the honest reading.

And the tyre **age** is not extrapolated past the end of the stint: it is
left at the age that stint began with, because carrying it forward would
invent a number no data supports.  So Hamilton shows `HARD, age 0` and is
marked out, rather than a tyre ageing at one lap per lap on data that
stopped seven minutes into the race.

---

## Session 2026-10-22, part 6 — a finished replay froze the app

### 35. The replay had no way to start again

The clock stops itself at the last racing lap (so the cool-down lap does not
show a car doing 60 km/h), and `advance()` clamps at the end.  Nothing then
moved it again, so once Austin 2024 played through, the pit wall sat on its
final frame for ever.  Measured: `elapsed_s 5795.699` of `duration_s
5795.699`, `finished: true`, speed 1.0 — and 14 seconds later, exactly the
same numbers.

The replay now **starts again by itself**.  The final frame is held for
`FINISH_HOLD_S` (8 s) and then the clock is seeked to zero, with
`_alert_keys` cleared so the second run's pit-window alerts fire again
instead of being suppressed as duplicates.  A live source never reports a
finished clock, so the live path is untouched.

Verified end to end: with the clock parked at the end, opening the page
took it to `elapsed 35.0` and then lap 1 of 56 with Leclerc leading.

`/api/replay/control?action=reset` now goes through `TickLoop.seek`, which
also clears the hold, so a manual reset takes effect immediately instead of
waiting out the eight seconds.

### 36. A detail worth remembering

The replay only advances while a client is subscribed — `TickLoop.start()`
is called from the WebSocket handler.  So a stuck clock can also mean "no
page is open", not just "the race finished".  Check the elapsed time is
actually moving before assuming the loop is broken.





