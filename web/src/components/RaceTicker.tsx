import React, { useEffect, useMemo, useState } from 'react'
import { formatClock, teamColour, type DriverPosition } from '../lib/api'

interface RaceTickerProps {
  drivers: DriverPosition[]
  lap: number | null
  totalLaps: number | null
  flagState: string
  elapsed: number
  focusCode?: string | null
  onSelect?: (driverNumber: number) => void
}

/** How long each driver stays in the round-robin before the next one. */
const ROTATE_MS = 3500

/**
 * The top strip.
 *
 * The order and the gaps are real, from the ``/intervals`` feed.  The three
 * driver cards **move round the field** like a round robin, so the whole
 * order is readable on a glance, and each card carries the driver's name and
 * the gap to the car in front.
 */
export function RaceTicker({
  drivers,
  lap,
  totalLaps,
  flagState,
  elapsed,
  focusCode,
  onSelect,
}: RaceTickerProps) {
  const [offset, setOffset] = useState(0)

  // Walk the field so every driver gets their turn in the strip.
  useEffect(() => {
    if (drivers.length <= 3) return
    const timer = window.setInterval(
      () => setOffset((current) => (current + 1) % Math.max(1, drivers.length - 2)),
      ROTATE_MS,
    )
    return () => window.clearInterval(timer)
  }, [drivers.length])

  const cards = useMemo(() => {
    if (drivers.length <= 3) return drivers.slice(0, 3)
    const window3 = drivers.slice(offset, offset + 3)
    if (window3.length < 3) {
      return [...window3, ...drivers.slice(0, 3 - window3.length)]
    }
    return window3
  }, [drivers, offset])

  return (
    <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white px-5 py-4 shadow-xl">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 font-mono">
        <div className="flex items-baseline gap-2">
          <span className="text-3xl font-black italic text-white light:text-zinc-900 leading-none">
            {lap ?? '—'}
          </span>
          <span className="text-sm font-bold text-zinc-500">
            {totalLaps ? `/${totalLaps} LAPS` : 'LAPS'}
          </span>
        </div>

        <div
          className="text-lg font-black text-zinc-200 light:text-zinc-800 tabular-nums"
          title="Race clock"
        >
          {formatClock(elapsed)}
        </div>

        <div className="flex items-center gap-2.5">
          {cards.map((driver) => {
            const leader = drivers[driver.position ? driver.position - 2 : 0]
            const gapAhead =
              driver.interval !== null && driver.interval !== undefined
                ? driver.interval
                : driver.gap_to_leader !== null && driver.gap_to_leader !== undefined
                  ? driver.gap_to_leader - (leader?.gap_to_leader ?? 0)
                  : null
            return (
              <button
                key={driver.driver_number}
                onClick={() => onSelect?.(driver.driver_number)}
                title={`${driver.name} — gap to the car in front`}
                className={`flex items-center gap-2 px-2.5 py-1 rounded-lg border transition-colors ${
                  focusCode === driver.code
                    ? 'border-red-600 bg-red-950/50'
                    : 'border-zinc-800 light:border-zinc-200 bg-zinc-900/60 light:bg-zinc-100 hover:border-zinc-600'
                }`}
              >
                <span className="text-zinc-500 font-black text-xs">
                  P{driver.position ?? '—'}
                </span>
                <span
                  className="w-2 h-4 rounded-sm"
                  style={{ backgroundColor: teamColour(driver.team_colour) }}
                  aria-hidden
                />
                <span className="font-black italic text-white light:text-zinc-900 text-sm">
                  {driver.code}
                </span>
                <span className="text-zinc-400 light:text-zinc-600 text-[11px] font-semibold hidden xl:inline">
                  {driver.name}
                </span>
                <span className="text-zinc-300 light:text-zinc-700 text-xs font-bold tabular-nums">
                  {driver.position === 1
                    ? 'LEADER'
                    : gapAhead === null || gapAhead < 0
                      ? '—'
                      : `+${gapAhead.toFixed(3)}`}
                </span>
              </button>
            )
          })}
        </div>

        <span
          className={`ml-auto px-3 py-1 rounded-lg border font-black text-xs tracking-widest animate-breathe ${
            flagState === 'GREEN'
              ? 'text-emerald-400 border-emerald-600/50 bg-emerald-500/10'
              : flagState === 'YELLOW' || flagState === 'DOUBLE YELLOW'
                ? 'text-amber-300 border-amber-500/60 bg-amber-500/10'
                : 'text-red-300 border-red-600/60 bg-red-500/10'
          }`}
        >
          {flagState}
        </span>
      </div>
    </div>
  )
}
