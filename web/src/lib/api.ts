/**
 * Typed client for the PitWall API.
 *
 * Every endpoint can answer with an explicit `{ available: false, reason }`
 * payload, so the client always returns a discriminated result and the UI can
 * render an honest "no data" state instead of a placeholder number.
 */

export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? 'http://localhost:8000'

export interface Unavailable {
  available: false
  reason: string
  source: 'unavailable'
}

export function isUnavailable(value: unknown): value is Unavailable {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { available?: unknown }).available === false
  )
}

/**
 * Normalise a team colour to a CSS colour.
 *
 * OpenF1 reports the colour as a bare hex string ("3671C6") with no leading
 * "#".  Handing that to an SVG `fill` is invalid, so the browser silently
 * ignores it and the marker falls back to the default colour.
 */
export function teamColour(raw: string | null | undefined): string {
  if (!raw) return '#71717a'
  let text = raw.trim()
  if (!text) return '#71717a'
  if (!text.startsWith('#')) text = `#${text}`
  if (text.length === 4) {
    text = `#${text[1]}${text[1]}${text[2]}${text[2]}${text[3]}${text[3]}`
  }
  return text.length === 7 ? text : '#71717a'
}

export interface DriverPosition {
  driver_number: number
  code: string
  name: string
  team: string
  team_colour: string
  position: number | null
  x: number | null
  y: number | null
  z: number | null
  view_x: number | null
  view_y: number | null
  /** Where the car will be on the next frame, for smooth interpolation. */
  next_view_x?: number | null
  next_view_y?: number | null
  speed: number | null
  throttle: number | null
  brake: number | null
  gear: number | null
  rpm: number | null
  drs: number | null
  drs_open: boolean
  gap_to_leader: number | null
  interval: number | null
  compound: string | null
  tyre_age: number | null
  lap: number | null
  last_lap_s: number | null
  best_lap_s: number | null
  speed_trap: number | null
  on_track: boolean
  /** Whether the timing feed is still reporting for this driver. */
  reporting: boolean
}

export interface OutlineCorner {
  number: number
  letter: string
  x: number
  y: number
}

export interface CircuitOutline {
  available: true
  source: string
  circuit: string
  year: number
  points: { x: number; y: number }[]
  corners: OutlineCorner[]
  start_finish: { x: number; y: number } | null
  view_box: { width: number; height: number }
  projection: {
    min_x: number
    max_x: number
    min_y: number
    max_y: number
    flip_y: boolean
    scale: number
  }
  metadata: Record<string, unknown>
}

export interface RaceFrame {
  available: true
  type?: string
  session_key: number
  circuit: string
  year: number
  timestamp: string
  elapsed_s: number
  flag_state: 'GREEN' | 'YELLOW' | 'VSC' | 'SAFETY_CAR' | 'RED'
  replay_speed: number
  lap: number | null
  total_laps: number | null
  drivers: DriverPosition[]
  source: string
  session_meta?: Record<string, unknown>
}

export interface CurrentSession {
  available: boolean
  source: string
  mode?: string
  session_key?: number
  circuit?: string
  year?: number
  meeting_name?: string
  session_type?: string
  date_start?: string
  date_end?: string
  total_laps?: number | null
  status?: string
  reason?: string
  next_session?: Record<string, unknown>
}

export interface StrategyOption {
  strategy_id: string
  label: string
  pit_lap: number | null
  compound: string | null
  expected_total_s: number
  p10_s: number
  p50_s: number
  p90_s: number
  delta_vs_current_s: number
  p_beat_rival: number | null
  /** Where the driver would end up under this strategy. */
  position_after: number | null
  /** The car immediately ahead of them afterwards. */
  ahead_of: string | null
  /** The car immediately behind them afterwards. */
  behind_of: string | null
}

export interface StrategyReport {
  available: boolean
  source?: string
  driver_number?: number
  circuit?: string
  year?: number
  lap?: number | null
  total_laps?: number | null
  flag_state?: string
  tyre?: {
    compound: string | null
    age_laps: number | null
    stint_number: number | null
    laps_remaining: number | null
    degradation_s: number | null
    projected_cliff_lap: number | null
    cliff_laps_away: number | null
  }
  pace?: {
    rival_code: string | null
    rival_driver_number: number | null
    gap_to_rival_s: number | null
    pace_delta_s_per_lap: number | null
    reference: string | null
  }
  options?: StrategyOption[]
  optimal_pit_lap?: number | null
  optimal_window?: number[] | null
  recommendation?: string
  recommendation_reason?: string
  alerts?: { type: string; lap: number | null; driver_number: number; payload: Record<string, unknown> }[]
  messages?: { date: string | null; message: string; lap_number: number | null }[]
  modelled_compounds?: string[]
  compound_tradeoff?: {
    offset_s_per_lap: Record<string, number>
    agreement: number
    samples: number
    reliable: boolean
    note: string
  }
  reason?: string
}

