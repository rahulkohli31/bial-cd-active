import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// Streamdown's caret/animation keyframes (local, no CDN) — required once, globally.
import 'streamdown/styles.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
