// Smoke surface for the shadcn/ui infrastructure (U13 prep): the two vendored
// components mount under the portal's real resolver chain (`@/` alias through
// vitest.config.js) and carry their variant classes. No app code uses them yet —
// this test is what keeps the wiring honest until the U13 mode toggle does.
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

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

  it('mounts a single-select ToggleGroup — the U13 mode-toggle shape', () => {
    render(
      <ToggleGroup type="single" defaultValue="plan" aria-label="Mode">
        <ToggleGroupItem value="ask">Ask</ToggleGroupItem>
        <ToggleGroupItem value="plan">Plan</ToggleGroupItem>
        <ToggleGroupItem value="write">Write</ToggleGroupItem>
      </ToggleGroup>
    )
    const selected = screen.getByRole('radio', { name: 'Plan' })
    expect(selected).toHaveProperty('dataset.state', 'on')
    expect(selected.className).toContain('data-[state=on]:bg-accent')
    expect(screen.getByRole('radio', { name: 'Ask' })).toHaveProperty('dataset.state', 'off')
  })
})
