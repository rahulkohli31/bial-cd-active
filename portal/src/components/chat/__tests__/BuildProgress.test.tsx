/**
 * BuildProgress (U15): the chat-native build narrative that replaced the ActivityFeed
 * pane + SessionControls row. What this pins:
 *  - WHILE WORKING: exactly ONE step row is visible at a time — the most recent — in a
 *    fixed spot, replacing itself as new steps arrive rather than accumulating a list;
 *  - AFTER the build ends: the full step history (all steps, in seq order, deduped
 *    last-wins by seq) becomes available behind a dropdown that is COLLAPSED by default;
 *  - NOTHING developer-facing renders anywhere: no raw log expander, no <pre> stack, no
 *    compiler-authored title — every error status is a product sentence plus a next action (U16);
 *  - the headline transitions: working (with elapsed reassurance) → "Your app is ready";
 *  - `ended` renders nothing here (the persisted BuildOutcome message is the record);
 *  - Stop fires directly; Force-end CONFIRMS first (it kills in-progress work);
 *  - error / escalation / quota alerts stay visible, styled by kind.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import BuildProgress, {
  ERROR_FALLBACK_ACTION,
  ERROR_FALLBACK_MESSAGE,
  atLimitSendState,
  formatResetTime,
  hasBuildNarrative,
  withMailtoLinks,
} from '../BuildProgress'
import type {
  ErrorSource,
  FeedEnvelope,
  QuotaExceededEvent,
} from '../../../utils/buildSessionTypes'
import { narrativeEnvelopes } from '../../../utils/turnNarrative'

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

describe('the live view shows exactly one step at a time', () => {
  it('while working, only the MOST RECENT step is visible — not the earlier ones', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container } = draw({ envelopes })
    const steps = container.querySelectorAll('[data-kind="tool-activity"]')
    expect(steps).toHaveLength(1)
    expect(container.textContent).toContain('Installing packages')
    expect(container.textContent).not.toContain('Scaffolding your app')
  })

  it('a new step REPLACES the previous one in the same spot, not appended below it', () => {
    const first: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'started' },
    ]
    const { container, rerender } = draw({ envelopes: first })
    expect(container.textContent).toContain('Scaffolding your app')

    const second: FeedEnvelope[] = [
      ...first,
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    rerender(
      <BuildProgress
        envelopes={second}
        status="building"
        startedAt={null}
        stopping={false}
        onStop={noop}
        onForceEnd={noop}
      />,
    )
    expect(container.querySelectorAll('[data-kind="tool-activity"]')).toHaveLength(1)
    expect(container.textContent).toContain('Installing packages')
    expect(container.textContent).not.toContain('Scaffolding your app')
  })

  it('the current step keeps its state icon (spinner while started)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container } = draw({ envelopes })
    expect(container.querySelector('[data-state="started"] .animate-spin')).toBeTruthy()
  })

  it('a failed step announces "failed" as CONTAINED text, not a page-stretching absolute', () => {
    // The sr-only span is a deliberate WCAG 1.4.1 affordance (failure as text, never colour
    // alone) — do not delete it. sr-only is position:absolute with no inset; without a
    // positioned ancestor it anchors to the DOCUMENT and stretched a long transcript to
    // ~11,558px against an 836px viewport, measured at runtime. jsdom cannot measure layout,
    // so this pins the two halves it can see: the affordance text survives, and the wrapper
    // carries the positioning class that contains it (verified live: `relative` → 836px).
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'build', label: 'Checking everything works', state: 'failed' },
    ]
    const { container } = draw({ envelopes })
    const line = container.querySelector('[data-kind="tool-activity"][data-state="failed"]')
    expect(line).toBeTruthy()
    expect(line?.querySelector('.sr-only')?.textContent).toBe('failed')
    expect(line?.className).toContain('relative')
  })

  it('the current step lives in a polite live region (role=status) that does not steal focus', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 's', label: 'Working', state: 'started' },
    ]
    draw({ envelopes })
    const status = screen.getByRole('status', { name: /build activity/i })
    expect(status.getAttribute('aria-atomic')).toBe('true')
  })

  it('once a later step resolves, an earlier still-"started" step degrades to the generic "Working…" placeholder — indistinguishable from a permanent orphan otherwise (fix 2)', () => {
    // A later-started step resolving before an earlier one is legitimate under a parallel
    // tool batch — but it is ALSO exactly what a permanently-orphaned step (stuck at
    // 'started' forever, e.g. a snapshot/toolCallId key mismatch) looks like from the
    // envelope stream alone. There is no way to tell the two apart, so the earlier step no
    // longer pins the row once anything after it has resolved; it falls back to a neutral
    // "Working…" rather than either showing a possibly-stale label forever or guessing.
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'edit', label: 'Building your app’s main page', state: 'started' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'ok' },
    ]
    const { container } = draw({ envelopes })
    expect(container.textContent).not.toContain('Building your app’s main page')
    expect(container.textContent).toContain('Working…')
  })

  it('an orphaned started step does not mask a genuinely failed later step', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'edit', label: 'Building your app’s main page', state: 'started' },
      { type: 'step', seq: 2, name: 'build', label: 'Checking everything works', state: 'failed' },
    ]
    const { container } = draw({ envelopes })
    const line = container.querySelector('[data-kind="tool-activity"]')
    expect(line?.getAttribute('data-state')).toBe('failed')
    expect(container.textContent).toContain('Checking everything works')
    expect(container.textContent).not.toContain('Building your app’s main page')
  })
})

describe('after the build ends, the full step history is a collapsed dropdown', () => {
  it('is collapsed by default — no step rows in the DOM until opened', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'ok' },
    ]
    const { container } = draw({ envelopes, status: 'ended' })
    const trigger = container.querySelector('button[aria-expanded]')
    expect(trigger?.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelectorAll('[data-kind="step"]')).toHaveLength(0)
  })

  it('fails open — a build with a failed step defaults to EXPANDED, with the failed count in the trigger', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'build', label: 'Checking everything works', state: 'failed' },
    ]
    const { container } = draw({ envelopes, status: 'ended' })
    const trigger = container.querySelector('button[aria-expanded]')
    expect(trigger?.getAttribute('aria-expanded')).toBe('true')
    expect(trigger?.textContent).toContain('1 failed')
    expect(container.querySelectorAll('[data-kind="step"]')).toHaveLength(2)
  })

  it('opening it reveals every step, in seq order, with their icons', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 3, name: 'build', label: 'Checking everything works', state: 'failed' },
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'ok' },
    ]
    const { container } = draw({ envelopes, status: 'ended' })
    // A failed step is present, so this defaults to expanded already (finding 7's fail-open) —
    // don't click, that would toggle it CLOSED.
    const steps = [...container.querySelectorAll('[data-kind="step"]')]
    expect(steps.map((s) => s.getAttribute('data-state'))).toEqual(['ok', 'ok', 'failed'])
    expect(container.textContent).toContain('Scaffolding your app')
    expect(container.textContent).toContain('Checking everything works')
  })

  it('two envelopes bearing the same seq collapse to ONE step (last-wins, C3 §4.2)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'install', label: 'Installing packages', state: 'ok' },
      { type: 'step', seq: 1, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container } = draw({ envelopes, status: 'ended' })
    fireEvent.click(container.querySelector('button[aria-expanded]') as HTMLButtonElement)
    const steps = container.querySelectorAll('[data-kind="step"]')
    expect(steps).toHaveLength(1)
    expect(steps[0].getAttribute('data-state')).toBe('started')
  })

  it('while the build is still working, no dropdown/history exists at all', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container } = draw({ envelopes, status: 'building' })
    expect(container.querySelector('button[aria-expanded]')).toBeNull()
  })

  it('a single mounted instance survives the building→ended flip with no step lost', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app', state: 'ok' },
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'started' },
    ]
    const { container, rerender } = draw({ envelopes, status: 'building' })
    expect(screen.getByRole('status', { name: /build activity/i })).toBeTruthy()

    const ended: FeedEnvelope[] = [
      ...envelopes.slice(0, 1),
      { type: 'step', seq: 2, name: 'install', label: 'Installing packages', state: 'ok' },
    ]
    rerender(
      <BuildProgress
        envelopes={ended}
        status="ended"
        startedAt={null}
        stopping={false}
        onStop={noop}
        onForceEnd={noop}
      />,
    )
    expect(screen.queryByRole('status', { name: /build activity/i })).toBeNull()
    const trigger = container.querySelector('button[aria-expanded]')
    expect(trigger?.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger as HTMLButtonElement)
    expect(container.textContent).toContain('Scaffolding your app')
    expect(container.textContent).toContain('Installing packages')
    expect(container.querySelectorAll('[data-kind="step"]')).toHaveLength(2)
  })
})

describe('U16: the raw-output expander is gone, not merely quieter', () => {
  it('log envelopes render NOTHING — no expander, no lines, not even behind a click', () => {
    // FLIPPED, NOT DELETED. This block used to pin the expander as the CORRECT home for raw
    // shell output ("log lines render ONLY inside the expander"), which made a developer
    // surface one disclosure click away from every citizen. The lines are still produced and
    // still relayed to the model; the bubble simply has nowhere to put them.
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 's', label: 'Setting up the tools your app needs', state: 'started' },
      { type: 'log', seq: 2, source: 'exec', stream: 'stdout', text: 'added 10 packages' },
      { type: 'log', seq: 3, source: 'dev', stream: 'stderr', text: 'Error: EADDRINUSE :::3000' },
    ]
    const { container } = draw({ envelopes })
    // LIVENESS FIRST: the bubble rendered, so the absences below are absences and not a crash.
    expect(container.querySelector('[data-testid="build-progress"]')).toBeTruthy()
    expect(container.textContent).toContain('Setting up the tools your app needs')

    expect(container.querySelector('details')).toBeNull()
    expect(container.querySelector('[data-kind="log"]')).toBeNull()
    expect(container.textContent).not.toContain('added 10 packages')
    expect(container.textContent).not.toContain('EADDRINUSE')
  })
})

describe('the headline transitions', () => {
  it('building shows the working line with the elapsed-time reassurance', () => {
    const { container } = draw({ startedAt: Date.now() - 90_000 })
    expect(container.textContent).toMatch(/Building your app/i)
    expect(container.textContent).toMatch(/running 1m 30s/)
  })

  it('F4: `ready` frames the preview but does NOT claim the app is finished', () => {
    // `ready` means the dev server started serving. The agent routinely keeps working for
    // several more minutes after it, so the old copy ("Your app is ready") announced a finish
    // that had not happened — and with the spinner stopped at the same moment, a command wedged
    // for four minutes was indistinguishable from a completed build.
    const { container } = draw({ status: 'ready', startedAt: Date.now() - 90_000 })
    expect(container.textContent).toMatch(/preview is live on the right/i)
    expect(container.textContent).not.toMatch(/your app is ready/i)
    // The indicator KEEPS RUNNING, because the work does.
    expect(container.querySelector('.animate-spin')).not.toBeNull()
    expect(container.textContent).toMatch(/running 1m 30s/)
  })

  it('F4, the terminal (the regression this change could introduce): ended STOPS the indicator', () => {
    // Decoupling the spinner from `building` without naming a stop condition would leave it —
    // and the clock — running forever after every successful build. `ended`/`failed` are the
    // stop, and they must be, or one wrong state is simply traded for another.
    for (const status of ['ended', 'failed'] as const) {
      const { container } = draw({
        status,
        startedAt: Date.now() - 90_000,
        envelopes: [{ type: 'step', seq: 1, name: 's', label: 'Scaffolding your app', state: 'ok' }],
      })
      expect(container.querySelector('.animate-spin')).toBeNull()
      expect(container.textContent).not.toMatch(/running /)
    }
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
    expect(container.textContent).not.toMatch(/Build completed/i) // the chip does not render here
    fireEvent.click(container.querySelector('button[aria-expanded]') as HTMLButtonElement)
    expect(container.textContent).toContain('Scaffolding your app') // the story stays, behind the dropdown
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
    const error = container.querySelector('[data-kind="error"]')
    // FLIPPED (U16): this used to assert the compiler's own title WAS the error row's headline.
    // The row is still here and still red — it now speaks about the app instead of about a file.
    expect(error).toBeTruthy()
    expect(error?.textContent).toContain(ERROR_FALLBACK_MESSAGE)
    expect(error?.textContent).not.toContain('Type error in app/page.tsx')
    expect(error?.textContent).not.toContain('app/page.tsx(12,5)')
    expect(container.querySelector('[data-kind="escalation"]')?.textContent).toContain('gave up')
    expect(container.querySelector('[data-kind="quota_exceeded"]')?.textContent).toMatch(/daily limit/i)
  })

  it('an escalation stops carrying the compiler-authored last-error title', () => {
    // The same developer line the error arm dropped was still rendering one branch over, so
    // stripping only the error arm would have moved the leak rather than closed it.
    const envelopes: FeedEnvelope[] = [
      {
        type: 'escalation',
        seq: 1,
        reason: 'max_retries',
        detail: 'We could not get that working.',
        last_error: {
          source: 'tsc',
          title: "app/page.tsx(12,5): error TS2307: Cannot find module '@/components/X'",
          cleaned_stack: 'app/page.tsx(12,5): error TS2307',
        },
      },
    ]
    const { container } = draw({ envelopes })
    const row = container.querySelector('[data-kind="escalation"]')
    expect(row).toBeTruthy()
    expect(row?.textContent).toContain('We could not get that working.')
    expect(row?.textContent).not.toContain('app/page.tsx')
    expect(row?.textContent).not.toContain('TS2307')
    // An escalation is an error status like any other, so it carries a next step too.
    expect(row?.textContent).toContain(ERROR_FALLBACK_ACTION)
  })
})

describe('F3/U3: friendly labels only, hidden steps dropped, zero raw shell', () => {
  it('a hidden step never appears live, and is dropped from the post-build history too', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'run_command', label: "Inspected the app's files", state: 'ok', hidden: true },
      { type: 'step', seq: 2, name: 'edit', label: "Building your app's main page", state: 'ok' },
    ]
    // Live: the newest VISIBLE step has already resolved 'ok' with nothing in flight, so the
    // row degrades to a neutral "Working…" placeholder (finding 5) rather than presenting a
    // stale finished tick as current — but the hidden step still never surfaces either way.
    const live = draw({ envelopes })
    expect(live.container.textContent).toContain('Working…')
    expect(live.container.textContent).not.toContain("Inspected the app's files")
    live.unmount()

    // Post-build history: the hidden step is dropped, not merely deprioritized.
    const { container } = draw({ envelopes, status: 'ended' })
    fireEvent.click(container.querySelector('button[aria-expanded]') as HTMLButtonElement)
    const steps = container.querySelectorAll('[data-kind="step"]')
    expect(steps).toHaveLength(1)
    expect(container.textContent).toContain("Building your app's main page")
    expect(container.textContent).not.toContain("Inspected the app's files")
  })

  it('the live current-step row carries the friendly label and NO raw shell/argv', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'run_command', label: 'Setting up the tools your app needs', state: 'ok' },
      { type: 'step', seq: 2, name: 'run_command', label: 'Working on your app', state: 'failed' },
    ]
    draw({ envelopes })
    const status = screen.getByRole('status', { name: /build activity/i })
    expect(status.textContent).toContain('Working on your app')
    for (const raw of ['$ ', 'npx', 'bash -c', 'ls -la', 'npm install']) {
      expect(status.textContent).not.toContain(raw)
    }
  })

  it('the multi-step feed collapses under a real <button aria-expanded> header (Mode B)', () => {
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'edit', label: 'Building your app’s main page', state: 'ok' },
      { type: 'step', seq: 2, name: 'run_command', label: 'Setting up the tools your app needs', state: 'ok' },
    ]
    const { container } = draw({ envelopes, status: 'ended' })
    expect(container.querySelector('button[aria-expanded]')).toBeTruthy()
  })
})

describe('a self-healed failure reads as a retry, once, whole (U4)', () => {
  // The MODEL's half is present on both fixtures on purpose: the point of the split is that it
  // rides along on the envelope and is simply never rendered.
  const recovering: FeedEnvelope = {
    type: 'error',
    seq: 4,
    source: 'tsc',
    title: 'Type error in app/page.tsx — the About page imports a component that does not…',
    cleaned_stack: 'Type error in app/page.tsx — the About page imports a component that does not…',
    recovering: true,
    user_message: "Part of your app didn't fit together.",
    user_action: "Nothing to do right now — we're working on it. If it keeps happening, try asking for something simpler.",
  }
  const terminalError: FeedEnvelope = {
    type: 'error',
    seq: 5,
    source: 'server',
    title: 'The dev server crashed',
    cleaned_stack: 'Error: boom\n  at Server.listen',
    user_message: 'Your app ran into a problem while it was starting up.',
    user_action: "Nothing to do right now — we're working on it. If it keeps happening, try asking for something simpler.",
  }

  it('a diagnostic renders the retry framing while a genuine error stays red (mutation: restore the diagnostic→error collapse and this goes red)', () => {
    const { container } = draw({ envelopes: [recovering, terminalError] })
    const retry = container.querySelector('[data-kind="retry"]')
    const error = container.querySelector('[data-kind="error"]')
    expect(retry?.textContent).toContain('trying another way')
    // The two rows still tell DIFFERENT stories — they just tell them in product language now.
    expect(retry?.textContent).toContain("Part of your app didn't fit together.")
    expect(retry?.textContent).not.toContain('starting up')
    expect(error?.textContent).toContain('Your app ran into a problem while it was starting up.')
    expect(error?.textContent).not.toContain('The dev server crashed')
  })

  it('the retry framing renders the product sentence, never the compiler title', () => {
    const { container } = draw({ envelopes: [recovering] })
    const retry = container.querySelector('[data-kind="retry"]')
    // LIVENESS: the row rendered. A row that threw also has no <pre> and no title.
    expect(retry).toBeTruthy()
    expect(retry?.textContent).toContain('trying another way')
    expect(retry?.textContent).toContain("Part of your app didn't fit together.")
    expect(retry?.textContent).toContain('try asking for something simpler')

    expect(retry?.textContent).not.toContain('Type error in app/page.tsx')
    expect(retry?.querySelector('pre')).toBeNull()
  })

  it('FLIPPED: a terminal error no longer keeps its <pre> stack either', () => {
    // This test used to pin the <pre> as CORRECT for the terminal arm ("only diagnostics drop
    // the monospace block"). The stack is the single most developer-looking thing a citizen
    // reads, and the terminal arm is where they are most likely to read it.
    const { container } = draw({ envelopes: [terminalError] })
    const error = container.querySelector('[data-kind="error"]')
    // LIVENESS before absence — a crashed row has no <pre> either.
    expect(error).toBeTruthy()
    expect(error?.textContent).toContain('Your app ran into a problem while it was starting up.')

    expect(container.querySelector('[data-kind="error"] pre')).toBeNull()
    expect(container.querySelector('pre')).toBeNull()
    expect(error?.textContent).not.toContain('The dev server crashed')
    expect(error?.textContent).not.toContain('at Server.listen')
  })

  it('a diagnostic-only narrative still counts as narrative while the build runs (the 2b00ce3 chrome gate)', () => {
    expect(hasBuildNarrative('building', [recovering])).toBe(true)
    const { container } = draw({ envelopes: [recovering], status: 'building' })
    expect(container.querySelector('[data-kind="retry"]')).toBeTruthy()
  })

  it('at the terminal the retry framing disappears — the outcome message owns the ending', () => {
    for (const status of ['failed', 'ended'] as const) {
      const { container, unmount } = draw({ envelopes: [recovering], status })
      expect(container.querySelector('[data-kind="retry"]')).toBeNull()
      expect(container.querySelector('[data-kind="error"]')).toBeNull()
      unmount()
    }
    // …and the chrome gate agrees there is nothing left to wrap (no empty grey bubble).
    expect(hasBuildNarrative('failed', [recovering])).toBe(false)
    expect(hasBuildNarrative('ended', [recovering])).toBe(false)
  })

  it('a genuine red error DOES survive the terminal — only the retry framing is live-only', () => {
    const { container } = draw({ envelopes: [terminalError], status: 'failed' })
    expect(container.querySelector('[data-kind="error"]')).toBeTruthy()
    expect(hasBuildNarrative('failed', [terminalError])).toBe(true)
  })
})

/**
 * U24 — the at-limit experience.
 *
 * The old refusal told the citizen to click Save and secured nothing, and the client's own copy
 * ("You've hit your daily limit of 1,000,000 tokens") could never say the two things that
 * actually matter: whether their app was secured on the way out, and who they can write to. Both
 * are facts only the server holds, so the sentence arrives as a prop and this component's job is
 * to render it — including turning the configured address into something clickable.
 */
