import React from 'react'
import type { StrategyOption } from '../lib/api'

export interface StrategyReport {
  available: boolean
  source?: string
  recommendation?: string
  recommendation_reason?: string
  optimal_pit_lap?: number | null
  optimal_window?: number[] | null
  options?: StrategyOption[]
  compound_tradeoff?: {
    offset_s_per_lap: Record<string, number>
    agreement: number
    samples: number
    reliable: boolean
    note: string
  }
  modelled_compounds?: string[]
  reason?: string
}

interface StrategyCardProps {
  report: StrategyReport | null
}

const CALL_STYLE: Record<string, { className: string; headline: string }> = {
  PIT: {
    className: 'text-red-300 border-red-600/70 bg-red-600/15',
    headline: 'BOX NOW',
  },
  WAIT: {
    className: 'text-amber-300 border-amber-600/70 bg-amber-500/10',
    headline: 'WAIT',
  },
  HOLD: {
    className: 'text-emerald-300 border-emerald-600/70 bg-emerald-500/10',
    headline: 'HOLD',
  },
}

/**
 * The strategy card.
 *
 * The question a fan actually asks is not "what is the expected race time"
 * but **"if I stop now, do I come out in front of that car or behind it?"**
 * So the card leads with the call and then with where the driver would end
 * up, and who would be either side of them.
 *
 * The numbers behind it are the real ones: the strategy is projected from
 * the fitted degradation, the measured pace and the real gaps.
 */
export function StrategyCard({ report }: StrategyCardProps) {
  if (!report || !report.available) {
    return (
      <div className="rounded-2xl border border-amber-700/40 bg-amber-950/20 p-5">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-amber-400">
          Strategy unavailable
        </span>
        <p className="font-mono text-[11px] text-amber-300/70 mt-1">
          {report?.reason ?? 'The engine has no calibration for this circuit yet.'}
        </p>
      </div>
    )
  }

  const options = report.options ?? []
  const best = options[0]
  const call = CALL_STYLE[report.recommendation ?? 'HOLD'] ?? CALL_STYLE.HOLD
  const window = report.optimal_window
  const pitLaps = options.filter((option) => option.pit_lap !== null)

  return (
    <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-5 shadow-xl space-y-4">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-zinc-400 light:text-zinc-600">
          Strategy
        </span>
        <span
          className={`px-3 py-1 rounded-lg border font-mono text-xs font-black tracking-widest ${call.className}`}
        >
          {call.headline}
        </span>
      </div>

      {/* The call, in words a fan can read. */}
      <p className="font-mono text-sm font-bold text-white light:text-zinc-900 leading-snug">
        {best ? best.label : 'No strategy could be projected.'}
      </p>

      {window && (
        <p className="font-mono text-xs font-bold text-amber-300">
          Best window: lap {window[0]}
          {window[1] !== window[0] ? `–${window[1]}` : ''}
        </p>
      )}

      {/* Where this leaves you on the road. */}
      {best && best.position_after !== null && best.position_after !== undefined && (
        <Outcome option={best} />
      )}

      {best && best.p_beat_rival !== null && best.p_beat_rival !== undefined && (
        <div className="flex justify-between font-mono text-[11px] font-bold text-zinc-400 light:text-zinc-600">
          <span>Chance of passing the car ahead</span>
          <span className="text-zinc-100 light:text-zinc-900">
            {(best.p_beat_rival * 100).toFixed(0)}%
          </span>
        </div>
      )}

      {pitLaps.length > 0 && (
        <div className="space-y-1.5 pt-2 border-t border-zinc-800 light:border-zinc-200">
          <span className="font-mono text-[10px] font-black uppercase tracking-widest text-zinc-500 light:text-zinc-500">
            Other options
          </span>
          {pitLaps.slice(0, 3).map((option) => (
            <div
              key={option.strategy_id}
              className="flex items-center justify-between gap-2 font-mono text-[11px]"
            >
              <span className="text-zinc-400 light:text-zinc-600 truncate">
                {option.label}
              </span>
              <span className="text-zinc-300 light:text-zinc-700 shrink-0">
                {option.position_after !== null &&
                option.position_after !== undefined ? (
                  <>P{option.position_after}</>
                ) : null}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * The outcome: where the driver ends up, and who is either side of them.
 */
function Outcome({ option }: { option: StrategyOption }) {
  const parts: string[] = []
  if (option.behind_of) {
    parts.push(`ahead of ${option.behind_of}`)
  }
  if (option.ahead_of) {
    parts.push(`behind ${option.ahead_of}`)
  }

  return (
    <div className="rounded-xl border border-zinc-800 light:border-zinc-200 bg-zinc-900/70 light:bg-zinc-100 px-4 py-3 space-y-1">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-3xl font-black text-white light:text-zinc-900 leading-none">
          P{option.position_after}
        </span>
        <span className="font-mono text-[11px] font-bold text-zinc-400 light:text-zinc-600 uppercase tracking-wider">
          where you end up
        </span>
      </div>
      {parts.length > 0 && (
        <p className="font-mono text-[11px] font-bold text-zinc-300 light:text-zinc-700 leading-snug">
          {parts.join(', ')}
        </p>
      )}
    </div>
  )
}
