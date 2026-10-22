import React from 'react'

interface PedalProps {
  /** 0-100 as reported by the telemetry feed (values above 100 are clamped). */
  throttle?: number | null
  brake?: number | null
  compact?: boolean
}

/**
 * Throttle and brake bars.
 *
 * The throttle reads green and the brake red, the way a driver or an
 * engineer would describe it: green means going, red means stopping.
 */
export function PedalIndicator({ throttle, brake, compact = false }: PedalProps) {
  const throttleValue = Math.max(0, Math.min(100, throttle ?? 0))
  const brakeValue = Math.max(0, Math.min(100, brake ?? 0))

  const bar = (label: string, value: number, colour: string, track: string) => (
    <div className="flex items-center gap-2.5">
      <span className="font-mono text-[10px] font-black uppercase tracking-widest w-11 text-zinc-500 light:text-zinc-500">
        {label}
      </span>
      <div className={`flex-1 ${compact ? 'h-2' : 'h-3'} rounded-full bg-zinc-800 overflow-hidden`}>
        <div
          className={`h-full rounded-full transition-[width] duration-100 ${colour}`}
          style={{ width: `${value}%` }}
        />
      </div>
      <span
        className={`font-mono ${compact ? 'text-[10px]' : 'text-xs'} font-black tabular-nums w-10 text-right`}
        style={{ color: value > 0 ? colour.split(' ')[0] : '#52525b' }}
      >
        {Math.round(value)}%
      </span>
    </div>
  )

  return (
    <div className="space-y-2">
      {bar('Throttle', throttleValue, 'bg-emerald-500', '')}
      {bar('Brake', brakeValue, 'bg-red-500', '')}
    </div>
  )
}