describe('U24: what a citizen sees when their budget is gone', () => {
  const RESETS_AT = '2026-07-15T18:30:00.000Z'
  const AT_LIMIT =
    "You've used up your building budget for today, we've kept a copy of your app, so nothing " +
    'you did today is lost. You can carry on after midnight, and if you need more before then, ' +
    'email citizen-developer-support@bial.com.'
  const quota = (seq = 3): QuotaExceededEvent => ({
    type: 'quota_exceeded',
    seq,
    limit: 1_000_000,
    used: 1_000_001,
    resets_at: RESETS_AT,
  })

  it("renders the server's sentence and turns the support address into a real mailto: link", () => {
    // WITHOUT THIS the citizen reads an address and has to retype it into their mail client — at
    // the exact moment they are already blocked and least patient. Deleting this test would let
    // the row silently fall back to printing the sentence as inert text.
    //
    // Mutation check: render `{atLimitText}` directly instead of `withMailtoLinks(atLimitText)`
    // and this goes red on the anchor lookup.
    const { container } = draw({ envelopes: [quota()], atLimitText: AT_LIMIT, status: 'failed' })
    const row = container.querySelector('[data-kind="quota_exceeded"]')
    expect(row?.textContent).toContain('used up your building budget')
    expect(row?.textContent).toContain('after midnight')

    const link = row?.querySelector('a')
    expect(link?.getAttribute('href')).toBe('mailto:citizen-developer-support@bial.com')
    expect(link?.textContent).toBe('citizen-developer-support@bial.com')
    // The prose either side of the address survives — a linkifier that returns ONLY the matches
    // would pass every assertion above and lose the entire message.
    expect(row?.textContent).toContain('if you need more before then, email')
  })

  it('carries the reset time onto the row, and degrades rather than printing "Invalid Date"', () => {
    // `resets_at` is a wire value and the suite already feeds this component the string 'x'. A
    // naive `new Date(iso).toLocaleTimeString()` renders the literal words "Invalid Date" into a
    // citizen's banner, which is worse than a slightly vaguer sentence.
    //
    // Mutation check: drop the `Number.isNaN` guard in `formatResetTime` and this goes red.
    const { container } = draw({ envelopes: [quota()], status: 'failed' })
    const row = container.querySelector('[data-kind="quota_exceeded"]')
    expect(row?.getAttribute('data-resets-at')).toBe(RESETS_AT)
    expect(row?.getAttribute('title')).toBe(atLimitSendState([quota()])?.title)

    expect(formatResetTime('x')).toBeNull()
    expect(formatResetTime(RESETS_AT)).not.toBeNull()
    expect(formatResetTime(RESETS_AT)).not.toContain('Invalid')
  })

  it('falls back to the client-side quota copy when the server sent no sentence', () => {
    // Every surface that has not been wired to pass `atLimitText` must still say SOMETHING. A
    // prop-or-nothing render would leave a citizen looking at an empty amber box.
    const { container } = draw({ envelopes: [quota()], status: 'failed' })
    const row = container.querySelector('[data-kind="quota_exceeded"]')
    expect(row?.textContent).toMatch(/daily limit/i)
    expect(row?.querySelector('a')).toBeNull()
  })

  it('the SEND control is disabled and its title names when sending works again', () => {
    // THE COMPOSER STAYS ENABLED — this describes the send control only. A citizen who is
    // refused mid-thought has usually just typed something worth keeping, and disabling the
    // textarea takes their draft hostage until midnight (and, per KTD-3, blurs focus to the
    // document body). This is the state the composer applies to Send and to Send alone.
    //
    // Mutation check: return `null` unconditionally from `atLimitSendState` and this goes red.
    const state = atLimitSendState([quota()])
    expect(state?.disabled).toBe(true)
    expect(state?.title).toMatch(/^You can send again after /)
    expect(state?.title).toContain(formatResetTime(RESETS_AT) as string)
  })

  it('says nothing about sending while the citizen still has budget', () => {
    // The state must be ABSENT rather than a disabled-false object: a composer that spreads it
    // unconditionally would otherwise disable Send on every ordinary turn.
    expect(atLimitSendState([])).toBeNull()
    expect(
      atLimitSendState([{ type: 'step', seq: 1, name: 's', label: 'x', state: 'ok' }]),
    ).toBeNull()
  })

  it('takes the NEWEST reset time by seq, not the last envelope that happened to arrive', () => {
    // A reconnect replays the stream, so envelopes arrive out of order. Reading the last ARRIVED
    // envelope hands the citizen a stale reset time from a replayed frame — and "when can I send
    // again" is the only question this row exists to answer.
    //
    // The two instants differ in TIME OF DAY, not merely in date. `formatResetTime` renders a
    // clock time, so two different DATES at the same hour render identically and the assertion
    // would pass against either implementation — which is exactly what an earlier version of
    // this test did.
    //
    // Mutation check: pick the last array element instead of sorting by seq and this goes red.
    const stale: QuotaExceededEvent = { ...quota(9), resets_at: '2026-07-15T06:15:00.000Z' }
    const newest: QuotaExceededEvent = { ...quota(12), resets_at: '2026-07-15T18:30:00.000Z' }

    // Deliberately out of array order: newest first, stale last.
    const state = atLimitSendState([newest, stale])

    expect(state?.title).toContain(formatResetTime(newest.resets_at) as string)
    expect(state?.title).not.toContain(formatResetTime(stale.resets_at) as string)
  })

  it('linkifies every address in the sentence and never swallows a trailing full stop', () => {
    // A `mailto:` that carries the sentence's final "." into the mailbox name bounces, and the
    // citizen has no way to tell why.
    const nodes = withMailtoLinks('Write to a@b.com or c@d.co.uk.')
    const hrefs = nodes
      .filter((n): n is React.ReactElement<{ href: string }> => typeof n !== 'string')
      .map((n) => n.props.href)
    expect(hrefs).toEqual(['mailto:a@b.com', 'mailto:c@d.co.uk'])
    expect(nodes.filter((n) => typeof n === 'string').join('')).toBe('Write to  or .')
  })

  it('leaves a sentence with no address exactly as it was', () => {
    expect(withMailtoLinks('Nothing to link here.')).toEqual(['Nothing to link here.'])
  })
})

