import React, { useMemo } from 'react'

interface F1CarProps {
  /** SVG path the car should follow, in the same viewBox as the track. */
  path?: string
  /** Duration of one lap of the animation, in seconds. */
  lapSeconds?: number
  colour?: string
  className?: string
  /** Render the car pointing right instead of up, for a side view. */
  side?: boolean
  width?: number
  height?: number
}

/**
 * The car body as a plain group, so it can be dropped inside another SVG
 * (for example a group driven along the circuit path with ``animateMotion``).
 */
export function F1CarBody({ colour = '#e10600' }: { colour?: string }) {
  return (
    <g>
      {/* rear tyres */}
      <rect x="-26" y="6" width="11" height="20" rx="2.5" fill="#111" />
      <rect x="15" y="6" width="11" height="20" rx="2.5" fill="#111" />
      {/* front tyres */}
      <rect x="-25" y="-26" width="10" height="18" rx="2.5" fill="#111" />
      <rect x="15" y="-26" width="10" height="18" rx="2.5" fill="#111" />

      {/* sidepods */}
      <path d="M -13 4 L -15 -12 L -6 -14 L -6 4 Z" fill={colour} opacity="0.9" />
      <path d="M 13 4 L 15 -12 L 6 -14 L 6 4 Z" fill={colour} opacity="0.9" />

      {/* floor / monocoque */}
      <path d="M -6 -16 L 6 -16 L 8 4 L 4 8 L -4 8 L -8 4 Z" fill={colour} />

      {/* engine cover and airbox */}
      <path d="M -4 6 L 4 6 L 3 12 L -3 12 Z" fill={colour} opacity="0.85" />
      <path d="M -3 10 L 3 10 L 2 15 L -2 15 Z" fill={colour} opacity="0.7" />

      {/* nose cone */}
      <path d="M -2.4 -14 L 2.4 -14 L 1.4 -34 L -1.4 -34 Z" fill={colour} />

      {/* front wing */}
      <path d="M -19 -36 L 19 -36 L 17 -32 L -17 -32 Z" fill={colour} />
      <rect x="-19" y="-38" width="38" height="2.5" rx="1" fill={colour} opacity="0.75" />

      {/* rear wing */}
      <path d="M -20 14 L 20 14 L 19 19 L -19 19 Z" fill={colour} />
      <rect x="-21" y="19" width="42" height="2.5" rx="1" fill={colour} opacity="0.75" />

      {/* halo / driver head */}
      <circle cx="0" cy="-6" r="3.4" fill="#151515" />
      <circle cx="0" cy="-6" r="1.8" fill={colour} opacity="0.6" />
    </g>
  )
}

/**
 * A Formula 1 car, drawn as SVG.
 *
 * The car is a plan view: nose cone, front wing, four tyres, sidepods,
 * engine cover, rear wing.  It is drawn here rather than shipped as a bitmap
 * so it stays crisp at any size, can be recoloured per team, and can be
 * driven along the real circuit outline with ``animateMotion``.
 */
export function F1Car({
  path,
  lapSeconds = 14,
  colour = '#e10600',
  className = '',
  side = false,
  width = 64,
  height = 64,
}: F1CarProps) {
  if (side) {
    // A side view: low nose, big rear wing.
    return (
      <svg viewBox="0 0 120 48" className={className} width={width} height={height} aria-hidden>
        <g>
          <path
            d="M 6 30 L 30 30 L 34 16 L 78 12 L 104 16 L 108 30 L 108 36 L 6 36 Z"
            fill={colour}
          />
          <path d="M 34 16 L 46 4 L 62 4 L 62 14 Z" fill={colour} opacity="0.8" />
          <rect x="96" y="4" width="18" height="12" rx="2" fill={colour} />
          <circle cx="26" cy="34" r="11" fill="#111" />
          <circle cx="26" cy="34" r="5" fill="#3f3f46" />
          <circle cx="92" cy="34" r="12" fill="#111" />
          <circle cx="92" cy="34" r="5.5" fill="#3f3f46" />
          <path d="M 2 28 L 22 28 L 22 33 L 2 33 Z" fill={colour} opacity="0.75" />
        </g>
      </svg>
    )
  }

  return (
    <svg
      viewBox="-30 -42 60 66"
      className={className}
      width={width}
      height={height}
      aria-hidden
    >
      {path ? (
        <g>
          <animateMotion dur={`${lapSeconds}s`} repeatCount="indefinite" rotate="auto">
            <mpath href={path} />
          </animateMotion>
          <F1CarBody colour={colour} />
        </g>
      ) : (
        <F1CarBody colour={colour} />
      )}
    </svg>
  )
}
