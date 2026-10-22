import React, { useLayoutEffect, useRef, useState } from 'react'
import { formatLapTime, teamColour, type DriverPosition } from '../lib/api'

interface TimingTowerProps {
  drivers: DriverPosition[]
  selectedDriver: number | null
  onSelect: (driverNumber: number) => void
}

/**
 * The timing tower.
 *
 * Two things matter here:
 *
 * * the **gap to the car in front**, which is what a driver is actually
 *   chasing, not the gap to the leader;
 * * the rows **glide** to their new position when the order changes, so a
 *   position swap reads as movement rather than a jump.
 *
 * The glide is a FLIP animation: the row's position is measured after the
 * order changes, the difference is applied instantly as an offset, and the
 * offset is then released so the row eases into its new place.
 */
export function TimingTower({ drivers, selectedDriver, onSelect }: TimingTowerProps) {
  const rowRefs = useRef(new Map<number, HTMLDivElement>())
  const lastPositions = useRef(new Map<number, number>())
  const lastOrder = useRef('')
  const [offsets, setOffsets] = useState<Record<number, number>>({})

  const order = drivers.map((driver) => driver.driver_number).join(',')

  useLayoutEffect(() => {
    const current = new Map<number, number>()
    rowRefs.current.forEach((element, driverNumber) => {
      current.set(driverNumber, element.getBoundingClientRect().top)
    })

    const orderChanged = lastOrder.current !== order
    lastOrder.current = order

    if (!orderChanged) {
      lastPositions.current = current
      return
    }

    const next: Record<number, number> = {}
    let moved = false
    current.forEach((top, driverNumber) => {
      const before = lastPositions.current.get(driverNumber)
      if (before === undefined) return
      const delta = before - top
      if (Math.abs(delta) > 0.5) {
        next[driverNumber] = delta
        moved = true
      }
    })
    lastPositions.current = current

    if (!moved) return
    setOffsets(next)
    const timer = window.setTimeout(() => setOffsets({}), 700)
    return () => window.clearTimeout(timer)
  }, [order])

  if (!drivers.length) {
    return (
      <div className="text-zinc-500 text-center py-6 font-mono text-xs">
        No timing data.
      </div>
    )
  }

  return (
    <div className="space-y-2 font-mono text-sm">
      {drivers.map((driver) => {
        const offset = offsets[driver.driver_number] ?? 0
        const isSelected = driver.driver_number === selectedDriver
        const leader = drivers[driver.position ? driver.position - 2 : 0]
        const gapAhead =
          driver.interval !== null && driver.interval !== undefined
            ? driver.interval
            : driver.gap_to_leader !== null && driver.gap_to_leader !== undefined
              ? driver.gap_to_leader - (leader?.gap_to_leader ?? 0)
              : null
        return (
          <div
            key={driver.driver_number}
            ref={(element) => {
              if (element) rowRefs.current.set(driver.driver_number, element)
              else rowRefs.current.delete(driver.driver_number)
            }}
            onClick={() => onSelect(driver.driver_number)}
            style={{
              transform: offset ? `translateY(${offset}px)` : undefined,
              transition: offset
                ? 'transform 650ms cubic-bezier(0.22, 1, 0.36, 1)'
                : undefined,
            }}
            className={`flex items-center justify-between p-3 rounded-xl border-2 cursor-pointer transition-colors ${
              isSelected
                ? 'bg-red-950/40 border-red-600'
                : driver.reporting === false
                  ? 'bg-zinc-900/40 light:bg-zinc-50 border-zinc-800/60 light:border-zinc-200 opacity-60'
                  : 'bg-zinc-900/80 light:bg-zinc-100 border-zinc-800 light:border-zinc-200 hover:border-zinc-700'
            }`}
          >
            <div className="flex items-center space-x-3">
              <span className="font-black italic text-zinc-400 w-8">
                P{driver.position ?? '—'}
              </span>
              <span
                className="w-2 h-6 rounded"
                style={{ backgroundColor: teamColour(driver.team_colour) }}
                aria-hidden
              />
              <span className="font-black italic text-white light:text-zinc-900 px-2.5 py-1 rounded bg-zinc-800 light:bg-zinc-200">
                {driver.code}
              </span>
              <span className="text-zinc-200 light:text-zinc-800 font-semibold hidden sm:inline">
                {driver.name}
              </span>
              {driver.reporting === false && (
                <span className="text-[10px] font-black uppercase text-zinc-500 tracking-widest">
                  out
                </span>
              )}
            </div>

            <div className="flex items-center space-x-5">
              {/* The gap to the car directly in front. */}
              <span
                className="font-bold tabular-nums"
                style={{
                  color: driver.position === 1 ? '#a1a1aa' : '#e4e4e7',
                }}
                title={
                  driver.position === 1
                    ? 'Leading the race'
                    : `Gap to the car in front · ${(driver.gap_to_leader ?? 0).toFixed(3)}s to the leader`
                }
              >
                {driver.position === 1
                  ? 'LEADER'
                  : gapAhead === null
                    ? '—'
                    : `+${gapAhead.toFixed(3)}`}
              </span>

              <span
                className={`px-2.5 py-1 rounded-lg text-xs font-black ${
                  driver.compound === 'HARD'
                    ? 'bg-zinc-800 text-zinc-200 light:bg-zinc-200 light:text-zinc-700'
                    : driver.compound === 'MEDIUM'
                      ? 'bg-yellow-950 text-yellow-400 border border-yellow-700'
                      : driver.compound === 'SOFT'
                        ? 'bg-red-950 text-red-400 border border-red-700'
                        : 'bg-zinc-900 text-zinc-500'
                }`}
              >
                {driver.compound ? driver.compound.slice(0, 1) : '?'} (
                {driver.tyre_age ?? '—'} L)
              </span>

              {driver.last_lap_s && (
                <span className="text-zinc-500 font-bold text-xs">
                  {formatLapTime(driver.last_lap_s)}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
