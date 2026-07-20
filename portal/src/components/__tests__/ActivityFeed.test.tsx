import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, within } from '@testing-library/react'
import ActivityFeed from '../ActivityFeed'
import type { FeedEnvelope } from '../../utils/buildSessionTypes'

afterEach(cleanup)

describe('ActivityFeed — renders each of the 6 feed members (C7 §3)', () => {
  it('step: spinner → check → cross across started / ok / failed', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding…', state: 'started' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing…', state: 'ok' },
      { type: 'step', seq: 3, name: 'build', label: 'Building…', state: 'failed' },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const steps = container.querySelectorAll('li[data-kind="step"]')
    expect([...steps].map((s) => s.getAttribute('data-state'))).toEqual(['started', 'ok', 'failed'])
    expect(container.querySelector('li[data-state="started"] .animate-spin')).toBeTruthy() // spinner while running
    expect(container.textContent).toContain('Scaffolding…')
  })

  it('log: stdout vs stderr are styled distinctly and show the raw line', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'log', seq: 1, source: 'exec', stream: 'stdout', text: 'added 10 packages' },
      { type: 'log', seq: 2, source: 'dev', stream: 'stderr', text: 'warning: something' },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const out = container.querySelector('li[data-stream="stdout"]')
    const err = container.querySelector('li[data-stream="stderr"]')
    expect(out?.textContent).toContain('added 10 packages')
    expect(err?.textContent).toContain('warning: something')
    // stderr carries the danger tone; stdout does not.
    expect(err?.querySelector('.text-danger\\/90')).toBeTruthy()
    expect(out?.querySelector('.text-danger\\/90')).toBeNull()
  })

  it('error: shows the title, the source, and the cleaned stack', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'error', seq: 1, source: 'tsc', title: 'Type error in app/page.tsx', cleaned_stack: "app/page.tsx(12,5): Type 'string' is not assignable to 'number'." },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const row = container.querySelector('li[data-kind="error"]')!
    expect(row.textContent).toContain('Type error in app/page.tsx')
    expect(within(row as HTMLElement).getByText(/tsc/i)).toBeTruthy()
    expect(row.querySelector('pre')?.textContent).toContain("not assignable to 'number'")
  })

  it('escalation: shows the reason/detail (and the last error title when present)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'escalation', seq: 1, reason: 'self_heal_budget_exhausted', detail: 'Could not repair after several tries.', last_error: { source: 'next_build', title: 'Build failed at /page', cleaned_stack: '…' } },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const row = container.querySelector('li[data-kind="escalation"]')!
    expect(row.textContent).toContain('Could not repair after several tries.')
    expect(row.textContent).toContain('Build failed at /page')
  })

  it('quota_exceeded: formats the limit and the IST reset copy', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'quota_exceeded', seq: 1, limit: 1_000_000, used: 1_000_000, resets_at: '2026-07-15T18:30:00Z' },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    expect(container.textContent).toMatch(/1,000,000 tokens/)
    expect(container.textContent).toMatch(/resets at midnight IST/i)
  })

  it('ended: renders the terminal chip keyed on reason', () => {
    const done = render(<ActivityFeed envelopes={[{ type: 'ended', seq: 1, status: 'ended', preview_url: null, snapshot_committed: true, reason: 'completed' }]} />)
    expect(done.container.textContent).toMatch(/build completed/i)
    cleanup()

    const failed = render(<ActivityFeed envelopes={[{ type: 'ended', seq: 1, status: 'failed', preview_url: null, snapshot_committed: false, reason: 'build_failed' }]} />)
    const chip = failed.container.querySelector('li[data-kind="ended"]')
    expect(chip?.getAttribute('data-status')).toBe('failed')
    expect(failed.container.textContent).toMatch(/build failed/i)
  })
})

describe('ActivityFeed — ordering + idempotency (C3 §4.2)', () => {
  it('renders envelopes in seq order regardless of arrival order', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 3, name: 'c', label: 'Third', state: 'started' },
      { type: 'step', seq: 1, name: 'a', label: 'First', state: 'ok' },
      { type: 'step', seq: 2, name: 'b', label: 'Second', state: 'ok' },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const labels = [...container.querySelectorAll('li[data-kind="step"]')].map((li) => li.textContent)
    expect(labels).toEqual(['First', 'Second', 'Third'])
  })

  it('two envelopes bearing the same seq render exactly ONE row (last-wins)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'x', label: 'Original', state: 'started' },
      { type: 'step', seq: 1, name: 'x', label: 'Replaced', state: 'ok' },
    ]
    const { container } = render(<ActivityFeed envelopes={envelopes} />)
    const rows = container.querySelectorAll('li[data-kind="step"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].getAttribute('data-state')).toBe('ok') // last-wins
    expect(rows[0].textContent).toContain('Replaced')
  })

  it('is a polite live region (role=log) that does not steal focus', () => {
    const { container } = render(<ActivityFeed envelopes={[]} />)
    const region = container.querySelector('[role="log"]')
    expect(region?.getAttribute('aria-live')).toBe('polite')
    expect(container.textContent).toMatch(/waiting for build activity/i)
  })
})
