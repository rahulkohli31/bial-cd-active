/**
 * shadcn/ui `toggle`, copied in from the registry — the primitive `toggle-group` composes.
 *
 * RE-ADDED DELIBERATELY. Both this and `toggle-group.tsx` were removed from this folder earlier as
 * unused, which was correct at the time: nothing imported them. Plan F's rail composer is the
 * importer, so they come back with a caller rather than on spec. Saying so here because "these
 * were deleted once" is the kind of thing that gets them deleted again.
 *
 * THE CLASSES ARE THE TAILWIND-3 REGISTRY GENERATION, matching `button.tsx` and `badge.tsx` beside
 * it. The current registry uses v4-era arbitrary variants, and this portal is on Tailwind 3, where
 * a class the build does not produce renders as NOTHING while every DOM assertion still passes —
 * jsdom computes no Tailwind styles, so no unit test in this repo can catch it. Every token used
 * here (`neutral`, `foreground`, `surface-muted`, `input`, `ring`, `white`, `transparent`,
 * `shadow-segment`) is already defined in `tailwind.config.js` + `src/index.css`, which is what
 * `src/__tests__/tailwind-tokens.test.js` guards.
 *
 * THE SELECTED SEGMENT CARRIES NO HUE, WHICH IS THE POINT. The registry's `data-[state=on]:bg-accent`
 * resolves to the brand orange #F5A623 in this build, and the canvas draws the Plan/Build control
 * with a white pill and a 1px shadow on an #F0F4F8 track — elevation, not colour. Painting the ON
 * segment orange also put a gold icon on an orange ground at roughly 1.2:1. `shadow-segment` is the
 * board's own `0 1px 2px rgba(16,24,40,.08)`.
 */
import * as React from "react"
import * as TogglePrimitive from "@radix-ui/react-toggle"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const toggleVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors text-neutral hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 data-[state=on]:bg-white data-[state=on]:text-foreground data-[state=on]:shadow-segment [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-transparent",
        outline:
          "border border-input bg-transparent shadow-sm hover:bg-surface-muted hover:text-foreground",
      },
      size: {
        default: "h-9 px-2 min-w-9",
        sm: "h-8 px-1.5 min-w-8",
        lg: "h-10 px-2.5 min-w-10",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

const Toggle = React.forwardRef<
  React.ElementRef<typeof TogglePrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof TogglePrimitive.Root> &
    VariantProps<typeof toggleVariants>
>(({ className, variant, size, ...props }, ref) => (
  <TogglePrimitive.Root
    ref={ref}
    className={cn(toggleVariants({ variant, size, className }))}
    {...props}
  />
))

Toggle.displayName = TogglePrimitive.Root.displayName

export { Toggle, toggleVariants }
