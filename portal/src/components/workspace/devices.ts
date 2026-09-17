/**
 * THE TWO WIDTHS THE PANE CAN FRAME AT, and their one home. Read by `WorkspaceToolbar`
 * (the switcher) and the device card the pane draws, so both ends read one table rather than
 * two that could disagree about a width.
 *
 * DESKTOP AND A PHONE, because those are the two a citizen is actually building for and the
 * toolbar row has no room to spare. A third width in between was a preset nobody asked to
 * check against, paid for out of a row that does not fit nine occupants at 360px.
 *
 * ITS OWN LEAF MODULE: importing this table from the component that owns the switcher would
 * close a five-module ring (`LivePreview` → `WorkspaceToolbar` → `WorkspaceShell` → `AppPane`
 * → `AppPaneHost` → `LivePreview`). Module evaluation order is not something Vitest and the
 * production Rollup build are obligated to agree about, and the next top-level `const` added
 * anywhere in that ring turns it into a "cannot access before initialization" at boot. A leaf
 * nothing imports back cannot.
 */
import { Monitor, Smartphone, type LucideIcon } from 'lucide-react'

export const DEVICES = {
  Desktop: { icon: Monitor as LucideIcon, width: null as number | null },
  Mobile: { icon: Smartphone as LucideIcon, width: 390 }, // iPhone 12/13/14-class width
}

export type DeviceName = keyof typeof DEVICES
