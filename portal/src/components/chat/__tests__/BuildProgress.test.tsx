/**
 * BuildProgress (U15): the chat-native build narrative that replaced the ActivityFeed
 * pane + SessionControls row. What this pins:
 *  - friendly step rows across started/ok/failed, in seq order, deduped last-wins by seq;
 *  - raw log lines render ONLY inside the Details expander — never ambient in the bubble;
 *  - the headline transitions: working (with elapsed reassurance) → "Your app is ready";
 *  - `ended` renders nothing here (the persisted BuildOutcome message is the record);
 *  - Stop fires directly; Force-end CONFIRMS first (it kills in-progress work);
 *  - error / escalation / quota alerts stay visible, styled by kind.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import BuildProgress from '../BuildProgress'
import type { FeedEnvelope } from '../../../utils/buildSessionTypes'

afterEach(cleanup)

const noop = () => {}

function draw(props: Partial<Parameters<typeof BuildProgress>[0]> = {}) {
  return render(
    <BuildProgress
      envelopes={[]}
      status="building"
      startedAt={null}
      stopping={false}
      onStop={noop}
      onForceEnd={noop}
      {...props}
    />,
  )
}

describe('the friendly step narrative', () => {
  it('renders steps across started / ok / failed with their icons, in seq order', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 3, name: 'build', label: 'Checking everything works', state: 'failed' },
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container } = draw({ envelopes })
    const steps = [...container.querySelectorAll('[data-kind="step"]')]
    expect(steps.map((s) => s.getAttribute('data-state'))).toEqual(['ok', 'started', 'failed'])
    expect(container.querySelector('[data-state="started"] .animate-spin')).toBeTruthy()
    expect(container.textContent).toContain('Scaffolding your app')
  })

  it('two envelopes bearing the same seq render exactly ONE row (last-wins, C3 §4.2)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'install', label: 'Installing packages', state: 'started' },
      { type: 'step', seq: 1, name: 'install', label: 'Installing packages', state: 'ok' },
    ]
    const { container } = draw({ envelopes })
    const steps = container.querySelectorAll('[data-kind="step"]')
    expect(steps).toHaveLength(1)
    expect(steps[0].getAttribute('data-state')).toBe('ok')
  })

  it('steps live in a polite live region (role=log) that does not steal focus', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 's', label: 'Working', state: 'started' },
    ]
    draw({ envelopes })
    const log = screen.getByRole('log', { name: /build activity/i })
    expect(log.getAttribute('aria-live')).toBe('polite')
  })
})

describe('raw output stays behind Details (zero ambient shell lines)', () => {
  it('log lines render ONLY inside the expander, stderr styled distinctly', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'log', seq: 1, source: 'exec', stream: 'stdout', text: 'added 10 packages' },
      { type: 'log', seq: 2, source: 'dev', stream: 'stderr', text: 'warning: something' },
    ]
    const { container } = draw({ envelopes })
    const details = container.querySelector('details')
    expect(details).toBeTruthy()
    // The raw lines are INSIDE the expander — nothing mono leaks into the bubble body.
    expect(details?.textContent).toContain('added 10 packages')
    const outside = container.textContent?.replace(details?.textContent ?? '', '')
    expect(outside).not.toContain('added 10 packages')
    expect(details?.querySelector('[data-stream="stderr"] .text-danger\\/90')).toBeTruthy()
    expect(details?.querySelector('[data-stream="stdout"] .text-danger\\/90')).toBeNull()
  })

  it('no logs → no Details expander at all', () => {
    const { container } = draw({
      envelopes: [{ type: 'step', seq: 1, name: 's', label: 'Working', state: 'ok' }],
    })
    expect(container.querySelector('details')).toBeNull()
  })
})

describe('the headline transitions', () => {
  it('building shows the working line with the elapsed-time reassurance', () => {
    const { container } = draw({ startedAt: Date.now() - 90_000 })
    expect(container.textContent).toMatch(/Building your app/i)
    expect(container.textContent).toMatch(/running 1m 30s/)
  })

  it('preview_ready flips the headline to the visible "Your app is ready" transition', () => {
    const { container } = draw({ status: 'ready' })
    expect(container.textContent).toMatch(/Your app is ready — the preview is live on the right/i)
    expect(container.querySelector('.animate-spin')).toBeNull() // no longer "working"
  })

  it('ended with no envelopes renders NOTHING — the persisted outcome message is the record', () => {
    const { container } = draw({ status: 'ended' })
    expect(container.firstChild).toBeNull()
  })

  it('an ended envelope renders no terminal chip (the BuildOutcome bubble owns the terminal)', () => {
    const { container } = draw({
      status: 'ended',
      envelopes: [
        { type: 'step', seq: 1, name: 's', label: 'Scaffolding your app', state: 'ok' },
        { type: 'ended', seq: 2, status: 'ended', reason: 'completed', preview_url: null, snapshot_committed: true },
      ],
    })
    expect(container.textContent).toContain('Scaffolding your app') // the story stays
    expect(container.textContent).not.toMatch(/Build completed/i) // the chip does not
  })
})

describe('the controls on the bubble', () => {
  it('Stop fires directly and shows the pending state while stopping', () => {
    const onStop = vi.fn()
    draw({ onStop })
    fireEvent.click(screen.getByRole('button', { name: /^Stop$/ }))
    expect(onStop).toHaveBeenCalledTimes(1)

    cleanup()
    draw({ stopping: true })
    const pending = screen.getByRole('button', { name: /Stopping…/ }) as HTMLButtonElement
    expect(pending.disabled).toBe(true)
  })

  it('Force-end confirms first (surfacing elapsed time); only Confirm fires onForceEnd', () => {
    const onForceEnd = vi.fn()
    draw({ onForceEnd, startedAt: Date.now() - 65_000 })
    fireEvent.click(screen.getByRole('button', { name: /Force-end/ }))
    expect(onForceEnd).not.toHaveBeenCalled() // asked, not fired
    expect(screen.getByText(/kills in-progress work.*running 1m 5s/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /^Force-end$/ }))
    expect(onForceEnd).toHaveBeenCalledTimes(1)
  })

  it('Cancel backs out without force-ending; controls are absent once terminal', () => {
    const onForceEnd = vi.fn()
    draw({ onForceEnd })
    fireEvent.click(screen.getByRole('button', { name: /Force-end/ }))
    fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(onForceEnd).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /^Stop$/ })).toBeTruthy() // back to controls

    cleanup()
    draw({
      status: 'ended',
      envelopes: [{ type: 'step', seq: 1, name: 's', label: 'x', state: 'ok' }],
    })
    expect(screen.queryByRole('button', { name: /^Stop$/ })).toBeNull()
  })
})

describe('alerts stay visible', () => {
  it('error / escalation / quota render friendly, styled by kind', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'error', seq: 1, source: 'tsc', title: 'Type error in app/page.tsx', cleaned_stack: 'app/page.tsx(12,5)' },
      { type: 'escalation', seq: 2, reason: 'max_retries', detail: 'The self-heal loop gave up.', last_error: null },
      { type: 'quota_exceeded', seq: 3, limit: 1000000, used: 1000001, resets_at: 'x' },
    ]
    const { container } = draw({ envelopes })
    expect(container.querySelector('[data-kind="error"]')?.textContent).toContain('Type error in app/page.tsx')
    expect(container.querySelector('[data-kind="escalation"]')?.textContent).toContain('gave up')
    expect(container.querySelector('[data-kind="quota_exceeded"]')?.textContent).toMatch(/daily limit/i)
  })
})

describe('F3/U3: friendly labels only, hidden steps dropped, zero raw shell', () => {
  it('a hidden step renders no <li data-kind="step"> and its label never shows', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'run_command', label: "Inspected the app's files", state: 'ok', hidden: true },
      { type: 'step', seq: 2, name: 'edit', label: "Building your app's main page", state: 'ok' },
    ]
    const { container } = draw({ envelopes })
    const steps = container.querySelectorAll('[data-kind="step"]')
    expect(steps).toHaveLength(1)
    expect(container.textContent).toContain("Building your app's main page")
    expect(container.textContent).not.toContain("Inspected the app's files")
  })

  it('the visible Build activity list carries the friendly label and NO raw shell/argv', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'run_command', label: 'Setting up the tools your app needs', state: 'ok' },
      { type: 'step', seq: 2, name: 'run_command', label: 'Working on your app', state: 'failed' },
    ]
    draw({ envelopes })
    const log = screen.getByRole('log', { name: /build activity/i })
    expect(log.textContent).toContain('Setting up the tools your app needs')
    for (const raw of ['$ ', 'npx', 'bash -c', 'ls -la', 'npm install']) {
      expect(log.textContent).not.toContain(raw)
    }
  })

  it('the multi-step feed collapses under a real <button aria-expanded> header (Mode B)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'edit', label: 'Building your app’s main page', state: 'ok' },
      { type: 'step', seq: 2, name: 'run_command', label: 'Setting up the tools your app needs', state: 'ok' },
    ]
    const { container } = draw({ envelopes })
    expect(container.querySelector('button[aria-expanded]')).toBeTruthy()
  })
})
