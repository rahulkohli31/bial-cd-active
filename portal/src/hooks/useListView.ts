import { useState } from 'react'
import {
  readStoredDensity,
  readStoredView,
  storeDensity,
  storeView,
  type Density,
  type View,
} from '../utils/listView'

/**
 * How this citizen likes their application lists drawn, as state that writes itself through.
 *
 * ONE HOOK FOR EVERY APPLICATION LIST, which is what makes the preference a habit rather than a
 * per-page setting: the owner's list and the shared list read and write the same keys, and a page
 * cannot remember to store a change and then forget to. See `utils/listView.ts` for why the
 * preference lives in `localStorage` rather than in the URL.
 */
export function useListView(): {
  view: View
  setView: (next: View) => void
  density: Density
  setDensity: (next: Density) => void
} {
  const [view, setViewState] = useState<View>(readStoredView)
  const [density, setDensityState] = useState<Density>(readStoredDensity)

  return {
    view,
    setView: (next) => {
      setViewState(next)
      storeView(next)
    },
    density,
    setDensity: (next) => {
      setDensityState(next)
      storeDensity(next)
    },
  }
}
