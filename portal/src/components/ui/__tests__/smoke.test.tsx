// Smoke surface for the shadcn/ui resolver chain (`@/` alias via vitest.config.js) — not a
// component-behaviour test. Toggle/ToggleGroup were removed as dead weight (zero references);
// this stays Button-only by design.
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Button, buttonVariants } from '@/components/ui/button'

describe('shadcn/ui smoke surface', () => {
  it('mounts Button with the brand-token variant classes', () => {
    render(<Button>Build it</Button>)
    const button = screen.getByRole('button', { name: 'Build it' })
    expect(button.className).toContain('bg-primary')
    expect(button.className).toContain('text-primary-foreground')
  })

  it('mounts Button asChild onto an anchor (Slot path)', () => {
    render(
      <Button asChild variant="outline">
        <a href="/somewhere">Go</a>
      </Button>
    )
    const link = screen.getByRole('link', { name: 'Go' })
    expect(link.className).toContain('border-input')
  })

  /**
   * The `destructive` variant came back for ONE caller — `ConnectorReviewDialog`'s `Decline` —
   * and it is the board's red OUTLINE, not the registry's red fill (`button.tsx` departure 4).
   * The re-copy this guards against is `npx shadcn@latest add button`, which would restore
   * `bg-destructive text-destructive-foreground` and silently turn one dialog's refuse control
   * into the loudest thing on screen.
   */
  it('mounts Button in the destructive variant as a red outline, never a red fill', () => {
    render(<Button variant="destructive">Decline</Button>)
    const button = screen.getByRole('button', { name: 'Decline' })
    // Present, so a crashed render cannot pass the absence assertion below it.
    expect(button.className).toContain('border-destructive/30')
    expect(button.className).toContain('text-destructive')
    // TOKEN-WISE, not `toContain`: the hover tint is spelled `hover:bg-destructive/10`, so a
    // substring check would read the outline as a fill and never go red on the re-copy.
    expect(button.className.split(/\s+/)).not.toContain('bg-destructive')
    expect(button.className).toContain('bg-background')
  })

  it('reaches the destructive variant through buttonVariants itself', () => {
    // `Decline` selects it by string literal; this asserts the cva table actually carries the
    // key, so a variant deleted from the table fails here rather than resolving to `default`.
    expect(buttonVariants({ variant: 'destructive' })).toContain('text-destructive')
    expect(buttonVariants({ variant: 'destructive' })).not.toBe(buttonVariants({}))
  })
})
