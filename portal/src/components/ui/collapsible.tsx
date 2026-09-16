import * as CollapsiblePrimitive from "@radix-ui/react-collapsible"

/**
 * shadcn/ui `collapsible`, the registry block unchanged — three re-exports and not one class,
 * so unlike `switch.tsx` or `popover.tsx` there is nothing here to re-check against a real
 * Tailwind 3 build.
 *
 * THE HEIGHT IS NOT ANIMATED HERE. Radix publishes `--radix-collapsible-content-height` for a CSS
 * keyframe; this portal animates with `motion` from `lib/motion.ts`'s shared durations instead,
 * which is also what makes the root `MotionConfig` the reduced-motion answer. A consumer that
 * wants the CLOSING height to be seen must mount the content under `AnimatePresence` and pass
 * `forceMount`, because Radix otherwise unmounts the content before an exit can run.
 */
const Collapsible = CollapsiblePrimitive.Root
const CollapsibleTrigger = CollapsiblePrimitive.CollapsibleTrigger
const CollapsibleContent = CollapsiblePrimitive.CollapsibleContent

export { Collapsible, CollapsibleTrigger, CollapsibleContent }
