import React, { useState } from 'react'

interface TeamLogoProps {
  teamKey: string
  className?: string
}

export function TeamLogo({ teamKey, className = "w-10 h-7" }: TeamLogoProps) {
  const [imgError, setImgError] = useState(false)

  const getFileName = (key: string) => {
    switch (key?.toUpperCase()) {
      case 'MERCEDES': return 'mercedes.jpg'
      case 'FERRARI': return 'ferrari.png'
      case 'REDBULL': return 'redbull.png'
      case 'MCLAREN': return 'mclaren.png'
      case 'WILLIAMS': return 'williams.png'
      case 'ASTON': return 'aston.jpg'
      case 'ALPINE': return 'alpine.png'
      case 'VCARB': return 'vcarb.png'
      case 'CADILLAC': return 'cadillac.png'
      case 'HAAS': return 'haas.jpg'
      case 'AUDI': return 'audi.png'
      default: return 'redbull.png'
    }
  }

  const filename = getFileName(teamKey)

  if (imgError) {
    return (
      <div className={`flex items-center justify-center rounded-lg bg-zinc-800 text-zinc-200 light:text-zinc-800 font-black text-xs uppercase tracking-tighter ${className}`}>
        {teamKey?.slice(0, 3)}
      </div>
    )
  }

  return (
    <div className={`flex items-center justify-center overflow-hidden rounded-lg bg-zinc-900 light:bg-zinc-100 border border-zinc-700 light:border-zinc-300/50 p-1 shadow-md ${className}`}>
      <img
        src={`/teams/${filename}`}
        alt={teamKey}
        onError={() => setImgError(true)}
        className="w-full h-full object-contain filter drop-shadow"
      />
    </div>
  )
}
