/**
 * shadcn/ui `toggle-group`, copied in from the registry — the rail composer's kind picker.
 *
 * Re-added with its importer; see `toggle.tsx` beside it for why that sentence is worth writing
 * down, and for the Tailwind-3 generation note that applies to this file identically.
 *
 * WHY THIS AND NOT A `select`. The choice between a Plan chat and a Build chat is BINARY, both
 * options matter equally, and both need a line of explanation beside them — that is a segmented
 * control, not a dropdown that hides one of two options behind a click. A `select` would also make
 * the choice feel like a setting; it is a fork in what happens next.
 *
 * THE VARIANT CONTEXT is why the group and the item are separate exports rather than one
 * component: a variant set on the group has to reach every item without each call site repeating
 * it, and React context is the registry's answer. Keeping it means a later `variant="outline"` on
 * the group still works, which is the whole reason to copy a registry component rather than write
 * two buttons.
 */
import * as React from "react"
import * as ToggleGroupPrimitive from "@radix-ui/react-toggle-group"
import { type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"
import { toggleVariants } from "@/components/ui/toggle"

const ToggleGroupContext = React.createContext<
  VariantProps<typeof toggleVariants>
>({
  size: "default",
  variant: "default",
})

const ToggleGroup = React.forwardRef<
  React.ElementRef<typeof ToggleGroupPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ToggleGroupPrimitive.Root> &
    VariantProps<typeof toggleVariants>
>(({ className, variant, size, children, ...props }, ref) => (
  <ToggleGroupPrimitive.Root
    ref={ref}
    className={cn("flex items-center justify-center gap-1", className)}
    {...props}
  >
    <ToggleGroupContext.Provider value={{ variant, size }}>
      {children}
    </ToggleGroupContext.Provider>
  </ToggleGroupPrimitive.Root>
))

ToggleGroup.displayName = ToggleGroupPrimitive.Root.displayName

const ToggleGroupItem = React.forwardRef<
  React.ElementRef<typeof ToggleGroupPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof ToggleGroupPrimitive.Item> &
    VariantProps<typeof toggleVariants>
>(({ className, children, variant, size, ...props }, ref) => {
  const context = React.useContext(ToggleGroupContext)

  return (
    <ToggleGroupPrimitive.Item
      ref={ref}
      className={cn(
        toggleVariants({
          variant: context.variant || variant,
          size: context.size || size,
        }),
        className
      )}
      {...props}
    >
      {children}
    </ToggleGroupPrimitive.Item>
  )
})

ToggleGroupItem.displayName = ToggleGroupPrimitive.Item.displayName

export { ToggleGroup, ToggleGroupItem }
