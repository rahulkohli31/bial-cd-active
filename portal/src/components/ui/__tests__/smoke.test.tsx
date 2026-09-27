// Smoke surface for the shadcn/ui resolver chain (`@/` alias via vitest.config.js) — not a
// component-behaviour test. Toggle/ToggleGroup were removed as dead weight (zero references);
// this stays Button-only by design.
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'

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
})
