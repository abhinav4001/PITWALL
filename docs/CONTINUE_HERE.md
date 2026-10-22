# Continue here — 2026-10-22, session 3 (evening)

Work is committed; the tree is clean. Read this first, then
`docs/LEARNING_LOG.md` for the measurements behind every claim.

---

## 1. The headline: live timing is **free**

The earlier conclusion — "the official F1 feed is behind authentication, so
live timing needs the €9.90/month OpenF1 tier" — was **wrong**. It is
corrected here.

F1 has **two** SignalR endpoints on the same host:

| Endpoint | Status | Who uses it |
|---|---|---|
| `/signalr/` | **HTTP 401** | LiveF1 (hardcoded: `SIGNALR_ENDPOINT = "/signalr/"`) |
| `/signalrcore` | **open** | F1's own website and app |

The proof that `/signalrcore` is the current one is in the F1 website's own
embedded configuration:

```
NEXT_PUBLIC_GLOBAL_LIVETIMING_TIMINGHUB = https://livetiming.formula1.com/signalrcore
NEXT_PUBLIC_GLOBAL_EVENTTRACKER_URL    = https://livetiming.formula1.com/signalrcore
```

So LiveF1 was not defeated by an authentication wall — it was pointing at
the legacy endpoint F1 had retired. **LiveF1's own idea is right; only its
URL was stale.**

The handshake, measured:

```
POST /signalrcore/negotiate?negotiateVersion=1   body []      -> 200 + token
wss://livetiming.formula1.com/signalrcore?id=<token>              -> handshake "{}"
{"type":1,"target":"Subscribe","arguments":[...topics...]}        -> accepted
```

Three details that cost time and are worth keeping:

* the negotiate is **POST**, not GET (GET answers 405);
* the token **must be URL-encoded** — SignalR tokens are base64 and can
  contain `+`, `/` and `=`, and an unencoded `/` truncates the path so the
  server answers **404**, which looks exactly like a wrong endpoint;
* the URL form is `?id=<token>`; LiveF1's
  `/connect?transport=webSockets&connectionToken=…` answers **401**.

**The hub throttles.** Six or seven negotiate-and-connect cycles in quick
succession are answered; after that the upgrade answers **404** until the
budget refills. The client backs off exponentially (15/30/60/120/300 s) and
the app retries on its 60 s watcher. Verified: the app reconnected by itself.

The hub carries everything a pit wall needs: `CarData.z` (position, speed,
throttle, brake, gear, RPM, DRS, compound, tyre life), `Position.z`,
`TimingData.z` (lap times and the gap to the car ahead), `WeatherData.z`,
`RaceControlMessages.z`, `SessionInfo.z` — and `TeamRadio.z`, which carries
the radio **as text**, so live radio needs no speech recognition.

The historical archive (`/static/…/*.jsonStream`) is still gated and the
token does not unlock it; the **audio assets**
(`/static/…/TeamRadio/*.mp3`) are not gated, which is how the replayed
radio is obtained. OpenF1 covers the archive.

**Code:** `src/pitwall/realtime/livetiming.py` — `LivetimingLiveSource`
(the hub client, with reconnect) and `LivetimingLiveAdapter` (the same
replay-compatible surface the OpenF1 adapter presents).
`src/pitwall/api/app.py` tries the hub first and falls back to OpenF1.
`GET /api/live/status` reports the connection.

---

## 2. Where each request stands

