import React from 'react'
import ReactDOM from 'react-dom/client'
import PitWallApp from './App'
import './index.css'

const rootElement = document.getElementById('root')
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <PitWallApp />
    </React.StrictMode>,
  )
}
