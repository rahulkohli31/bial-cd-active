/**
 * shadcn/ui `badge`, copied in from the registry (it was the one primitive
 * `components/ui/` did not have yet).
 *
 * TWO THINGS TO KNOW BEFORE EDITING, both about this build rather than about the component:
 *
 * 1. THE CLASSES ARE THE OLDER (Tailwind-3) REGISTRY GENERATION, matching `button.tsx` in this
 *    folder. The current registry badge uses Tailwind-v4-era arbitrary variants (`[a&]:hover:…`)
 *    and this portal is on Tailwind 3, where a class the build does not produce renders as
 *    NOTHING while every DOM assertion still passes. jsdom computes no Tailwind styles, so no
 *    unit test in this repo can catch that (`src/__tests__/tailwind-tokens.test.js` says so in
 *    its own docblock) — the check is that every token used here (`primary`,
 *    `primary-foreground`, `secondary`, `secondary-foreground`, `destructive`,
 *    `destructive-foreground`, `foreground`, `ring`) is defined in `tailwind.config.js` +
 *    `src/index.css`, plus one look at the rendered badge in a browser.
 *
 * 2. IT RENDERS A `span`, not the older registry's `div`. The badge's only site today sits
 *    inline in a chat row beside the title's `<h3>`, and the current registry generation is
 *    span-based too — this is the one deliberate deviation from the copied source.
 */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive text-destructive-foreground shadow hover:bg-destructive/80",
        outline: "text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