/**
 * U16 — the platform's own surfaces speak product language.
 *
 * `BuildError` is deliberately dual-purpose: `title` is BUILT to be the compiler's own first
 * meaningful line, because that is what the repair run needs. Rendering it was the defect. What
 * this block pins is the split — the model's half still rides on the envelope, and none of it
 * reaches the screen — plus the rule that replaced it: every rendered error status carries a
 * plain sentence AND a next action.
 */
describe('U16: every error status is a sentence plus a next action', () => {
  const ALL_SOURCES: ErrorSource[] = ['tsc', 'next_build', 'server', 'client']

  it('a diagnostic renders NO <pre> and NO compiler title — with the row proven alive', () => {
    // ASSERT-ABSENCE IS HALF A TEST. A row that threw inside its own render has no <pre> and no
    // title either, and would pass every negative below on its own. So the row is located and
    // its product sentence read back FIRST; only then does the absence mean anything.
    const envelopes: FeedEnvelope[] = [
      {
        type: 'error',
        seq: 1,
        source: 'tsc',
        title: "app/page.tsx(12,5): error TS2307: Cannot find module '@/components/VisitorTable'",
        cleaned_stack:
          "app/page.tsx(12,5): error TS2307: Cannot find module '@/components/VisitorTable'\n" +
          '  at Object.<anonymous> (/workspace/app/node_modules/next/dist/build/index.js:1:9)',
        user_message: "Part of your app didn't fit together.",
        user_action: 'Try describing what you want again, or ask for something simpler.',
      },
    ]
    const { container } = draw({ envelopes })
    const row = container.querySelector('[data-kind="error"]')
    expect(row).toBeTruthy()
    expect(row?.querySelector('[data-part="message"]')?.textContent).toBe(
      "Part of your app didn't fit together.",
    )

    expect(container.querySelector('pre')).toBeNull()
    expect(container.textContent).not.toContain('app/page.tsx')
    expect(container.textContent).not.toContain('TS2307')
    expect(container.textContent).not.toContain('node_modules')
    expect(container.textContent).not.toContain('workspace')
  })

  it('an error with no product-language equivalent renders BOTH halves of the committed fallback', () => {
    // The legacy C7 feed carries neither field. Asserting only the absence of the stack would
    // pass against a row that renders an empty box — which is a quieter dead end, not a fix.
    const envelopes: FeedEnvelope[] = [
      { type: 'error', seq: 1, source: 'server', title: 'ECONNREFUSED 127.0.0.1:3000', cleaned_stack: 'at Socket.emit' },
    ]
    const { container } = draw({ envelopes })
    const row = container.querySelector('[data-kind="error"]')
    expect(row).toBeTruthy()
    expect(row?.textContent).toContain(ERROR_FALLBACK_MESSAGE)
    expect(row?.textContent).toContain(ERROR_FALLBACK_ACTION)
    // Both halves are separately locatable, so a render that concatenated one into the other
    // (or dropped the action) cannot pass on the combined string alone.
    expect(row?.querySelector('[data-part="message"]')?.textContent).toBe(ERROR_FALLBACK_MESSAGE)
    expect(row?.querySelector('[data-part="action"]')?.textContent).toBe(ERROR_FALLBACK_ACTION)
    expect(row?.textContent).not.toContain('ECONNREFUSED')
  })

  it('TABLE-DRIVEN over every ErrorSource, including client: each renders a non-empty action', () => {
    // A per-source table is exactly the kind of thing that grows a member with no row, and the
    // failure is silent. Both arms are covered — a class that only renders its action while
    // recovering still dead-ends the citizen at the terminal, which is when they read it.
    for (const source of ALL_SOURCES) {
      for (const recovering of [true, false]) {
        const env: FeedEnvelope = {
          type: 'error',
          seq: 1,
          source,
          title: 'app/page.tsx(1,1): error TS1005',
          cleaned_stack: 'app/page.tsx(1,1): error TS1005',
          ...(recovering ? { recovering: true } : {}),
        }
        const { container, unmount } = draw({ envelopes: [env], status: 'building' })
        const row = container.querySelector(recovering ? '[data-kind="retry"]' : '[data-kind="error"]')
        expect(row, `${source} / recovering=${recovering}`).toBeTruthy()
        expect(row?.getAttribute('data-source')).toBe(source)
        const action = row?.querySelector('[data-part="action"]')?.textContent ?? ''
        expect(action.trim().length, `${source} / recovering=${recovering}`).toBeGreaterThan(0)
        expect(row?.textContent).not.toContain('app/page.tsx')
        unmount()
      }
    }
  })

  it('the pair survives the DiagnosticFrame → ErrorEvent mapping (the field-drops-here guard)', () => {
    // THIS MAPPING IS WHERE A NEW FIELD SILENTLY DISAPPEARS. `ErrorEvent`'s citizen-facing
    // fields are optional, so a `narrativeEnvelopes` that simply forgot to copy them would
    // typecheck, render, and quietly serve every citizen the generic fallback forever.
    const envelopes = narrativeEnvelopes({
      steps: {},
      diagnostics: [
        {
          source: 'client',
          title: 'TypeError: undefined is not a function',
          cleanedStack: '',
          userMessage: 'The app opened but ran into a problem in the browser.',
          userAction: "Nothing to do right now — we're working on it.",
        },
      ],
      quota: null,
      workspace: null,
      preview: { url: null, state: null },
    })

    const error = envelopes.find((env) => env.type === 'error')
    expect(error).toBeTruthy()
    expect(error?.type === 'error' && error.user_message).toBe(
      'The app opened but ran into a problem in the browser.',
    )
    expect(error?.type === 'error' && error.user_action).toBe(
      "Nothing to do right now — we're working on it.",
    )

    // …and it survives all the way to the screen, which is the only place it matters.
    const { container } = draw({ envelopes, status: 'building' })
    const row = container.querySelector('[data-kind="retry"]')
    expect(row).toBeTruthy()
    expect(row?.querySelector('[data-part="message"]')?.textContent).toBe(
      'The app opened but ran into a problem in the browser.',
    )
    expect(row?.querySelector('[data-part="action"]')?.textContent).toBe(
      "Nothing to do right now — we're working on it.",
    )
  })

  it('AE13: nothing in a whole build — steps, logs, errors, escalation — is addressed to a developer', () => {
    // THE COMPLETE RENDERED SET, which is the part that makes this AE13 rather than a narration
    // check: the platform's own error surfaces are scanned alongside the agent's steps, because
    // those surfaces were the worst offenders. U15 and U18 assert against this same list.
    const DEVELOPER_VOCABULARY = [
      '/', '.tsx', '.ts', '.css', '.json', 'app/', 'components/', 'workspace', 'node_modules',
      'npm', 'npx', 'pnpm', 'yarn', 'bash', 'tsc', 'eslint', 'drizzle-kit', '$ ',
      'next.js', 'nextjs', 'react', 'tailwind', 'shadcn', 'drizzle', 'typescript', 'webpack',
      'stack trace', 'stderr', 'stdout', 'traceback', 'compiler', 'exit code', 'console',
    ]
    const envelopes: FeedEnvelope[] = [
      { type: 'step', seq: 1, name: 'read_file', label: "Looking at your app's main page", state: 'ok', hidden: true },
      { type: 'step', seq: 2, name: 'write_file', label: "Building your app's main page", state: 'ok' },
      { type: 'step', seq: 3, name: 'run_command', label: 'Setting up the tools your app needs', state: 'started' },
      { type: 'log', seq: 4, source: 'exec', stream: 'stdout', text: 'npm WARN deprecated glob@7' },
      { type: 'log', seq: 5, source: 'dev', stream: 'stderr', text: 'app/page.tsx(12,5): error TS2307' },
      {
        type: 'error',
        seq: 6,
        source: 'tsc',
        title: "app/page.tsx(12,5): error TS2307: Cannot find module '@/components/VisitorTable'",
        cleaned_stack: 'at Object.<anonymous> (/workspace/app/node_modules/next/dist/x.js:1:9)',
        user_message: "Part of your app didn't fit together.",
        user_action: "Nothing to do right now — we're working on it.",
        recovering: true,
      },
      {
        type: 'error',
        seq: 7,
        source: 'client',
        title: 'The app opened but ran into a problem in the browser.',
        cleaned_stack: '',
        user_message: 'The app opened but ran into a problem in the browser.',
        user_action: 'Try describing what you want again, or ask for something simpler.',
      },
      {
        type: 'escalation',
        seq: 8,
        reason: 'max_retries',
        detail: 'We could not get that working.',
        last_error: { source: 'tsc', title: 'app/page.tsx(12,5): error TS2307', cleaned_stack: 'x' },
      },
      { type: 'quota_exceeded', seq: 9, limit: 1000000, used: 1000001, resets_at: '2026-07-15T18:30:00.000Z' },
    ]
    const { container } = draw({ envelopes, status: 'building', startedAt: Date.now() - 5000 })

    // LIVENESS: a build that rendered nothing would satisfy every absence below.
    const rendered = container.textContent ?? ''
    expect(container.querySelector('[data-testid="build-progress"]')).toBeTruthy()
    expect(rendered).toContain('Setting up the tools your app needs')
    expect(rendered).toContain("Part of your app didn't fit together.")
    expect(rendered).toContain('The app opened but ran into a problem in the browser.')
    expect(rendered).toContain('We could not get that working.')
    expect(rendered.length).toBeGreaterThan(200)

    const lowered = rendered.toLowerCase()
    const hits = DEVELOPER_VOCABULARY.filter((word) => lowered.includes(word))
    expect(hits, `the rendered build leaks ${hits.join(', ')}`).toEqual([])

    // …AND THE FINISHED BUILD, expanded. The live view shows one step at a time, so a leak in
    // an earlier label would simply not be on screen yet — the step history is where a citizen
    // reads the whole run back, and it has to hold the same rule.
    const ended = draw({ envelopes, status: 'ended' })
    fireEvent.click(ended.container.querySelector('button[aria-expanded]') as HTMLButtonElement)
    const history = ended.container.textContent ?? ''
    expect(history).toContain("Building your app's main page")
    expect(history).toContain('Setting up the tools your app needs')
    const historyHits = DEVELOPER_VOCABULARY.filter((word) => history.toLowerCase().includes(word))
    expect(historyHits, `the step history leaks ${historyHits.join(', ')}`).toEqual([])
  })
})
