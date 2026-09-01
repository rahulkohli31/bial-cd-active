import * as React from "react"
import * as PopoverPrimitive from "@radix-ui/react-popover"

import { cn } from "@/lib/utils"

/**
 * Hand-authored shadcn `new-york` popover primitive, matching `dropdown-menu.tsx` in this
 * folder rather than the current registry.
 *
 * THREE THINGS TO KNOW BEFORE EDITING, all about this build rather than the component:
 *
 * 1. THE CLASSES ARE THE OLDER (Tailwind-3) REGISTRY GENERATION. The current registry
 *    popover uses Tailwind-v4-era syntax, and this portal is on Tailwind 3.4.17, where a
 *    class the build does not produce renders as NOTHING while every DOM assertion still
 *    passes. jsdom computes no Tailwind styles, so no unit test in this repo can catch
 *    that, and `src/__tests__/tailwind-tokens.test.js` guards only the `bial-*` namespace
 *    — every token used here is outside it. The check is that `--popover`,
 *    `--popover-foreground` and `--border` are declared in `tailwind.config.js` +
 *    `src/index.css` (they are, all three, in both `:root` and `.dark`), plus one look at
 *    the rendered popover in a real browser.
 *
 * 2. IT IS PORTALLED, AND THAT IS LOAD-BEARING HERE rather than a default carried over.
 *    Its one consumer mounts inside the builder's pane toolbar, under four nested
 *    `overflow-hidden` ancestors (`WorkspaceShell` root, its row wrapper, the pane column,
 *    and `AppPaneHost`'s pane). Content anchored without a portal is clipped the moment it
 *    extends past the toolbar row — it does not overflow, it disappears.
 *
 * 3. `@radix-ui/react-popover` IS NOW A DIRECT DEPENDENCY. It was already on disk as a
 *    transitive of `@assistant-ui/react` → `radix-ui`, so importing it would have
 *    resolved — and silently broken on any future install that reshaped that graph.
 */

const Popover = PopoverPrimitive.Root

const PopoverTrigger = PopoverPrimitive.Trigger

const PopoverAnchor = PopoverPrimitive.Anchor

const PopoverClose = PopoverPrimitive.Close

const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className, align = "start", sideOffset = 6, ...props }, ref) => (
  <PopoverPrimitive.Portal>
    <PopoverPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      className={cn(
        "z-50 w-72 rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2",
        className
      )}
      {...props}
    />
  </PopoverPrimitive.Portal>
))
PopoverContent.displayName = PopoverPrimitive.Content.displayName

export { Popover, PopoverTrigger, PopoverAnchor, PopoverClose, PopoverContent }