| Request | Status | Evidence |
|---|---|---|
| "is the realtime api working then switch it to the realtime one" | **Working** | `hub_connected: true`. The app goes live by itself the moment a session runs; there is nothing to switch. |
| "why can't we use the new api for the realtime details" | **Answered and fixed** | LiveF1's URL was stale, not paywalled. The open endpoint is `/signalrcore`. |
| "the strategy should be a guess: if the driver I am watching pits this time he will be behind or ahead of this guy" | **Done** | The card now leads with the call and then **"P1 — where you end up — ahead of VER"**, worked out from the real gaps. |
| "what is that time tbh, i think it is unnecessary" | **Removed** | The race time, the p10-p90 band and the fastest/slowest case are gone from the card. |
| "the radio … should be given at the moment it comes in rather than all at once with the timestamp" | **Done** | Only the calls made by now are served, newest first. 81 shown, 81 transcribed. |
| "the ai asking stuff at the bottom i think we can scrap it for now" | **Removed** | The conversational assistant panel and its state are gone. |
| "make every boxes etc position better for viewing" | **Done** | The live view was reorganised: the map and tower on the left, strategy, tyre, weather and radio down the right, with the chat box's space given to them. |
| "the transcribing feature is not working" | **It was, and now it is faster** | 147 transcripts are cached; the task runs **newest first** so the calls on screen are ready first. |
| "in real race weekend will the radio be audio or transcript" | **Both, and better** | Live: F1's own hub sends the radio **as text**, so it appears as it is said. Replay: the MP3 plays and the transcript is produced offline. |
| "tyre degradation option … keeps fluctuating between soft and medium" | **Fixed** | It was echoing whichever compound the driver was on. Now only the compound they are on is projected, and the unreliable offsets no longer tilt the ranking. |
| "remove all the simulation things like the 1x things" | **Removed** | The speed control, the pause button and the "REPLAY" badges are gone. The pit wall plays at real-time speed. |
| "like this text please remove from everywhere" | **Removed** | "Session 9617 • REPLAY • Lap 19/56 • Real OpenF1 telemetry", "AUSTIN 2024 • REPLAY • SESSION 9617" and "154 readings \| rain risk 0%" are all gone. |
| "please everything more polished and error free" | **Done** | Also found: OpenF1 has only 2 laps for Hamilton in the 2024 race (verified upstream, not a broken download), so the pit wall was showing him P20 with a stale gap. He is now marked **out** and left out of the forecast. |
| "please confirm that we are using the new api for all purposes" | **Yes, with one honest caveat** | Live timing, telemetry, positions, gaps, flags, weather and radio all come from **F1's own hub**. The **archive** comes from OpenF1, because F1's historical `.jsonStream` files are gated (401) while its radio MP3s are not. |


---

## 3. The two real bugs the audit found

* **`get_intervals()` did not accept `date_gt`.** The first live poll raised
  a `TypeError`, so `/api/live/snapshot` was an HTTP 500 after 50 seconds
  and the app's own live-switcher would have died the moment a session
  started. **Live mode had never worked.** Fixed.
* **`/api/radio` took 9–15 s** because it fetched upstream inline on every
  request. The radio is now fetched when the session pack is prepared and
  served from the cache.

---

## 4. How the degradation fit works now

The split between fuel and tyre wear **cannot be fitted** from lap times,
and that is proved rather than assumed: inside a stint the fuel mass and
the tyre age are exactly affine in one another (correlation **-0.9995**),
and a pit stop resets both at once, so its level shift says only that the
sum changed. The raw joint least-squares fit has a condition number of
1e16 and returns nonsense.

So the fit measures what the data can say and takes the rest from physics:

* the **within-stint slope** of lap time on tyre age, measured directly,
  with the pit laps removed by a 6-sigma median-absolute-deviation filter;
* the **fuel effect** is the published Formula 1 figure, 0.05 s/kg;
* **b1 = slope + fuel_effect × burn**.

The cliff is detected from the residuals and only where the residual rises
*and stays up*. On these two races the degradation is **concave** — the
residual is +1.0 s on fresh tyres and -1.3 s on old ones — which is the
opposite of a cliff, so **no cliff is reported** and the bundle says 999
rather than inventing one.

---

## 5. The radio pipeline

```
/api/team_radio (OpenF1)  ->  recording_url (F1's public archive, no auth)
                          ->  MP3 fetched
                          ->  vosk transcribes it locally
                          ->  radio.json, served by /api/radio
```

* The model is `vosk-model-en-us-0.22-lgraph` (128 MB), chosen after
  comparing it with the small model on the same clips: the large one heard
  "the stewards are looking at a **max**" where the small one heard
  "a **bucks**". Override with `PITWALL_RADIO_MODEL`.
