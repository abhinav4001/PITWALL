import React from 'react'

export interface PitWallAlert {
  type: string
  kind?: string
  lap?: number | null
  driver_number: number
  payload: Record<string, unknown>
}

interface AlertBannerProps {
  alerts: PitWallAlert[]
  driverCodes?: Record<number, string>
  /** The flag currently out, which colours the whole strip. */
  flagState?: string
  /** The latest race-control message, shown as the detail line. */
  detail?: string | null
}

/** How each alert type is styled. */
const ALERT_STYLE: Record<string, string> = {
  PIT_WINDOW_OPEN: 'border-amber-500/60 bg-amber-500/10 text-amber-200',
  CLIFF_APPROACHING: 'border-red-600/70 bg-red-600/15 text-red-200',
  UNDERCUT_THREAT: 'border-red-600/70 bg-red-600/15 text-red-200',
  SAFETY_CAR: 'border-red-600/70 bg-red-600/15 text-red-200',
  VSC: 'border-orange-500/70 bg-orange-500/10 text-orange-200',
  RED_FLAG: 'border-red-600 bg-red-700/25 text-red-100',
}

/** How the track state colours the strip when no alert has fired. */
const FLAG_STYLE: Record<string, string> = {
  GREEN: 'border-zinc-800 light:border-zinc-200 bg-zinc-900/50 light:bg-zinc-100/60 text-zinc-400 light:text-zinc-500',
  YELLOW: 'border-amber-500/70 bg-amber-500/10 text-amber-200',
  'DOUBLE YELLOW': 'border-amber-400 bg-amber-400/15 text-amber-100',
  RED: 'border-red-600 bg-red-700/25 text-red-100',
  VSC: 'border-orange-500/70 bg-orange-500/10 text-orange-100',
  'SAFETY CAR': 'border-red-600 bg-red-700/25 text-red-100',
  CHEQUERED: 'border-zinc-400 bg-zinc-700/40 text-zinc-100',
}

const FLAG_LABEL: Record<string, string> = {
  GREEN: 'NO ACTIVE ALERTS',
  YELLOW: 'YELLOW FLAG',
  'DOUBLE YELLOW': 'DOUBLE YELLOW FLAG',
  RED: 'RED FLAG',
  VSC: 'VIRTUAL SAFETY CAR',
  'SAFETY CAR': 'SAFETY CAR',
  CHEQUERED: 'CHEQUERED FLAG',
}

/**
 * The alert strip.
 *
 * One box does both jobs: it is the "no active alerts" panel when the track is
 * clear, and it takes the **colour of the flag** when one is out.  The alert
 * cards themselves appear inside the same strip, so there is only ever one
 * thing to look at above the timing tower.
 */
export function AlertBanner({
  alerts,
  driverCodes,
  flagState = 'GREEN',
  detail,
}: AlertBannerProps) {
  const key = String(flagState).toUpperCase()
  const flagStyle = FLAG_STYLE[key] ?? FLAG_STYLE.GREEN
  const flagLabel = FLAG_LABEL[key] ?? 'NO ACTIVE ALERTS'
  const isAlert = key !== 'GREEN'

  return (
    <div className="space-y-2">
      <div
        className={`rounded-2xl border px-5 py-4 flex items-center gap-4 transition-colors ${
          isAlert ? 'animate-flag-pulse' : ''
        } ${flagStyle}`}
      >
        <svg viewBox="0 0 40 28" className="w-8 h-6 shrink-0" aria-hidden>
          <path d="M 4 2 L 4 26" stroke="#a1a1aa" strokeWidth="2" />
          {key === 'YELLOW' && <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#facc15" />}
          {key === 'DOUBLE YELLOW' && (
            <>
              <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#facc15" />
              <path d="M 6 15 L 34 15 L 34 25 L 6 25 Z" fill="#facc15" />
            </>
          )}
          {key === 'RED' && <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#ef4444" />}
          {key === 'SAFETY CAR' && (
            <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#ef4444" />
          )}
          {key === 'VSC' && <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#fb923c" />}
          {key === 'CHEQUERED' && (
            <>
              {[0, 1, 2, 3].map((i) =>
                [0, 1].map((j) => (
                  <rect
                    key={`${i}-${j}`}
                    x={6 + i * 7}
                    y={3 + j * 5}
                    width="7"
                    height="5"
                    fill={(i + j) % 2 === 0 ? '#f4f4f5' : '#18181b'}
                  />
                )),
              )}
            </>
          )}
          {key === 'GREEN' && <path d="M 6 3 L 34 3 L 34 13 L 6 13 Z" fill="#22c55e" />}
        </svg>

        <div className="min-w-0">
          <div className="font-mono text-xs font-black uppercase tracking-[0.2em]">
            {flagLabel}
          </div>
          {detail && (
            <div className="font-mono text-[11px] opacity-70 truncate">{detail}</div>
          )}
        </div>
      </div>

      {alerts.map((alert, index) => {
        const code = driverCodes?.[alert.driver_number]
        return (
          <div
            key={`${alert.type}-${alert.driver_number}-${index}`}
            className={`rounded-2xl border px-5 py-3 font-mono ${
              ALERT_STYLE[alert.type] ?? 'border-zinc-700 bg-zinc-900 text-zinc-200'
            }`}
          >
            <div className="flex items-center gap-3">
              <span className="font-black text-xs uppercase tracking-widest">
                {alert.type.replace(/_/g, ' ')}
              </span>
              {code && (
                <span className="text-xs font-black italic opacity-80">{code}</span>
              )}
              {alert.lap ? (
                <span className="text-xs font-bold opacity-70">lap {alert.lap}</span>
              ) : null}
              <span className="ml-auto text-[11px] font-bold opacity-80">
                {describe(alert.type, alert.payload)}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function describe(kind: string, payload: Record<string, unknown>): string {
  if (kind === 'PIT_WINDOW_OPEN') {
    const window = payload.optimal_window
    if (Array.isArray(window)) return `optimal stop laps ${window[0]}–${window[1]}`
  }
  if (kind === 'CLIFF_APPROACHING') {
    const cliff = payload.cliff_lap
    if (typeof cliff === 'number') return `projected cliff on lap ${cliff}`
  }
  if (kind === 'UNDERCUT_THREAT') {
    const gap = payload.gap_ahead_s
    if (typeof gap === 'number') return `${gap.toFixed(2)}s behind the car ahead`
  }
  return ''
}
