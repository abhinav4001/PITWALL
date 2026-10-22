import React from 'react'

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

interface WeatherStripProps {
  weather: WeatherReport | null
}

/** The compass point for a wind direction, so it reads at a glance. */
const COMPASS = [
  'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW',
]

function compass(degrees: number | null | undefined): string {
  if (degrees === null || degrees === undefined || !Number.isFinite(degrees)) {
    return '-'
  }
  const index = Math.round(((degrees % 360) + 360) % 360 / 22.5) % 16
  return COMPASS[index]
}

function round(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return value.toFixed(digits)
}

/**
 * The weather strip.
 *
 * Everything here is the **real** per-minute feed published with the
 * timing data: air and track temperature, humidity, pressure, wind and
 * rainfall.  The animation appears only when the feed is actually
 * reporting rain, so a dry race does not pretend to be wet.
 */
export function WeatherStrip({ weather }: WeatherStripProps) {
  if (!weather || !weather.available) {
    return (
      <div className="rounded-2xl border border-zinc-800/60 light:border-zinc-200 bg-[#09090b]/60 light:bg-white/60 px-4 py-3 font-mono">
        <span className="text-[11px] font-black uppercase tracking-widest text-zinc-500">
          Weather
        </span>
        <p className="text-[11px] text-zinc-500 mt-1">
          {weather?.reason_detail ?? weather?.reason ?? 'No weather feed for this session.'}
        </p>
      </div>
    )
  }

  const current = weather.current ?? weather.readings?.[weather.readings.length - 1]
  const raining = Boolean(current?.rainfall)
  const slippery = weather.condition === 'slippery'
  const wet = raining || slippery

  let label = 'TRACK DRY'
  let colour = 'text-zinc-300 light:text-zinc-700'
  if (raining) {
    label = 'RAIN'
    colour = 'text-blue-300'
  } else if (slippery) {
    label = 'SLIPPERY'
    colour = 'text-sky-300'
  } else if ((weather.rain_risk_percent ?? 0) >= 30) {
    label = 'RAIN LIKELY'
    colour = 'text-blue-300'
  }

  return (
    <div
      className={`rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white px-4 py-3 space-y-3 relative overflow-hidden ${
        wet ? 'weather-wet' : ''
      }`}
    >
      {/* The animation only appears when the feed reports rain. */}
      {wet && (
        <div className="pointer-events-none absolute inset-0 opacity-25" aria-hidden>
          {Array.from({ length: 12 }).map((_, index) => (
            <span
              key={index}
              className="absolute w-0.5 h-3 rounded-full bg-sky-400 animate-rain"
              style={{
                left: `${(index * 8.1) % 100}%`,
                animationDelay: `${(index % 7) * 0.28}s`,
                animationDuration: `${0.9 + (index % 4) * 0.22}s`,
              }}
            />
          ))}
        </div>
      )}

      <div className="relative flex items-center justify-between">
        <span className="font-mono text-[11px] font-black uppercase tracking-widest text-zinc-500">
          Weather
        </span>
        <span className={`font-mono text-xs font-black ${colour}`}>{label}</span>
      </div>

      <div className="relative grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-[11px]">
        <Metric label="Air" value={`${round(current?.air_temperature)}°C`} />
        <Metric label="Track" value={`${round(current?.track_temperature)}°C`} />
        <Metric label="Humidity" value={`${round(current?.humidity, 0)}%`} />
        <Metric
          label="Wind"
          value={`${round(current?.wind_speed)} m/s ${compass(current?.wind_direction)}`}
        />
      </div>

      <div className="relative flex items-center justify-between font-mono text-[10px] font-bold text-zinc-500">
        <span>
          {wet ? 'Racing in the wet' : 'Racing on a dry track'}
        </span>
        <span>{current?.date ? current.date.slice(11, 19) : ''}</span>
      </div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-zinc-500 uppercase tracking-wider">{label}</span>
      <span className="text-zinc-200 light:text-zinc-800 font-bold tabular-nums">{value}</span>
    </div>
  )
}
