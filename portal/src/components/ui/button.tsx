import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `button`, copied in from the registry, with two deliberate departures from the
 * copied source — both recorded here because the next person to re-copy the registry will
 * reintroduce them.
 *
 * 1. THERE IS NO `secondary` VARIANT. The registry's resolves to `bg-secondary`, which in this
 *    build is the brand gold #D9A036. The UX canvas paints every primary action #0D7377 and
 *    paints that gold nowhere at all (one `:root` declaration across 41 boards, zero usages),
 *    so a gold action fill is not a variant this product has. It had no caller when it was
 *    removed; adding one back means adding a colour the boards do not draw.
 * 2. `outline` AND `ghost` HOVER ON `surface-muted`, NOT ON `accent`. Stock shadcn means
 *    `accent` as a near-neutral hover tint; this build's `accent` is the brand orange #F5A623,
 *    which turned the transcript's Copy button solid orange under the pointer. The canvas
 *    specifies exactly one hover in 41 boards — a link going teal-dark — and no orange surface
 *    anywhere. `accent` keeps its two real board roles (the token-meter fill and the 6px
 *    unsaved dot) and stops being a hover.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow hover:bg-primary/90",
        destructive:
          "bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90",
        outline:
          "border border-input bg-background shadow-sm hover:bg-surface-muted hover:text-foreground",
        ghost: "hover:bg-surface-muted hover:text-foreground",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-md px-3 text-xs",
        lg: "h-10 rounded-md px-8",
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
