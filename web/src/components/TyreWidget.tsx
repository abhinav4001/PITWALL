import React from 'react'

export interface TyreState {
  compound: string | null
  age_laps: number | null
  stint_number: number | null
  laps_remaining: number | null
  degradation_s: number | null
  projected_cliff_lap: number | null
  cliff_laps_away: number | null
}

interface TyreWidgetProps {
  tyre: TyreState | null
  available: boolean
  reason?: string
}

const COMPOUND_STYLE: Record<string, string> = {
  SOFT: 'bg-red-950 text-red-300 border-red-700/70',
  MEDIUM: 'bg-yellow-950 text-yellow-300 border-yellow-700/70',
  HARD: 'bg-zinc-800 text-zinc-200 light:text-zinc-800 border-zinc-600',
  INTERMEDIATE: 'bg-green-950 text-green-300 border-green-700/70',
  WET: 'bg-blue-950 text-blue-300 border-blue-700/70',
}

/**
 * The tyre card: compound, age, how much has been lost and when the cliff
 * arrives.
 *
 * The degradation bar is the *measured* loss so far, against the modelled
 * loss at the end of the stint, so the driver can see how much is left.
 */
export function TyreWidget({ tyre, available, reason }: TyreWidgetProps) {
  if (!available || !tyre) {
    return (
      <div className="rounded-2xl border border-amber-700/40 bg-amber-950/20 p-5">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-amber-400">
          Tyre data unavailable
        </span>
        {reason && (
          <p className="font-mono text-[11px] text-amber-300/70 mt-1">{reason}</p>
        )}
      </div>
    )
  }

  const compound = (tyre.compound ?? '?').toUpperCase()
  const age = tyre.age_laps ?? 0
  const lost = tyre.degradation_s ?? 0
  // A medium tyre is expected to hold its performance for roughly 30 laps;
  // the bar is the share of that window already used.
  const window = Math.max(1, tyre.laps_remaining ?? 0)
  const used = Math.min(1, age / Math.max(1, age + window))
  const cliffSoon =
    tyre.cliff_laps_away !== null && tyre.cliff_laps_away <= 3

  return (
    <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-5 shadow-xl space-y-4">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-zinc-400 light:text-zinc-600">
          Tyre
        </span>
        <div className="flex items-center gap-2">
          <span
            className={`px-2.5 py-1 rounded-lg border font-mono text-xs font-black ${
              COMPOUND_STYLE[compound] ?? 'bg-zinc-900 light:bg-zinc-100 text-zinc-300 light:text-zinc-700 border-zinc-700 light:border-zinc-300'
            }`}
          >
            {compound}
          </span>
          <span className="font-mono text-xs font-bold text-zinc-400 light:text-zinc-600">
            age {age} L
          </span>
        </div>
      </div>

      <div>
        <div className="flex justify-between font-mono text-[11px] font-bold text-zinc-500 light:text-zinc-500 mb-1.5">
          <span>Degradation</span>
          <span className="text-zinc-300 light:text-zinc-700">+{lost.toFixed(2)}s</span>
        </div>
        <div className="relative h-3 rounded-full bg-zinc-800 overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-emerald-500 via-amber-500 to-red-500"
            style={{ width: `${Math.max(2, used * 100)}%` }}
          />
          {tyre.projected_cliff_lap !== null && (
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-white"
              style={{ left: `${Math.min(100, used * 100)}%` }}
              title={`Cliff projected on lap ${tyre.projected_cliff_lap}`}
            />
          )}
        </div>
      </div>

      <div className="flex justify-between font-mono text-[11px] font-bold">
        <span className="text-zinc-500 light:text-zinc-500">
          {tyre.laps_remaining ?? '—'} laps of stint left
        </span>
        {tyre.projected_cliff_lap !== null ? (
          <span className={cliffSoon ? 'text-red-400' : 'text-zinc-400 light:text-zinc-600'}>
            cliff proj. lap {tyre.projected_cliff_lap}
            {cliffSoon && ' ⚠'}
          </span>
        ) : (
          <span className="text-zinc-500 light:text-zinc-500">no cliff in the fitted range</span>
        )}
      </div>
    </div>
  )
}
