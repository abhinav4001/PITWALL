import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertBanner, type PitWallAlert } from './components/AlertBanner'
import { PedalIndicator } from './components/PedalIndicator'
import { RaceTicker } from './components/RaceTicker'
import { RadioPanel } from './components/RadioPanel'
import { StrategyCard, type StrategyReport } from './components/StrategyCard'
import { TeamLogo } from './components/TeamLogo'
import { TimingTower } from './components/TimingTower'
import { TrackMap } from './components/TrackMap'
import { TyreWidget } from './components/TyreWidget'
import { WeatherStrip, type WeatherReport } from './components/WeatherStrip'
import { ThemeToggle, useTheme } from './lib/theme'
import {
  api,
  formatGap,
  formatLapTime,
  isUnavailable,
  teamColour,
  type CircuitOutline,
  type ConstructorStanding,
  type CurrentSession,
  type DriverPosition,
  type DriverStanding,
  type RaceFrame,
  type RadioReport,
} from './lib/api'

type View = 'landing' | 'championship' | 'live'

const FLAG_STYLE: Record<string, { label: string; className: string }> = {
  GREEN: { label: 'GREEN', className: 'bg-emerald-500/20 text-emerald-300 border-emerald-600/50' },
  YELLOW: { label: 'YELLOW', className: 'bg-amber-500/20 text-amber-300 border-amber-600/50' },
  VSC: { label: 'VSC', className: 'bg-orange-500/20 text-orange-300 border-orange-600/50' },
  SAFETY_CAR: { label: 'SAFETY CAR', className: 'bg-red-500/20 text-red-300 border-red-700/60' },
  RED: { label: 'RED FLAG', className: 'bg-red-600/30 text-red-200 border-red-700/80' },
}

function Unavailable({ reason, hint }: { reason: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-amber-700/40 bg-amber-950/30 px-6 py-8 text-center">
      <span className="font-mono text-xs font-black uppercase tracking-widest text-amber-400">
        No data available
      </span>
      <span className="font-mono text-[11px] text-amber-300/80">{reason}</span>
      {hint && <span className="font-mono text-[11px] text-zinc-500 light:text-zinc-500">{hint}</span>}
    </div>
  )
}

/** Race clock as hours, minutes and seconds. */
function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—:—:—'
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
}

