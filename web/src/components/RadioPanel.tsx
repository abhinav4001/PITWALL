import React, { useState } from 'react'
import type { RadioReport } from '../lib/api'

interface RadioPanelProps {
  radio: RadioReport | null
  driverCodes?: Record<number, string>
}

/**
 * Team radio.
 *
 * The calls come from F1's own feed.  Each one is an MP3 on the official
 * static archive, so the audio plays in the browser, and the transcript is
 * produced on the server with an offline speech recogniser - nothing is
 * sent anywhere.
 *
 * F1 does not release radio for every session, so an empty panel says so
 * plainly rather than looking broken.
 */
export function RadioPanel({ radio, driverCodes }: RadioPanelProps) {
  const [playing, setPlaying] = useState<number | null>(null)
  const messages = radio?.messages ?? []
  const available = Boolean(radio?.available) && messages.length > 0
  const pending = radio?.pending ?? 0

  return (
    <div className="rounded-2xl border border-zinc-800 light:border-zinc-200 bg-[#09090b] light:bg-white p-5 shadow-xl">
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-xs font-black uppercase tracking-widest text-zinc-400 light:text-zinc-600">
          Team Radio
        </span>
        {available && pending > 0 && (
          <span className="font-mono text-[10px] font-black uppercase text-amber-300/80">
            {pending} still coming through
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
          <ul className="space-y-2 max-h-72 overflow-y-auto pr-1">
            {[...messages].reverse().map((message, index) => {
              const code =
                message.driver_number !== null
                  ? driverCodes?.[message.driver_number]
                  : null
              return (
                <li
                  key={`${message.date}-${index}`}
                  className={`rounded-lg border px-3 py-2 ${
                    index === 0
                      ? 'border-red-900/60 light:border-red-300 bg-red-950/20 light:bg-red-50'
                      : 'border-zinc-800 light:border-zinc-200 bg-zinc-900/70 light:bg-zinc-100'
                  }`}
                >
                  <div className="flex items-center gap-2 font-mono text-[10px] font-black uppercase text-zinc-500">
                    {code && (
                      <span className="text-zinc-200 light:text-zinc-800 text-xs">
                        {code}
                      </span>
                    )}
                    {message.team && <span className="truncate">{message.team}</span>}
                    {message.recording_url && (
                      <button
                        onClick={() => setPlaying(playing === index ? null : index)}
                        className="ml-auto px-1.5 py-0.5 rounded border border-zinc-700 light:border-zinc-300 text-[9px] hover:border-zinc-500"
                        title={playing === index ? 'Stop' : 'Play the call'}
                      >
                        {playing === index ? '■' : '▶'}
                      </button>
                    )}
                  </div>
                  {message.text ? (
                    <p className="font-mono text-[12px] text-zinc-200 light:text-zinc-800 mt-1 leading-snug">
                      {message.text}
                    </p>
                  ) : (
                    <p className="font-mono text-[11px] text-zinc-500 mt-1 italic">
                      transcribing…
                    </p>
                  )}
                  {playing === index && message.recording_url && (
                    <audio
                      src={message.recording_url}
                      controls
                      autoPlay
                      className="w-full mt-2 h-8"
                    />
                  )}
                </li>
              )
            })}
          </ul>
        </>
      )}
    </div>
  )
}
