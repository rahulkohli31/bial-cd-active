import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// Streamdown's caret/animation keyframes (local, no CDN). Currently inert — MessageContent
// doesn't enable `caret`/`animated` — kept imported so it's already wired for whenever one is.
import 'streamdown/styles.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
