# PitWall

A real-time Formula 1 pit wall: live timing, car positions, telemetry, tyre strategy, team radio, weather and race control, built from real session data.

PitWall is not a race simulator. It follows the official F1 live timing feed when a session is running and falls back to a cached race replay when nothing is on track. Every number comes from a real source; when a value is not available, the UI says so instead of inventing one.

[![CI](https://github.com/abhinav4001/PITWALL/actions/workflows/ci.yml/badge.svg)](https://github.com/abhinav4001/PITWALL/actions/workflows/ci.yml)

## What it does

- **Live timing** - positions, gaps, intervals, laps, sectors and race status from F1's own SignalR hub (`livetiming.formula1.com/signalrcore`).
- **Live track map** - car positions drawn from the feed's own X/Y coordinates. While a live session is starting, the outline is built from the path the cars actually drive.
- **Telemetry** - speed, throttle, brake, gear, RPM and DRS for the selected driver.
- **Strategy** - tyre life, degradation, pit loss and a ranked stop plan. The main question is answered directly: if this driver pits now, where do they come out relative to the cars around them?
- **Tyres** - the compound the driver is actually on, its age, and a lap-by-lap degradation projection. The compound never flips to another tyre.
- **Radio** - the official F1 team radio audio, played as each call arrives. No machine transcripts.
- **Weather and race control** - track and air temperature, wind, rain risk, flags, safety cars and investigations.
- **Championship** - 2026 drivers' and constructors' standings with title scenarios computed from the published points.
- **Dark and light themes** - the whole pit wall works in both.

## Data sources

| Data | Source |
|---|---|
| Live timing, positions, telemetry, weather, radio, race control | F1 live timing hub (`/signalrcore`) |
| Historical race replay | OpenF1 session packs |
| Circuit outlines | FastF1 circuit data and live position traces |
| Championship standings | Published 2026 season figures |

The live hub is free and unauthenticated. It throttles repeated connections, so the backend backs off and reconnects by itself rather than retrying aggressively.

## Requirements

- Python 3.11+
- Node.js 20.19+ (or 22.12+)
- npm

## Quick start

### 1. Backend

From the project root:

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Install and run:

```bash
pip install -e ".[dev]"
python -m uvicorn pitwall.api.app:app --port 8111
```

The API is then at `http://localhost:8111`.

### 2. Frontend

In a second terminal:

```bash
cd web
npm install
npm run dev -- --port 5174
```

Open `http://localhost:5174`.

The frontend reads `VITE_API_BASE` and defaults to `http://localhost:8000`. If the backend is on port 8111, create `web/.env.local`:

```dotenv
VITE_API_BASE=http://localhost:8111
```

### 3. Replay data

The 2024 Austin replay is session `9617`. Session packs are not committed, so download it once:

```bash
python scripts/download_session.py --session 9617
```

Then start the backend and open the pit wall.

## Live sessions

When a real session is running, the backend switches to the live feed automatically; there is no mode switch in the UI. The header shows the session name, lap count and a live indicator.

To check what the backend currently sees:

```bash
python scripts/check_live.py
```

It prints the mode (`LIVE` or `REPLAY`), hub connection state, subscribed topics, driver count, outline source and any decode problems.

The F1 hub can refuse connections after several attempts in a short period. PitWall treats that as normal: it retries with exponential backoff, and watches the calendar more closely when the next session is due.

## Strategy and calibration

Strategy is only shown when a calibration bundle exists for the session's circuit and year. Calibration is fitted from real laps, not hard-coded:

```bash
python scripts/calibrate_circuit.py --circuit Austin --year 2024 --session 9617
```

Bundles live in `calibration/<circuit>/<year>.json`. If there is no bundle, the strategy card says it is unavailable rather than guessing.

## Development

Run the checks:

```bash
ruff check .
pytest
cd web && npm run build
```

CI runs `ruff check .` and `pytest` on every push and pull request.

## Project layout

```text
src/pitwall/
  api/          FastAPI app, REST endpoints and the live WebSocket
  data/         OpenF1 client, session packs and radio metadata
  engine/       Strategy, tyre degradation, pit loss and championship models
  realtime/     F1 live hub client, replay source and tick loop
  eval/         Evaluation tooling
  llm/          Optional intent and insight layer
web/src/        React pit wall (Vite + Tailwind)
scripts/        Session download, calibration, outline and live diagnostics
calibration/    Fitted circuit bundles
tests/          Backend test suite
docs/           Architecture notes, measurements and verification log
```

## Notes and limitations

- Live data is only available while a real F1 session is running.
- The live hub is unofficial in the sense that it is the same endpoint F1's website uses; its availability and throttling are outside this project's control.
- Strategy requires a calibration bundle. Without one, PitWall reports "unavailable" instead of a forecast.
- Team radio is audio only. F1's recordings are played directly; no speech recognition is used.
- The optional LLM layer is disabled unless an OmniRoute-compatible gateway is configured in `.env`. The deterministic pit wall does not need it.

## API

The backend exposes the pit wall over HTTP and streams race frames over a WebSocket.

Useful endpoints:

```text
GET  /api/health
GET  /api/sessions/current
GET  /api/live/status
GET  /api/live/snapshot
GET  /api/positions
GET  /api/telemetry
GET  /api/timing
GET  /api/strategies
GET  /api/weather
GET  /api/radio
GET  /api/championship/drivers
GET  /api/championship/constructors
GET  /api/circuits
GET  /api/outline/current
WS   /ws/live
```

Every data endpoint can return an explicit unavailable payload with a reason, so clients can render an honest empty state.
