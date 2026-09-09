import * as React from "react"
import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu"

import { cn } from "@/lib/utils"

/**
 * WHY THIS EXISTS: hand-authored shadcn `new-york` dropdown-menu, matching the other
 * primitives in this folder (`popover.tsx`, `select.tsx`, `dialog.tsx`) rather than a
 * `npx shadcn@latest add` of the current registry. Five things before editing:
 *
 * 1. CLASSES ARE THE OLDER (Tailwind-3) REGISTRY GENERATION — this portal is on Tailwind
 *    3.4.17, and a class the build does not produce renders as NOTHING while every jsdom
 *    assertion still passes (jsdom computes no styles). The current registry's copy of this
 *    component is v4-only in at least two places: `origin-(--radix-dropdown-menu-content-
 *    transform-origin)` and `max-h-(--radix-dropdown-menu-content-available-height)` use the
 *    v4 bare-custom-property shorthand, which v3 cannot parse. Tokens used below —
 *    `--popover`, `--popover-foreground`, `--border`, `--radius` — are all declared in
 *    `tailwind.config.js` + `index.css` (`:root` and `.dark`, confirmed).
 *
 * 2. THE ITEM'S FOCUS STATE CARRIES NO HUE, DELIBERATELY — the same departure `toggle.tsx`
 *    records. The registry's `focus:bg-accent focus:text-accent-foreground` resolves to the
 *    BRAND ORANGE here (`accent.DEFAULT` is `#F5A623`, `--accent: 37 91% 55%`), so a
 *    keyboard-focused menu item would light up orange. `focus:bg-surface-muted` is the same
 *    quiet grey the portal's other hover states use.
 *
 * 3. PORTALLED, like `popover.tsx`: the menu is anchored to a control in a `sticky` navbar
 *    that other shells nest inside `overflow-hidden` columns, and portalled content cannot be
 *    clipped by an ancestor a future layout adds. It also means the content is NOT inside the
 *    `<nav>` — anything that dismisses by "is this node inside the nav?" would fight Radix's
 *    own `DismissableLayer` (see `Navbar.tsx`, which deleted exactly such a handler).
 *
 * 4. THE REGISTRY'S `[&>svg]:size-4` IS NOT HERE. Every lucide icon in this portal is sized
 *    at the call site (`<LogOut size={13} />`, `<MessageSquare size={17} />`), and a CSS
 *    `width/height: 1rem` beats the attribute the `size` prop writes — so vendoring that class
 *    would silently round every caller's icon up to 16px. (`toggle-group.tsx` carries it and its
 *    consumer's `size={12}` icons do render at 16; that is the bug, not the precedent.)
 *    `[&>svg]:shrink-0` stays: it protects the label from squashing the icon, and overrides
 *    nothing a caller set.
 *
 * 5. THE ALIAS SET IS TRIMMED TO THE FIVE NAMES THE ONE CONSUMER MOUNTS. Group, Separator,
 *    Shortcut, CheckboxItem, RadioGroup/RadioItem, ItemIndicator, Sub* and the `inset` prop
 *    were all vendored by the registry and reach nothing; they are named here because the next
 *    `npx shadcn@latest add` listing `dropdown-menu` restores them as an unexplained diff.
 *    Re-add one together with its caller, not on spec.
 */

const DropdownMenu = DropdownMenuPrimitive.Root

const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger

const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        "z-50 min-w-[8rem] overflow-hidden rounded-md border bg-popover p-1 text-popover-foreground shadow-md data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2",
        className
      )}
      {...props}
    />
  </DropdownMenuPrimitive.Portal>
))
DropdownMenuContent.displayName = DropdownMenuPrimitive.Content.displayName

const DropdownMenuLabel = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Label>
>(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.Label
    ref={ref}
    className={cn("px-2 py-1.5 text-sm font-semibold", className)}
    {...props}
  />
))
DropdownMenuLabel.displayName = DropdownMenuPrimitive.Label.displayName

const DropdownMenuItem = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item>
>(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
    ref={ref}
    className={cn(
      "relative flex cursor-default select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none transition-colors focus:bg-surface-muted focus:text-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&>svg]:shrink-0",
      className
    )}
    {...props}
  />
))
DropdownMenuItem.displayName = DropdownMenuPrimitive.Item.displayName

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuItem,
}
