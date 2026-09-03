/**
 * THE THREE WIDTHS THE PANE CAN FRAME AT, and their one home.
 *
 * They were `LivePreview`'s private map, chosen there because the switcher lived in that
 * component's own toolbar. The switcher is in `WorkspaceToolbar` now and the widths are read by
 * the device card the pane draws, so both ends read one table rather than two that can disagree
 * about what "Tablet" means.
 *
 * ITS OWN LEAF MODULE, for the reason `hiddenSubtree.ts` states about itself: the table's natural
 * home is the component that owns the switcher, and importing it from there closed a five-module
 * ring — `LivePreview` → `WorkspaceToolbar` → `WorkspaceShell` → `AppPane` → `AppPaneHost` →
 * `LivePreview`. Nothing broke, because the map is only ever dereferenced inside a render body and
 * every default export in the ring is a hoisted function declaration; but that safety is a
 * property of module evaluation order, which Vitest and the production Rollup build are under no
 * obligation to agree about, and the next top-level `const` added anywhere in the ring turns it
 * into a "cannot access before initialization" at boot. A leaf nothing imports back cannot.
 */
import { Monitor, Smartphone, Tablet, type LucideIcon } from 'lucide-react'

export const DEVICES = {
  Desktop: { icon: Monitor as LucideIcon, width: null as number | null },
  Tablet: { icon: Tablet as LucideIcon, width: 834 }, // iPad Pro 11" portrait — Chrome DevTools preset
  Mobile: { icon: Smartphone as LucideIcon, width: 390 }, // iPhone 12/13/14-class width
}

export type DeviceName = keyof typeof DEVICES