export default function PitWallApp() {
  const [view, setView] = useState<View>('landing')
  const [session, setSession] = useState<CurrentSession | null>(null)
  const [outline, setOutline] = useState<CircuitOutline | null>(null)
  const [frame, setFrame] = useState<RaceFrame | null>(null)
  const [driverStandings, setDriverStandings] = useState<DriverStanding[]>([])
  const [constructorStandings, setConstructorStandings] = useState<ConstructorStanding[]>([])
  const [selectedDriver, setSelectedDriver] = useState<number | null>(null)
  const [expandedDriver, setExpandedDriver] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [strategy, setStrategy] = useState<StrategyReport | null>(null)
  const [weather, setWeather] = useState<WeatherReport | null>(null)
  const [radio, setRadio] = useState<RadioReport | null>(null)
  const [alerts, setAlerts] = useState<PitWallAlert[]>([])
  const [smoothDrivers, setSmoothDrivers] = useState<DriverPosition[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const frameRef = useRef<RaceFrame | null>(null)
  const receivedAtRef = useRef<number>(0)
  const { theme, toggle: toggleTheme } = useTheme()

  // ---------------------------------------------------------------- load
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const load = async (): Promise<boolean> => {
      try {
        const [sessionData, outlineData, driverData, constructorData, frameData] = await Promise.all([
          api.currentSession(),
          api.currentOutline(),
          api.driverStandings(),
          api.constructorStandings(),
          api.frame(),
        ])
        if (cancelled) return true

        if (isUnavailable(sessionData)) {
          setError(`Session unavailable: ${sessionData.reason}`)
        } else {
          setSession(sessionData)
          // A failed attempt before the backend was up must not leave an
          // error on a page that has now loaded.
          setError(null)
        }

        if (isUnavailable(outlineData)) {
          // A live session builds its map from the cars' own positions, so an
          // outline that is still filling in is expected, not an error.
          if (outlineData.reason !== 'track_still_building') {
            setError(`Circuit outline unavailable: ${outlineData.reason}`)
          }
        } else {
          setOutline(outlineData)
        }

        if (!isUnavailable(driverData)) setDriverStandings(driverData)
        if (!isUnavailable(constructorData)) setConstructorStandings(constructorData)
        if (!isUnavailable(frameData)) setFrame(frameData)
        return true
      } catch {
        // The backend may still be starting; a page opened a second early
        // must not sit on "connecting" for ever.
        return false
      }
    }

    const attempt = async () => {
      const loaded = await load()
      if (!loaded && !cancelled) timer = setTimeout(attempt, 3000)
    }

    attempt()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  // ------------------------------------------------------------- live feed
  useEffect(() => {
    if (view !== 'live') return
    let closed = false
    let socket: WebSocket | null = null
    let pollTimer: number | undefined

    const applyFrame = (payload: unknown) => {
      if (closed) return
      if (isUnavailable(payload)) {
        setError(`Live feed unavailable: ${payload.reason}`)
        return
      }
      const data = payload as RaceFrame & {
        type?: string
        kind?: string
        driver_number?: number
        lap?: number | null
        payload?: Record<string, unknown>
        pitwall?: StrategyReport
      }

      if (data.type === 'alert') {
        setAlerts((current) => [
          ...current,
          {
            type: data.kind ?? 'ALERT',
            kind: data.kind,
            lap: data.lap ?? null,
            driver_number: data.driver_number ?? 0,
            payload: data.payload ?? {},
          },
        ])
        return
      }

      setFrame(data)
      frameRef.current = data
      receivedAtRef.current = performance.now()
      if (data.pitwall) setStrategy(data.pitwall)
      setSelectedDriver((current) => {
        if (current !== null) return current
        const first = data.drivers.find((d) => d.on_track) ?? data.drivers[0]
        return first ? first.driver_number : null
      })
    }

    const startPolling = () => {
      const tick = async () => {
        if (closed) return
        const payload = await api.frame()
        applyFrame(payload)
        pollTimer = window.setTimeout(tick, 1000)
      }
      tick()
    }

    try {
      const wsUrl = `${(import.meta.env.VITE_API_BASE as string | undefined) ?? 'http://localhost:8000'}`.replace(
        /^http/,
        'ws',
      )
      socket = new WebSocket(`${wsUrl}/ws/live`)
      wsRef.current = socket
      socket.onmessage = (event) => {
        try {
          applyFrame(JSON.parse(event.data))
        } catch {
          /* ignore malformed frames */
        }
      }
      socket.onerror = () => {
        socket?.close()
        socket = null
      }
      socket.onclose = () => {
        if (!closed) startPolling()
      }
    } catch {
      startPolling()
    }

    return () => {
      closed = true
      if (pollTimer) window.clearTimeout(pollTimer)
      socket?.close()
      wsRef.current = null
    }
  }, [view])

  // ------------------------------------------------------ smooth tracking
  // The server publishes one frame per second; the cars cover a lot of
  // ground in that time, so the browser interpolates between the position a
  // car is at now and the position it will be at on the next frame.  That
  // makes the motion continuous at any replay speed instead of stepping.
  useEffect(() => {
    if (view !== 'live') {
      setSmoothDrivers(frame?.drivers ?? [])
      return
    }

    const step = () => {
      const current = frameRef.current
      if (current) {
        const elapsed = (performance.now() - receivedAtRef.current) / 1000
        const speed = current.replay_speed || 1
        // A frame covers one second of race time.
        const span = Math.max(0.2, 1 / Math.max(speed, 0.2))
        const progress = Math.max(0, Math.min(1, elapsed / span))
        setSmoothDrivers(
          current.drivers.map((driver) => {
            const nextX = driver.next_view_x
            const nextY = driver.next_view_y
            if (
              driver.view_x === null ||
              driver.view_y === null ||
              nextX === null ||
              nextY === null ||
              nextX === undefined ||
              nextY === undefined
            ) {
              return driver
            }
            return {
              ...driver,
              view_x: driver.view_x + (nextX - driver.view_x) * progress,
              view_y: driver.view_y + (nextY - driver.view_y) * progress,
            }
          }),
        )
      }
    }

    // requestAnimationFrame gives a smooth 60 fps glide, but it is paused in
    // a background tab, so a timer keeps the positions tracking as well.
    let frameHandle = requestAnimationFrame(function loop() {
      step()
      frameHandle = requestAnimationFrame(loop)
    })
    const timer = window.setInterval(step, 50)

    return () => {
      cancelAnimationFrame(frameHandle)
      window.clearInterval(timer)
    }
  }, [view, frame])

  // ------------------------------------------------------- strategy + weather
  useEffect(() => {
    if (view !== 'live') return
    let cancelled = false
    let ticks = 0

    const load = async () => {
      const [report, weatherData, radioData] = await Promise.all([
        api.strategies(selectedDriver ?? undefined),
        api.weather(),
        api.radio(),
      ])
      if (cancelled) return
      if (!isUnavailable(report)) setStrategy(report)
      if (!isUnavailable(weatherData)) setWeather(weatherData)
      if (!isUnavailable(radioData)) setRadio(radioData)

      // The live track fills in as the cars circulate, so the map is
      // refreshed while a session is running.
      ticks += 1
      if (ticks % 3 === 0) {
        const outlineData = await api.currentOutline()
        if (!cancelled && !isUnavailable(outlineData)) setOutline(outlineData)
      }
    }

    load()
    const timer = window.setInterval(load, 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [view, selectedDriver])

  // --------------------------------------------------------- replay control
  // The pit wall plays at real-time speed.  There is no fast-forward: the
  // point of the thing is to watch the race, not to skim through it.
  useEffect(() => {
    if (view !== 'live') return
    api.replayControl('play', 1).catch(() => undefined)
  }, [view])

  // ------------------------------------------------------------------ chat
  // The conversational assistant has been taken out for now.  The panel it
  // lived in was taking the space the strategy, tyre and radio cards need,
  // and the answers it gave were the same numbers the cards already show.

  // ------------------------------------------------------------- selectors
  const drivers = useMemo(() => smoothDrivers, [smoothDrivers])
  const hasPositions = useMemo(
    () => drivers.some((d) => d.x !== null && d.y !== null),
    [drivers],
  )
  const selected = useMemo(
    () => drivers.find((d) => d.driver_number === selectedDriver) ?? null,
    [drivers, selectedDriver],
  )
  const activeScenarioDriver = useMemo(
    () =>
      driverStandings.find((d) => d.code === expandedDriver) ??
      driverStandings.find((d) => d.code === 'ANT') ??
      driverStandings[0] ??
      null,
    [driverStandings, expandedDriver],
  )

  const driverCodes = useMemo(() => {
    const map: Record<number, string> = {}
    for (const driver of drivers) map[driver.driver_number] = driver.code
    return map
  }, [drivers])

  /** One entry per team on track, so the marker colours can be read. */
  const teamLegend = useMemo(() => {
    const seen = new Map<string, { code: string; label: string; colour: string }>()
    for (const driver of drivers) {
      if (driver.team && !seen.has(driver.team)) {
        seen.set(driver.team, {
          code: driver.code,
          label: driver.team,
          colour: teamColour(driver.team_colour),
        })
      }
    }
    return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label))
  }, [drivers])

  /** The circuit outline as an SVG path, for the car to drive along. */
  const heroPath = useMemo(() => {
    if (!outline || !outline.points.length) return undefined
    return (
      outline.points
        .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`)
        .join(' ') + ' Z'
    )
  }, [outline])

  const flag = frame ? (FLAG_STYLE[frame.flag_state] ?? FLAG_STYLE.GREEN) : FLAG_STYLE.GREEN

  /** The most recent race-control message, used as the flag detail. */
  const flagDetail = useMemo(() => {
    const messages = frame?.messages as
      | { date: string | null; message: string; lap_number: number | null }[]
      | undefined
    if (!messages || !messages.length) return null
    const latest = messages[messages.length - 1]
    return latest.message
  }, [frame])

  const circuitFacts = useMemo(() => {
    const meta = (frame?.session_meta ?? {}) as Record<string, unknown>
    return {
      circuit: String(meta.circuit_short_name ?? session?.circuit ?? 'Austin'),
      year: Number(meta.year ?? session?.year ?? 2024),
      name: String(
        meta.session_name ?? session?.meeting_name ?? session?.session_type ?? 'Race',
      ),
      start: String(meta.date_start ?? ''),
    }
  }, [frame, session])

  const onSelectDriver = useCallback((driverNumber: number) => {
    setSelectedDriver(driverNumber)
  }, [])

  // ------------------------------------------------------------------ view
  return (
    <div className="pw-app min-h-screen text-zinc-200 light:text-zinc-800 font-sans selection:bg-red-600 selection:text-white antialiased">
      {/* ------------------------------------------------------------ nav */}
      <header className="flex items-center justify-between px-6 sm:px-10 py-4 border-b border-zinc-900 light:border-zinc-200 bg-[#020205] light:bg-zinc-50 dark:bg-[#020205] light:bg-white light:border-zinc-200 sticky top-0 z-50">
        <div className="flex items-center space-x-6">
          <button
            onClick={() => setView('landing')}
            className="cursor-pointer flex items-center space-x-2.5 bg-gradient-to-r from-red-600 to-rose-700 px-4 py-2 rounded-lg shadow-md"
          >
            <span className="font-black italic tracking-wider text-sm uppercase text-white font-mono">
              PitWall F1
            </span>
          </button>
          <div className="hidden md:flex items-center space-x-2.5 text-xs font-mono text-zinc-400 light:text-zinc-600">
            <span
              className={`w-2.5 h-2.5 rounded-full ${
                session?.mode === 'LIVE' ? 'bg-emerald-500 animate-pulse' : 'bg-amber-500'
              }`}
            />
            <span className="font-bold tracking-wide">
              {session
                ? `${session.circuit?.toUpperCase()} ${session.year} · ${
                    session.mode === 'LIVE' ? 'LIVE' : 'LATEST RACE'
                  }`
                : 'CONNECTING…'}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <nav className="hidden lg:flex items-center space-x-8 text-xs font-mono uppercase tracking-widest text-zinc-300 light:text-zinc-700 font-bold">
            {(
              [
                ['landing', 'Overview'],
                ['championship', 'World Championship'],
                ['live', 'Pit Wall Live'],
              ] as const
            ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setView(key)}
              className={`hover:text-red-500 transition-colors ${
                view === key ? 'text-red-500 border-b-2 border-red-600 pb-1 font-black' : ''
              }`}
            >
              {label}
            </button>
          ))}
          </nav>

          <div className="flex items-center gap-3">
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
          </div>
        </div>
      </header>

      {error && (
        <div className="mx-6 sm:mx-10 mt-4 p-3 rounded-lg bg-amber-950/40 border border-amber-800/60 text-xs font-mono text-amber-300 font-bold">
          {error}
        </div>
      )}

      {/* --------------------------------------------------------- landing */}
      {view === 'landing' && (
        <div className="relative overflow-hidden">
          {/* Ambient light wash behind the hero. */}
          <div className="pointer-events-none absolute inset-0 -z-10">
            <div className="absolute -top-40 left-1/3 h-[520px] w-[520px] rounded-full bg-red-600/10 blur-[120px]" />
            <div className="absolute top-1/3 right-0 h-[420px] w-[420px] rounded-full bg-zinc-800/40 blur-[120px]" />
          </div>

          <div className="max-w-7xl mx-auto px-6 sm:px-10 pt-14 pb-10">
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 items-center">
              {/* ---- the pitch ---- */}
              <div className="lg:col-span-6">
                <div className="text-red-500 font-mono text-[11px] uppercase tracking-[0.25em] mb-5 flex items-center space-x-2.5 font-bold">
                  <span className="w-2 h-2 rounded-full bg-red-600 animate-ping" />
                  <span>
                    {session?.mode === 'LIVE'
                      ? 'LIVE — FOLLOWING THE REAL SESSION'
                      : 'PIT WALL — REAL F1 STRATEGY DATA'}
                  </span>
                </div>

                <h1 className="font-sans font-black uppercase leading-[0.86] tracking-tight">
                  <span className="block text-5xl sm:text-7xl text-white">United</span>
                  <span className="block text-5xl sm:text-7xl text-white">States</span>
                  <span className="block text-5xl sm:text-7xl text-red-600">Grand Prix</span>
                </h1>

                <p className="text-zinc-400 light:text-zinc-600 font-mono text-sm mt-6 font-semibold max-w-md leading-relaxed">
                  Circuit of the Americas, Austin • {circuitFacts.year} •{' '}
                  {frame?.total_laps
                    ? `${frame.total_laps} laps`
                    : String(circuitFacts.name).toLowerCase()}{' '}
                  • {session?.mode === 'LIVE' ? 'live now' : 'the latest race'}
                </p>

                <div className="flex flex-wrap items-center gap-4 mt-8">
                  <button
                    onClick={() => setView('live')}
                    className="group relative inline-flex items-center gap-3 px-8 py-4 bg-red-600 hover:bg-red-500 text-white rounded-xl font-mono text-xs uppercase font-black tracking-[0.2em] transition-all shadow-[0_10px_40px_-10px_rgba(225,6,0,0.8)]"
                  >
                    <span className="absolute inset-0 rounded-xl bg-red-500 opacity-0 group-hover:opacity-25 transition-opacity" />
                    <span className="relative">Enter the pit wall</span>
                    <span className="relative transition-transform group-hover:translate-x-1">→</span>
                  </button>
                  <button
                    onClick={() => setView('championship')}
                    className="px-8 py-4 bg-white/5 hover:bg-white/10 border border-zinc-700 light:border-zinc-300 hover:border-zinc-500 text-zinc-100 rounded-xl font-mono text-xs uppercase font-black tracking-[0.2em] transition-all"
                  >
                    Championship
                  </button>
                </div>

                <div className="flex flex-wrap gap-x-10 gap-y-4 mt-12 pt-8 border-t border-zinc-900 light:border-zinc-200">
                  {[
                    ['CIRCUIT', circuitFacts.circuit],
                    ['LAPS', String(frame?.total_laps ?? '—')],
                    ['DRIVERS', String(drivers.length || 20)],
                    ['FLAG', frame?.flag_state ?? '—'],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <span className="text-zinc-600 block mb-1 uppercase font-bold text-[10px] tracking-[0.2em]">
                        {label}
                      </span>
                      <span className="text-zinc-100 font-black text-lg">{value}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* ---- the circuit, with a car on it ---- */}
              <div className="lg:col-span-6">
                <div className="relative rounded-3xl border border-zinc-800 light:border-zinc-200 bg-black/60 light:bg-white backdrop-blur-sm p-6 shadow-2xl">
                  <div className="absolute inset-0 rounded-3xl bg-[radial-gradient(circle_at_30%_20%,rgba(225,6,0,0.12),transparent_55%)]" />
                  {outline ? (
                    <>
                      <div className="relative h-[300px] sm:h-[420px]">
                        <TrackMap
                          outline={outline}
                          drivers={[]}
                          showLabels={false}
                          showCorners={false}
                          carPath={heroPath}
                        />
                      </div>
                      <div className="relative mt-3 flex items-center justify-between font-mono text-[10px] font-bold text-zinc-600">
                        <span>
                          {outline.points.length} points from OpenF1 GPS •{' '}
                          {outline.corners.length} corners
                        </span>
                        <span className="text-red-600/80">the layout the cars actually drive</span>
                      </div>
                    </>
                  ) : (
                    <Unavailable
                      reason="Circuit outline unavailable"
                      hint="Run scripts/build_track_outlines.py"
                    />
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* ---- what the pit wall does ---- */}
          <div className="max-w-7xl mx-auto px-6 sm:px-10 py-14 grid grid-cols-1 md:grid-cols-3 gap-6">
            {[
              {
                title: 'Live car positions',
                body: 'Cars plotted on the real circuit outline whenever F1 publishes positions, in team colour. When it does not, the map shows the outline and says so.',
              },
              {
                title: 'Real strategy',
                body: 'Tyre life, pace against the car ahead and a ranked stop plan, projected from a model fitted to real lap times.',
              },
              {
                title: 'Everything traceable',
                body: 'No invented numbers. If the data is not there, the pit wall says so rather than guessing.',
              },
            ].map((item) => (
              <div
                key={item.title}
                className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-zinc-900 light:bg-zinc-100 p-6 hover:border-zinc-700 light:border-zinc-300 transition-colors"
              >
                <h3 className="font-mono font-black uppercase tracking-wider text-white text-sm mb-3">
                  {item.title}
                </h3>
                <p className="text-zinc-400 light:text-zinc-600 text-sm leading-relaxed font-sans">{item.body}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* --------------------------------------------------- championship */}
      {view === 'championship' && (
        <div className="max-w-7xl mx-auto px-6 sm:px-10 py-10 space-y-8">
          <div className="flex items-center justify-between pb-6 border-b border-zinc-900 light:border-zinc-200">
            <div>
              <h2 className="text-3xl sm:text-4xl font-black italic uppercase tracking-wider text-white font-sans">
                2026 World Championship
              </h2>
              <p className="text-zinc-300 light:text-zinc-700 text-sm font-mono mt-1 font-semibold">
                Standings after round 17 • title odds computed by Monte-Carlo simulation
              </p>
            </div>
            <button
              onClick={() => setView('landing')}
              className="px-5 py-2.5 bg-zinc-900 light:bg-zinc-100 border border-zinc-700 light:border-zinc-300 rounded-xl text-xs font-mono text-zinc-200 light:text-zinc-800 hover:text-white uppercase font-black tracking-wider"
            >
              ← Back to Overview
            </button>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div className="lg:col-span-7 rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-8 space-y-5 shadow-xl">
              <h3 className="text-lg font-black italic uppercase tracking-wider text-white font-mono">
                Drivers' Championship
              </h3>
              <p className="font-mono text-[11px] text-zinc-500 light:text-zinc-500 -mt-3">
                Click a driver to see everything they still need to do.
              </p>
              <div className="space-y-3.5 font-mono text-sm max-h-[820px] overflow-y-auto pr-3">
                {driverStandings.length === 0 && (
                  <Unavailable reason="Championship standings unavailable" />
                )}
                {driverStandings.map((d) => {
                  const isOpen = expandedDriver === d.code
                  const hasChance = d.title_probability > 0.05
                  return (
                    <div
                      key={d.code}
                      className={`rounded-xl border-2 bg-zinc-900 light:bg-zinc-100 transition-colors ${
                        isOpen ? 'border-red-600' : 'border-zinc-800 light:border-zinc-200 hover:border-zinc-700 light:border-zinc-300'
                      }`}
                    >
                      <button
                        onClick={() =>
                          setExpandedDriver(isOpen ? null : d.code)
                        }
                        className="w-full flex items-center justify-between p-4 text-left"
                      >
                        <div className="flex items-center space-x-4">
                          <span
                            className={`font-black italic text-lg w-10 ${
                              d.position <= 3 ? 'text-amber-400' : 'text-zinc-400 light:text-zinc-600'
                            }`}
                          >
                            P{d.position}
                          </span>
                          <TeamLogo teamKey={d.team_logo_svg} className="w-14 h-10" />
                          <span className="font-black italic text-white px-3 py-1 rounded bg-zinc-800 tracking-wider">
                            {d.code}
                          </span>
                          <div>
                            <span className="text-zinc-100 font-bold text-base block">
                              {d.name}
                            </span>
                            <span className="text-zinc-400 light:text-zinc-600 text-xs font-semibold">
                              {d.team} • #{d.driver_number} • {d.wins} Wins • {d.podiums}{' '}
                              Podiums
                            </span>
                            {d.required_results.length > 0 && (
                              <span className="block text-[11px] font-semibold text-amber-500/90 mt-1 leading-snug">
                                → {d.required_results[0]}
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center space-x-8 text-right">
                          <div>
                            <span className="text-white font-black text-lg block">
                              {d.points} PTS
                            </span>
                            <span className="text-zinc-500 light:text-zinc-500 text-[10px] font-bold uppercase">
                              points
                            </span>
                          </div>
                          <div className="w-32">
                            <div className="flex justify-between text-xs text-zinc-300 light:text-zinc-700 mb-1 font-bold">
                              <span>Title odds</span>
                              <span
                                className={`font-black text-sm ${
                                  hasChance ? 'text-emerald-400' : 'text-zinc-500 light:text-zinc-500'
                                }`}
                              >
                                {d.title_probability}%
                              </span>
                            </div>
                            <div className="w-full bg-zinc-800 h-2.5 rounded-full overflow-hidden">
                              <div
                                className="h-full rounded-full bg-emerald-500"
                                style={{ width: `${Math.max(1, d.title_probability)}%` }}
                              />
                            </div>
                          </div>
                        </div>
                      </button>

                      {isOpen && (
                        <div className="px-4 pb-4 space-y-3">
                          <div className="pt-3 border-t border-zinc-800 light:border-zinc-200 space-y-2">
                            <span className="text-zinc-400 light:text-zinc-600 text-[10px] font-black uppercase tracking-widest block">
                              What is required to become champion
                            </span>
                            <ul className="space-y-1.5">
                              {d.required_results.map((req, idx) => (
                                <li
                                  key={idx}
                                  className="flex items-start space-x-2 text-zinc-300 light:text-zinc-700 text-xs"
                                >
                                  <span className="text-red-500 font-black">•</span>
                                  <span className="leading-snug">{req}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                          <p className="text-zinc-500 light:text-zinc-500 text-[11px] leading-relaxed">
                            {d.scenario_summary}
                          </p>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>

            <div className="lg:col-span-5 space-y-6">
              <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-6 shadow-xl">
                <h3 className="text-base font-black italic uppercase tracking-wider text-white font-mono mb-1">
                  Title prediction
                </h3>
                <p className="font-mono text-[11px] text-zinc-500 light:text-zinc-500 mb-4">
                  Monte-Carlo over the remaining rounds — every driver's chance
                  of ending the season as world champion.
                </p>
                <div className="space-y-2">
                  {driverStandings.map((d) => (
                    <button
                      key={d.code}
                      onClick={() => setExpandedDriver(d.code)}
                      className="w-full flex items-center gap-3 group"
                    >
                      <span className="font-black italic text-zinc-300 light:text-zinc-700 w-9 text-left group-hover:text-white">
                        {d.code}
                      </span>
                      <div className="flex-1 h-2.5 rounded-full bg-zinc-800 overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            d.title_probability > 20
                              ? 'bg-emerald-500'
                              : d.title_probability > 1
                                ? 'bg-amber-500'
                                : 'bg-zinc-600'
                          }`}
                          style={{ width: `${Math.max(0.6, d.title_probability)}%` }}
                        />
                      </div>
                      <span
                        className={`font-black text-xs w-14 text-right tabular-nums ${
                          d.title_probability > 1 ? 'text-zinc-200 light:text-zinc-800' : 'text-zinc-500 light:text-zinc-500'
                        }`}
                      >
                        {d.title_probability}%
                      </span>
                    </button>
                  ))}
                </div>
                {driverStandings.length === 0 && (
                  <Unavailable reason="Championship standings unavailable" />
                )}
              </div>

              {activeScenarioDriver && (
                <div className="rounded-2xl border-2 border-red-900/60 bg-[#09090b] light:bg-white p-8 font-mono space-y-5 shadow-xl">
                  <div className="flex items-center justify-between pb-4 border-b border-zinc-800 light:border-zinc-200">
                    <div className="flex items-center space-x-4">
                      <TeamLogo teamKey={activeScenarioDriver.team_logo_svg} className="w-14 h-10" />
                      <div>
                        <span className="font-black italic text-white text-lg uppercase tracking-wider block">
                          {activeScenarioDriver.name} ({activeScenarioDriver.code})
                        </span>
                        <span className="text-zinc-300 light:text-zinc-700 text-xs font-semibold">
                          {activeScenarioDriver.team}
                        </span>
                      </div>
                    </div>
                    <span className="text-emerald-400 font-black text-sm px-3 py-1.5 rounded-xl bg-emerald-950 border border-emerald-700">
                      {activeScenarioDriver.title_probability}% ODDS
                    </span>
                  </div>
                  <p className="text-zinc-200 light:text-zinc-800 text-sm leading-relaxed font-sans">
                    {activeScenarioDriver.scenario_summary}
                  </p>
                  <span className="block text-zinc-400 light:text-zinc-600 text-[10px] font-black uppercase tracking-widest">
                    What is required to become champion
                  </span>
                  <ul className="space-y-2.5">
                    {activeScenarioDriver.required_results.map((req, idx) => (
                      <li
                        key={idx}
                        className="flex items-start space-x-3 text-zinc-200 light:text-zinc-800 text-sm bg-zinc-900 light:bg-zinc-100 p-3.5 rounded-xl border border-zinc-800 light:border-zinc-200"
                      >
                        <span className="text-red-500 font-black text-base">•</span>
                        <span className="leading-snug">{req}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-8 space-y-5 shadow-xl">
                <h3 className="text-lg font-black italic uppercase tracking-wider text-white font-mono">
                  Constructors' Championship
                </h3>
                <div className="space-y-3.5 font-mono text-sm max-h-[380px] overflow-y-auto pr-3">
                  {constructorStandings.length === 0 && (
                    <Unavailable reason="Constructor standings unavailable" />
                  )}
                  {constructorStandings.map((c) => (
                    <div
                      key={c.team_name}
                      className="flex items-center justify-between p-3.5 rounded-xl bg-zinc-900 light:bg-zinc-100 border border-zinc-800 light:border-zinc-200"
                    >
                      <div className="flex items-center space-x-4">
                        <span className="font-black italic text-zinc-400 light:text-zinc-600 w-8">P{c.position}</span>
                        <TeamLogo teamKey={c.team_logo_svg} className="w-12 h-8" />
                        <span className="font-bold text-white text-base">{c.team_name}</span>
                      </div>
                      <div className="text-right">
                        <span className="text-zinc-100 font-black text-base">{c.points} PTS</span>
                        <span className="text-zinc-400 light:text-zinc-600 text-xs ml-2 font-semibold">({c.wins} W)</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* --------------------------------------------------------- live */}
      {view === 'live' && (
        <div className="max-w-[1600px] mx-auto px-6 sm:px-10 py-8 space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-4 pb-5 border-b border-zinc-900 light:border-zinc-200">
            <div>
              <h2 className="text-3xl font-black italic uppercase tracking-wider text-white light:text-zinc-900 font-sans">
                Pit Wall Live —{' '}
                {session?.mode === 'LIVE' && circuitFacts.name
                  ? circuitFacts.name
                  : `${circuitFacts.circuit} GP`}
              </h2>
              <p className="text-zinc-300 light:text-zinc-700 text-sm font-mono mt-1 font-semibold">
                {circuitFacts.circuit.toUpperCase()} {circuitFacts.year} ·{' '}
                {frame?.total_laps
                  ? `LAP ${frame?.lap ?? '—'} OF ${frame.total_laps}`
                  : String(circuitFacts.name).toUpperCase()}
              </p>
            </div>
            <span
              className={`px-3 py-1.5 rounded-lg border font-mono text-xs font-black tracking-widest ${flag.className}`}
            >
              {flag.label}
            </span>
          </div>

          <div className="space-y-4">
            <RaceTicker
              drivers={drivers}
              lap={frame?.lap ?? null}
              totalLaps={frame?.total_laps ?? null}
              flagState={frame?.flag_state ?? 'GREEN'}
              elapsed={frame?.elapsed_s ?? 0}
              focusCode={selected?.code ?? null}
              onSelect={onSelectDriver}
            />
            <AlertBanner
              alerts={alerts}
              driverCodes={driverCodes}
              flagState={frame?.flag_state ?? 'GREEN'}
              detail={flagDetail}
            />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-12 gap-6 mt-6">
            {/* map + timing tower */}
            <div className="xl:col-span-8 space-y-6">
              <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-6 shadow-xl">
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center space-x-3">
                    <span className="w-3 h-3 rounded-full bg-emerald-500 animate-pulse" />
                    <span className="text-xs font-mono font-black text-zinc-200 light:text-zinc-800 uppercase tracking-widest">
                      {circuitFacts.circuit} Circuit —{' '}
                      {session?.mode === 'LIVE'
                        ? hasPositions
                          ? 'Live positions'
                          : 'Live session'
                        : 'GPS Telemetry Map'}
                    </span>
                  </div>
                  <span className="text-xs font-mono px-3 py-1 rounded bg-zinc-900 light:bg-zinc-100 text-emerald-400 border border-zinc-800 light:border-zinc-200 font-bold">
                    {session?.mode === 'LIVE' && !hasPositions
                      ? 'LIVE POSITIONS UNAVAILABLE'
                      : `${drivers.filter((d) => d.on_track).length} CARS ON TRACK`}
                  </span>
                </div>
                {outline ? (
                  <div className="relative h-[420px] sm:h-[540px] w-full overflow-hidden">
                    <div className="absolute inset-0 opacity-10 bg-[linear-gradient(to_right,#ef444415_1px,transparent_1px),linear-gradient(to_bottom,#ef444415_1px,transparent_1px)] bg-[size:24px_24px]" />
                    <TrackMap
                      outline={outline}
                      drivers={drivers}
                      selectedDriver={selectedDriver}
                      onSelectDriver={onSelectDriver}
                    />
                  </div>
                ) : (
                  <Unavailable
                    reason={
                      session?.mode === 'LIVE'
                        ? "Live car positions are not available from F1's free feed."
                        : 'Circuit outline unavailable'
                    }
                  />
                )}
                {session?.mode === 'LIVE' && !hasPositions && (
                  <p className="mt-3 font-mono text-[11px] leading-relaxed text-zinc-500 light:text-zinc-500">
                    Timing, tyres, gaps and radio are live from F1&apos;s feed.
                    This feed is not publishing car positions, so the map
                    cannot place the cars.
                  </p>
                )}
                {drivers.length > 0 && (
                  <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[10px] text-zinc-500 light:text-zinc-500">
                    {teamLegend.map((team) => (
                      <span key={team.code} className="flex items-center gap-1.5">
                        <span
                          className="w-2.5 h-2.5 rounded-sm"
                          style={{ backgroundColor: team.colour }}
                        />
                        {team.label}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-6 shadow-xl">
                <h3 className="text-base font-black italic uppercase tracking-wider text-white mb-4 font-mono">
                  Timing Tower
                </h3>
                <div className="max-h-[420px] overflow-y-auto pr-2">
                  <TimingTower
                    drivers={drivers}
                    selectedDriver={selectedDriver}
                    onSelect={onSelectDriver}
                  />
                </div>
              </div>
            </div>

            {/* telemetry + strategy + weather + chat */}
            <div className="xl:col-span-4 space-y-6">
              <StrategyCard report={strategy} />
              <TyreWidget
                tyre={strategy?.tyre ?? null}
                available={Boolean(strategy?.available && strategy?.tyre)}
                reason={
                  strategy?.reason ??
                  (session?.mode === 'LIVE'
                    ? 'No degradation model for this circuit and season. The live compound and age are in the timing tower.'
                    : undefined)
                }
              />
              <WeatherStrip weather={weather} />
              <RadioPanel radio={radio} driverCodes={driverCodes} />
              <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-6 shadow-xl">
                <div className="flex items-center justify-between mb-5 pb-3 border-b border-zinc-800 light:border-zinc-200">
                  <div className="flex items-center space-x-2.5">
                    <span className="w-3 h-3 rounded-full bg-emerald-500 animate-pulse" />
                    <span className="text-xs font-mono font-black text-zinc-200 light:text-zinc-800 uppercase tracking-widest">
                      Telemetry: {selected?.code ?? '—'}
                    </span>
                  </div>
                </div>

                {!selected ? (
                  <Unavailable reason="No driver selected" />
                ) : (
                  <div className="p-5 rounded-xl bg-zinc-900 light:bg-zinc-100 border border-zinc-800 light:border-zinc-200 font-mono text-sm space-y-3.5">
                    {[
                      ['SPEED', selected.speed !== null ? `${selected.speed} km/h` : '—', 'text-white'],
                      [
                        'SPEED TRAP',
                        selected.speed_trap !== null ? `${selected.speed_trap} km/h` : '—',
                        'text-cyan-300',
                      ],
                      [
                        'GEAR / RPM',
                        `Gear ${selected.gear ?? '—'} (${selected.rpm ?? '—'} RPM)`,
                        'text-amber-400',
                      ],
                      [
                        'DRS STATUS',
                        selected.drs_open ? 'ENGAGED (ON)' : 'CLOSED (OFF)',
                        selected.drs_open ? 'text-cyan-400' : 'text-zinc-400 light:text-zinc-600',
                      ],
                    ].map(([label, value, colour]) => (
                      <div
                        key={label as string}
                        className="flex justify-between items-center text-zinc-400 light:text-zinc-600 border-b border-zinc-800 light:border-zinc-200 pb-2.5 last:border-0"
                      >
                        <span className="font-bold">{label}</span>
                        <span className={`font-black text-base ${colour}`}>{value}</span>
                      </div>
                    ))}

                    <div className="pt-1">
                      <PedalIndicator
                        throttle={selected.throttle}
                        brake={selected.brake}
                      />
                    </div>

                    {session?.mode === 'LIVE' && selected.speed === null && (
                      <p className="text-[11px] leading-relaxed text-zinc-500 light:text-zinc-500">
                        F1&apos;s free feed is not publishing continuous car
                        telemetry for this session. The speed trap comes from
                        the timing feed.
                      </p>
                    )}

                    <div className="pt-1 text-xs text-zinc-500 light:text-zinc-500 font-bold">
                      {selected.name} • {selected.team} • Lap {selected.lap ?? '—'}
                    </div>
                    {strategy?.pace?.pace_delta_s_per_lap !== null &&
                      strategy?.pace?.pace_delta_s_per_lap !== undefined && (
                        <div className="pt-1 text-xs font-bold">
                          <span className="text-zinc-500 light:text-zinc-500">PACE </span>
                          <span
                            className={
                              strategy.pace.pace_delta_s_per_lap > 0
                                ? 'text-red-400'
                                : 'text-emerald-400'
                            }
                          >
                            {strategy.pace.pace_delta_s_per_lap > 0 ? '+' : ''}
                            {strategy.pace.pace_delta_s_per_lap.toFixed(3)}s/lap
                          </span>
                          <span className="text-zinc-500 light:text-zinc-500">
                            {' '}
                            vs {strategy.pace.rival_code ?? '—'}
                          </span>
                        </div>
                      )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
