import * as CollapsiblePrimitive from '@radix-ui/react-collapsible'

// shadcn/ui `new-york` collapsible — a thin, fully-owned wrapper over the one Radix primitive
// F3/U3 introduces. Hand-authored (not `npx ai-elements add`): the portal is Tailwind v3.4.17 and
// the v4 registry pull would rewrite components.json / base CSS. No styling of its own — the
// consumer owns the trigger/content chrome (`BuildProgress`, the shipped build narrative).

const Collapsible = CollapsiblePrimitive.Root

const CollapsibleTrigger = CollapsiblePrimitive.CollapsibleTrigger

const CollapsibleContent = CollapsiblePrimitive.CollapsibleContent

export { Collapsible, CollapsibleTrigger, CollapsibleContent }
