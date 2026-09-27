import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `badge` from `npx shadcn@2.3.0 add badge` (the Tailwind 3 registry), changed in two
 * ways:
 *
 * - The pill is `rounded-full` at 11px bold, the one pill shape the admin screens draw, rather
 *   than the registry's `rounded-md` at 12px semibold.
 * - No `secondary` variant: it resolves to `bg-secondary`, this build's brand gold, which no
 *   screen uses as a fill (see `button.tsx`).
 *
 * Coloured status pills use `variant="outline"` with their colours as a className, so no
 * `hover:` fill from another variant survives underneath them.
 */
const badgeVariants = cva(
  "inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-bold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
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
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

export { Badge, badgeVariants }