export interface WeatherReading {
  date: string | null
  air_temperature: number | null
  track_temperature: number | null
  humidity: number | null
  pressure: number | null
  wind_speed: number | null
  wind_direction: number | null
  rainfall: number | null
}

export interface WeatherReport {
  available: boolean
  source?: string
  rain_risk_percent?: number | null
  condition?: 'clear' | 'slippery' | 'wet'
  clear_reports?: number
  slippery_reports?: number
  readings?: WeatherReading[]
  current?: WeatherReading
  note?: string
  reason?: string
  reason_detail?: string
}

export interface RadioMessage {
  date: string | null
  driver_number: number | null
  team: string | null
  text: string
  recording_url?: string | null
  duration: number | null
}

export interface RadioReport {
  available: boolean
  source?: string
  reason?: string
  count?: number
  detail?: string
  messages: RadioMessage[]
}

export interface ChatResponse {
  available: boolean
  source?: string
  message?: string
  reason?: string
  driver_number?: number
  strategy?: { strategy_id: string; stints: { compound: string; start_lap: number; end_lap: number }[] }
  key_numbers?: Record<string, number>
  n_sims?: number
}

export interface DriverStanding {
  position: number
  driver_number: number
  code: string
  name: string
  team: string
  team_logo_svg: string
  points: number
  wins: number
  podiums: number
  title_probability: number
  scenario_summary: string
  required_results: string[]
}

export interface ConstructorStanding {
  position: number
  team_name: string
  team_logo_svg: string
  points: number
  wins: number
  podiums: number
}

async function getJson<T>(path: string): Promise<T | Unavailable> {
  try {
    const response = await fetch(`${API_BASE}${path}`)
    if (!response.ok) {
      return { available: false, reason: `http_${response.status}`, source: 'unavailable' }
    }
    const data = (await response.json()) as T
    return data
  } catch (error) {
    return {
      available: false,
      reason: error instanceof Error ? error.message : 'network_error',
      source: 'unavailable',
    }
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T | Unavailable> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      return { available: false, reason: `http_${response.status}`, source: 'unavailable' }
    }
    return (await response.json()) as T
  } catch (error) {
    return {
      available: false,
      reason: error instanceof Error ? error.message : 'network_error',
      source: 'unavailable',
    }
  }
}

export const api = {
  currentSession: () => getJson<CurrentSession>('/api/sessions/current'),
  outline: (circuit: string, year?: number) =>
    getJson<CircuitOutline>(
      `/api/circuits/${encodeURIComponent(circuit)}/outline${year ? `?year=${year}` : ''}`,
    ),
  currentOutline: () => getJson<CircuitOutline>('/api/outline/current'),
  frame: (at?: number) =>
    getJson<RaceFrame>(`/api/positions${at !== undefined ? `?at=${at}` : ''}`),
  timing: (at?: number) => getJson<RaceFrame>(`/api/timing${at !== undefined ? `?at=${at}` : ''}`),
  strategies: (driverNumber?: number, at?: number) => {
    const params = new URLSearchParams()
    if (driverNumber !== undefined) params.set('driver_number', String(driverNumber))
    if (at !== undefined) params.set('at', String(at))
    const query = params.toString()
    return getJson<StrategyReport>(`/api/strategies${query ? `?${query}` : ''}`)
  },
  weather: () => getJson<WeatherReport>('/api/weather'),
  radio: () => getJson<RadioReport>('/api/radio'),
  messages: () => getJson<{ available: boolean; messages: { date: string | null; message: string; lap_number: number | null }[] }>('/api/messages'),
  drivers: () => getJson<{ available: boolean; drivers: Record<string, unknown>[] }>('/api/drivers'),
  driverStandings: () => getJson<DriverStanding[]>('/api/championship/drivers'),
  constructorStandings: () => getJson<ConstructorStanding[]>('/api/championship/constructors'),
  replayControl: (action: string, speed?: number, at?: number) => {
    const params = new URLSearchParams({ action })
    if (speed !== undefined) params.set('speed', String(speed))
    if (at !== undefined) params.set('at', String(at))
    return getJson<Record<string, unknown>>(`/api/replay/control?${params.toString()}`)
  },
  chat: (query: string, driverNumber?: number) =>
    postJson<ChatResponse>('/api/chat', {
      query,
      driver_number: driverNumber ?? null,
    }),
}

export function formatGap(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds >= 999) return '+1 LAP'
  if (seconds <= 0) return 'LEADER'
  return `+${seconds.toFixed(3)}s`
}

export function formatLapTime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const minutes = Math.floor(seconds / 60)
  const rest = seconds - minutes * 60
  return `${minutes}:${rest.toFixed(3).padStart(6, '0')}`
}

/** The race clock, as hours, minutes and seconds. */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return '-'
  }
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  // Spelled out, so nobody has to guess whether "0:27:25" is minutes or
  // hours.
  if (hours > 0) {
    return `${hours}h ${String(minutes).padStart(2, '0')}m ${String(secs).padStart(2, '0')}s`
  }
  return `${minutes}m ${String(secs).padStart(2, '0')}s`
}
