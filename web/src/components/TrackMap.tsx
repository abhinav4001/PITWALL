import React, { useMemo } from 'react'
import { F1CarBody } from './F1Car'
import { teamColour, type CircuitOutline, type DriverPosition } from '../lib/api'

interface TrackMapProps {
  outline: CircuitOutline
  drivers: DriverPosition[]
  selectedDriver?: number | null
  onSelectDriver?: (driverNumber: number) => void
  showCorners?: boolean
  showLabels?: boolean
  className?: string
  /** When set, a Formula 1 car is driven around the circuit outline. */
  carPath?: string
  /** How long one lap of the car animation takes, in seconds. */
  carLapSeconds?: number
  carColour?: string
}

/**
 * The circuit map.
 *
 * The outline points and the driver dots arrive in the *same* projected
 * coordinate space (the API applies one shared projection to both), so a dot
 * is guaranteed to sit on the racing line.  Nothing here is positioned by
 * percentage or array index.
 */
export function TrackMap({
  outline,
  drivers,
  selectedDriver = null,
  onSelectDriver,
  showCorners = true,
  showLabels = true,
  className = '',
  carPath,
  carLapSeconds = 16,
  carColour = '#e10600',
}: TrackMapProps) {
  const { width, height } = outline.view_box

  const path = useMemo(() => {
    if (!outline.points.length) return ''
    return outline.points
      .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`)
      .join(' ')
  }, [outline.points])

  const onTrack = drivers.filter((d) => d.view_x !== null && d.view_y !== null)

  return (
    <div className={`relative w-full h-full overflow-hidden ${className}`}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="absolute inset-0 w-full h-full"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={`${outline.circuit} circuit map`}
      >
        <defs>
          <filter id="trackGlow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="5" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          {/* animateMotion follows an element by id, not by path data. */}
          {carPath && <path id="pitwall-lap-path" d={carPath} />}
        </defs>

        {/* Track surface */}
        <path
          d={path}
          fill="none"
          stroke="#18181b"
          strokeWidth={26}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d={path}
          fill="none"
          stroke="#3f3f46"
          strokeWidth={16}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d={path}
          fill="none"
          stroke="#e10600"
          strokeWidth={3}
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity={0.85}
        />

        {/* Start / finish line */}
        {outline.start_finish && (
          <g>
            <rect
              x={outline.start_finish.x - 13}
              y={outline.start_finish.y - 13}
              width={26}
              height={26}
              fill="#22c55e"
              opacity={0.9}
              rx={2}
            />
            <text
              x={outline.start_finish.x}
              y={outline.start_finish.y + 5}
              textAnchor="middle"
              fontSize="13"
              fontWeight="800"
              fill="#052e16"
            >
              S/F
            </text>
          </g>
        )}

        {/* Corner numbers */}
        {showCorners &&
          outline.corners.map((corner) => (
            <text
              key={`${corner.number}${corner.letter}`}
              x={corner.x}
              y={corner.y}
              textAnchor="middle"
              fontSize="15"
              fontWeight="700"
              fill="#52525b"
              pointerEvents="none"
            >
              {corner.number}
            </text>
          ))}

        {/* A Formula 1 car driving the circuit. */}
        {carPath && (
          <g>
            <animateMotion
              dur={`${carLapSeconds}s`}
              repeatCount="indefinite"
              rotate="auto"
              calcMode="linear"
            >
              <mpath href="#pitwall-lap-path" />
            </animateMotion>
            <g transform="scale(0.85)">
              <F1CarBody colour={carColour} />
            </g>
          </g>
        )}

        {/* Driver markers */}
        {onTrack.map((driver) => {
          const isSelected = driver.driver_number === selectedDriver
          const radius = isSelected ? 13 : 9
          const colour = teamColour(driver.team_colour)
          return (
            <g
              key={driver.driver_number}
              transform={`translate(${driver.view_x} ${driver.view_y})`}
              onClick={() => onSelectDriver?.(driver.driver_number)}
              style={{ cursor: onSelectDriver ? 'pointer' : 'default' }}
            >
              {isSelected && (
                <circle
                  r={radius + 7}
                  fill="none"
                  stroke={colour}
                  strokeWidth={2}
                  opacity={0.85}
                >
                  <animate
                    attributeName="r"
                    values={`${radius + 4};${radius + 12};${radius + 4}`}
                    dur="1.8s"
                    repeatCount="indefinite"
                  />
                </circle>
              )}
              <circle
                r={radius}
                fill={colour}
                stroke="#fafafa"
                strokeWidth={isSelected ? 3 : 1.5}
                filter={isSelected ? 'url(#trackGlow)' : undefined}
              />
              {showLabels && (
                <text
                  y={-radius - 6}
                  textAnchor="middle"
                  fontSize="12"
                  fontWeight="800"
                  fill="#fafafa"
                  stroke="#09090b"
                  strokeWidth={3}
                  paintOrder="stroke"
                >
                  {driver.code}
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
