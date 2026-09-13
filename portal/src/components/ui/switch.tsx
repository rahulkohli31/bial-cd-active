import * as React from "react"
import * as SwitchPrimitive from "@radix-ui/react-switch"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `switch`, hand-authored for THIS build rather than pasted from the registry — the
 * same treatment `dropdown-menu.tsx` and `popover.tsx` record, and for the same three reasons.
 *
 * 1. THE REGISTRY BLOCK IS TAILWIND v4 AND THIS PORTAL IS 3.4.17. Its track carries `shadow-xs`,
 *    a utility Tailwind 3 does not produce: it renders as NOTHING, with no build error, no
 *    console warning and no failing test — jsdom computes no styles. Every class below was
 *    checked against a real `npx tailwindcss` build of `tailwind.config.js`.
 *
 * 2. TWO SHADCN TOKENS RESOLVE WRONG HERE. The registry paints the thumb `bg-background` and the
 *    off-track `bg-input`; in this config `--background` is the app's pale blue-grey ground
 *    (#F0F4F8, `bial-bg`) and `--input` is the hairline (#E2E8F0). The boards draw a WHITE thumb
 *    on a #D6DDE4 track, so both are named literally — `bg-white` and `canvas-sendoff`, the
 *    palette entry that already holds that hex for the send control with nothing to send.
 *
 * 3. THE GEOMETRY IS THE BOARDS', NOT THE REGISTRY'S. `DialogProjects` and `Main` both draw a
 *    32×18 track with 2px of padding and a 14px thumb, which is smaller than the registry's
 *    32×18.4 with a 16px thumb. 14px of travel is that geometry, written out rather than left to
 *    the registry's `translate-x-[calc(100%-2px)]`, so the number is checkable against the board.
 *
 * `peer` EMITS NO CSS AND THAT IS NOT A MISS. It is Tailwind's marker class — it exists only so a
 * SIBLING can style itself with `peer-checked:`/`peer-focus:`, and nothing does that today. It is
 * the one class in this file absent from a real `tailwindcss` build, and it is kept because it is
 * the registry's shape and because removing it would silently break the first sibling label that
 * reaches for it. Every OTHER class here was verified present in that build.
 *
 * WHY IT IS NEVER NATIVELY `disabled`. Callers lock this control mid-write with `aria-disabled`
 * and an ignored change handler, per `dialog.tsx`'s focus-backstop docblock: disabling a FOCUSED
 * control throws focus to `<body>`, which is the exact strand that backstop exists to catch. The
 * `disabled:` classes below are still here because Radix supports the prop and a future caller
 * outside a dialog may legitimately use it; `aria-disabled:` carries the lock this product uses.
 */
const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex h-[18px] w-8 shrink-0 cursor-pointer items-center rounded-full border border-transparent p-0.5 transition-colors",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-white",
      "disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-progress aria-disabled:opacity-60",
      "data-[state=checked]:bg-primary data-[state=unchecked]:bg-canvas-sendoff",
      className
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={cn(
        "pointer-events-none block h-3.5 w-3.5 rounded-full bg-white shadow-sm ring-0 transition-transform",
        "data-[state=checked]:translate-x-[14px] data-[state=unchecked]:translate-x-0"
      )}
    />
  </SwitchPrimitive.Root>
))
Switch.displayName = SwitchPrimitive.Root.displayName

export { Switch }
