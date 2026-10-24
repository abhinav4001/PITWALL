import React, { useRef, useState } from 'react'
import type { RadioReport } from '../lib/api'

interface RadioPanelProps {
  radio: RadioReport | null
  driverCodes?: Record<number, string>
}

function clockOf(date: string | null): string {
  if (!date) return ''
  const when = new Date(date)
  if (Number.isNaN(when.getTime())) return ''
  return when.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone: 'UTC',
  })
}

function durationOf(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const whole = Math.floor(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/**
 * Team radio.
 *
 * The calls are the official recordings from F1's own feed, played straight
 * from the static archive.  There is no transcript: the audio is the real
 * thing, and a machine's guess at the words would only get in the way.
 *
 * F1 does not release radio for every session, so an empty panel says so
 * plainly rather than looking broken.
 */
export function RadioPanel({ radio, driverCodes }: RadioPanelProps) {
  const messages = radio?.messages ?? []
  const available = Boolean(radio?.available) && messages.length > 0
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [length, setLength] = useState<number | null>(null)

  const calls = messages
    .map((message, index) => ({
      message,
      key: `${message.date ?? ''}|${message.recording_url ?? ''}|${index}`,
    }))
    .reverse()

  const stop = () => {
    setPlaying(null)
    setElapsed(0)
    setLength(null)
  }

  const toggle = (key: string, url?: string | null) => {
    const element = audio.current
    if (!element || !url) return
    if (playing === key) {
      element.pause()
      stop()
      return
    }
    setFailed(null)
    if (element.src !== url) {
      element.src = url
      element.currentTime = 0
      setElapsed(0)
      setLength(null)
    }
    setPlaying(key)
    void element.play().catch(() => {
      setPlaying(null)
      setFailed(key)
    })
  }

  const progress =
    length && length > 0 ? Math.min(100, (elapsed / length) * 100) : 0

  return (
    <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-5 shadow-xl">
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-zinc-400 light:text-zinc-600">
          Team Radio
        </span>
        {available && (
          <span className="font-mono text-[10px] font-black uppercase text-zinc-500">
            {messages.length} {messages.length === 1 ? 'call' : 'calls'} · UTC
          </span>
        )}
      </div>

      {!available ? (
        <p className="font-mono text-[11px] leading-relaxed text-zinc-500">
          {radio?.detail ??
            'F1 did not publish team radio for this session. Coverage varies by event and fell sharply through 2026, so many sessions carry no radio at all.'}
        </p>
      ) : (
        <>
          <ul className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
            {calls.map(({ message, key }, index) => {
              const code =
                message.driver_number !== null
                  ? driverCodes?.[message.driver_number]
                  : null
              const url = message.recording_url ?? undefined
              const isPlaying = playing === key
              const newest = index === 0
              return (
                <li
                  key={key}
                  className={`flex items-center gap-3 rounded-lg border px-2.5 py-2 ${
                    newest
                      ? 'border-red-900/60 light:border-red-300 bg-red-950/20 light:bg-red-50'
                      : 'border-zinc-800 light:border-zinc-200 bg-zinc-900/70 light:bg-zinc-100'
                  }`}
                >
                  <button
                    onClick={() => toggle(key, url)}
                    disabled={!url}
                    title={
                      !url
                        ? 'F1 did not publish the audio for this call'
                        : isPlaying
                          ? 'Pause'
                          : 'Play the call'
                    }
                    aria-label={isPlaying ? 'Pause radio call' : 'Play radio call'}
                    className={`shrink-0 w-8 h-8 rounded-full border flex items-center justify-center text-[11px] transition-colors ${
                      url
                        ? 'border-zinc-700 light:border-zinc-300 text-zinc-200 light:text-zinc-800 hover:border-red-500 hover:text-red-400'
                        : 'border-zinc-800 light:border-zinc-200 text-zinc-600 cursor-not-allowed'
                    }`}
                  >
                    {isPlaying ? '❚❚' : '▶'}
                  </button>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2 font-mono text-[10px] font-black uppercase text-zinc-500">
                      <span className="text-xs text-zinc-200 light:text-zinc-800">
                        {code ?? (message.driver_number ? `#${message.driver_number}` : '—')}
                      </span>
                      {message.team && (
                        <span className="truncate text-zinc-500">{message.team}</span>
                      )}
                      <span className="ml-auto tabular-nums" title={message.date ?? undefined}>
                        {clockOf(message.date)}
                      </span>
                    </div>

                    {isPlaying ? (
                      <div className="mt-1.5 flex items-center gap-2">
                        <div className="h-1 flex-1 overflow-hidden rounded-full bg-zinc-800 light:bg-zinc-300">
                          <div
                            className="h-full rounded-full bg-red-500 transition-[width] duration-200"
                            style={{ width: `${progress}%` }}
                          />
                        </div>
                        <span className="font-mono text-[10px] tabular-nums text-zinc-500">
                          {durationOf(elapsed)}
                          {length !== null && ` / ${durationOf(length)}`}
                        </span>
                      </div>
                    ) : failed === key ? (
                      <p className="mt-0.5 font-mono text-[10px] text-amber-400/80">
                        audio unavailable
                      </p>
                    ) : !url ? (
                      <p className="mt-0.5 font-mono text-[10px] text-zinc-600">
                        audio not published
                      </p>
                    ) : null}
                  </div>
                </li>
              )
            })}
          </ul>
          <audio
            ref={audio}
            preload="none"
            onTimeUpdate={(event) => setElapsed(event.currentTarget.currentTime)}
            onLoadedMetadata={(event) => {
              const value = event.currentTarget.duration
              setLength(Number.isFinite(value) ? value : null)
            }}
            onEnded={stop}
            onError={() => {
              if (playing) setFailed(playing)
              stop()
            }}
          />
        </>
      )}
    </div>
  )
}
