import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `button`, copied in from the registry, with three deliberate departures — recorded
 * here because the next person to re-copy the registry will reintroduce them.
 *
 * 1. NO `secondary` VARIANT: the registry's resolves to `bg-secondary`, this build's brand gold
 *    #D9A036. The UX canvas paints every primary action #0D7377 and that gold nowhere (one
 *    `:root` declaration across 41 boards, zero usages) — a gold fill is not a variant this
 *    product has. It had no caller when removed; adding one back adds a colour the boards don't draw.
 * 2. `outline`/`ghost` HOVER ON `surface-muted`, NOT `accent`: stock shadcn's `accent` is a
 *    near-neutral tint, this build's is brand orange #F5A623, which turned the transcript's
 *    Copy button solid orange under the pointer. The canvas specifies one hover in 41 boards
 *    (a link going teal-dark) and no orange surface anywhere; `accent` keeps its two real board
 *    roles (token-meter fill, 6px unsaved dot) and stops being a hover.
 * 3. THE VARIANT TABLE IS ONLY WHAT IS MOUNTED: `link` and sizes `sm`/`lg` went the way
 *    `secondary` did — the one production call site (`assistant-ui/thread.tsx`'s copy control)
 *    asks for `variant="ghost" size="icon"`, and the portal has no `variant={…}`/`size={…}`
 *    expression to select any other key. `outline` STAYS: it's the sole coverage of the
 *    `asChild`/Slot branch below, live code riding the variant as a vehicle. `destructive` was
 *    pruned with them and has come back — see 4.
 * 4. `destructive` IS AN OUTLINE, NOT THE REGISTRY'S RED FILL. It was pruned for having no live
 *    caller; `ConnectorReviewDialog`'s `Decline` is that caller, and the `AdminReview` board
 *    draws it as a white button with a red hairline and red label, beside a teal-filled approve.
 *    Stock shadcn's `bg-destructive text-destructive-foreground` would put two solid buttons in
 *    one action row and make the destructive one the loudest thing in the dialog — the opposite
 *    of what the board weights. The COLOUR is the ramp's `--destructive`, not the board's
 *    #B4483F/#F4C7C7 pair: `tailwind.config.js` already owns those two as the `problem` family,
 *    whose documented role is "a group whose work failed" inside a transcript — a container, at
 *    container weights — and a second red on the one control that means "refuse" is how two
 *    reds start disagreeing about which is the refusal.
 *    IT IS STILL SELECTED ONLY BY STRING LITERAL. `variant="destructive"` at one call site; the
 *    claim in 3 that this portal has no runtime variant expression is what lets that list stay
 *    honest, and it is load-bearing for the pruning argument itself.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow hover:bg-primary/90",
        outline:
          "border border-input bg-background shadow-sm hover:bg-surface-muted hover:text-foreground",
        destructive:
          "border border-destructive/30 bg-background text-destructive shadow-sm hover:bg-destructive/10 hover:text-destructive",
        ghost: "hover:bg-surface-muted hover:text-foreground",
      },
      size: {
        default: "h-9 px-4 py-2",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button"
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