* The transcription runs **in the background** and the panel fills in as
  the results arrive; `/api/radio` reports `transcribed` and `pending`.
* `faster-whisper` **cannot** work on this machine: `onnxruntime`'s DLL is
  refused by the Windows Application Control policy. `vosk` is pure C++ and
  works.

---

## 6. How to run it

```powershell
python -m uvicorn pitwall.api.app:app --port 8111     # 8000 is taken
cd web && node node_modules/vite/bin/vite.js --port 5174
```

> Use **`localhost`**, not `127.0.0.1`, in the browser. `web/.env.local`
> must carry `VITE_API_BASE=http://localhost:8111` (gitignored).

```powershell
python scripts/download_session.py --session 9617        # re-download a pack
python scripts/calibrate_circuit.py --circuit Austin --year 2024 --session 9617
python scripts/build_track_outlines.py --auto
```

---

## 7. Current verification state

```
python -m pytest -q      -> 90 passed
python -m ruff check .   -> All checks passed!
cd web && npm run build  -> built
```

Browser, on the 2024 Austin race:

```
clock    "22m 57s"
weather  "TRACK DRY | AIR 29.2°C | TRACK 46.0°C | HUMIDITY 32% |
          WIND 1.0 m/s ENE | Racing on a dry track"
tyre     "MEDIUM | age 12 L | Degradation +0.32s | 44 laps of stint left |
          no cliff in the fitted range"
strategy "HOLD | Stay on Mediums | Best window: lap 25-36 |
          P1 WHERE YOU END UP | ahead of VER"
radio    "81 calls shown, 81 transcribed, none pending"
live     { "hub_connected": true, "hub_error": null, "session": null }
```

`session: null` is correct — no session is running. The next is **Austin
Practice 1, 23 Oct 17:30 UTC**; the app will switch to it by itself.

---

## 8. What is left

1. **`README.md`, `docs/ARCHITECTURE.md`, `docs/DEMO.md`** — still missing
   (milestone M9).
2. **Rolling 2026 evaluation** — `eval/rolling_eval.py` is still a stub (M5).
3. **Traffic model (ADR-3 L0→L2), SC/VSC hazard model, precompute (ADR-6)**.
4. **Debrief screen** — spec 12.1 wants a post-race divergence chart and a
   season scorecard.
5. **LLM layer (M6)** — blocked on **D2** (OmniRoute key; the gateway still
   answers 401). The app runs on the deterministic engine meanwhile.
6. **Priority circuits for calibration** — Austin 2024 and 2025 are done;
   Silverstone, Monza and Spa are not (D4).
7. **Watch the live switch on Austin P1** (23 Oct 17:30 UTC) — the first
   real test of the hub path with a session running.

---

## 9. Open decisions

| # | Decision | Blocks |
|---|---|---|
| ~~D1~~ | **Closed** — the official hub is free and open; no paid tier needed. The €9.90 OpenF1 tier is a fallback, not a requirement. | — |
| D2 | OmniRoute API key for `.env` | M6 LLM contract testing |
| D3 | Supabase auth — the fake modal was removed; re-add a real one? | M8 scope |
| D4 | Priority circuits for calibration | M2 batching |

---

## 10. Things to be careful about

* `data/cache/sessions/` is **gitignored**; `9617` and `9888` must be
  re-downloaded on a fresh clone. `data/track_outlines/*_2024.json` and
  `calibration/austin/*.json` **are** committed.
* **Never hammer the F1 hub.** Six or seven connections in quick
  succession and the upgrade answers 404 until the budget refills. The
  client backs off; leave it alone.
* OpenF1 free tier: **3 req/s, 30 req/min**. The client throttles at 1 req/s.
* `date>=` and `date<=` are **rejected** by OpenF1 (404). Use `date>` and
  `date<`.
* A whole-session `/location` or `/car_data` query for all drivers returns
  **HTTP 422**; query per driver or use a date window.
* `livef1` needs `pandas>=2.2`; `fastf1` needs `pandas<3`. Install
  `livef1==1.2.0`.
