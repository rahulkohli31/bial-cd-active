import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// Streamdown's caret/animation keyframes (local, no CDN). Currently inert — MessageContent
// doesn't enable `caret`/`animated` — kept imported so it's already wired for whenever one is.
import 'streamdown/styles.css'
import { MotionConfig } from 'motion/react'
import App from './App'

/**
 * `reducedMotion="user"` is the portal's THIRD reduced-motion mechanism, and it is wired once
 * here because it is the only layer that can reach the library. `index.css`'s media block cannot
 * touch JS-driven animation, and `usePrefersReducedMotion()` swaps an element rather than a
 * motion — so without this, every `motion` component would ignore the preference while the
 * stylesheet's own docblock claimed full coverage. See the guarantee docblock in `index.css`;
 * `src/__tests__/reducedMotion.test.ts` holds all three to what they claim.
 */
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <App />
    </MotionConfig>
  </StrictMode>,
)
