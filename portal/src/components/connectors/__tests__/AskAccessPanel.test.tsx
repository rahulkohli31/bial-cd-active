/**
 * The ask panel — the informed-consent copy, the word rule, and the two ways out.
 *
 * THE CONSENT ASSERTIONS ARE DELIBERATELY LITERAL — AND THEY ARE ABOUT THE RENDERER, NOT THE
 * COPY. These lines state what an approval does and does not give a person; R1 makes copy that
 * states a capability or a consequence binding in substance, and the failure it exists to prevent
 * is a well-meaning summary. The panel is now a renderer of what the wire sent, so the sentences
 * are held byte-exact against the boards where they live — `backend/tests/db/
 * test_connector_models.py` — and what is proved HERE is that every line the server sends reaches
 * the screen whole, in order, with its lead in bold markup and its body after it, and that the
 * panel adds none of its own.
 *
 * THE FIXTURE'S COPY IS INVENTED ON PURPOSE, like its connector name. If a single sentence of the
 * real connector's panel were compiled into the component, this suite would still be green — so
 * the suite supplies copy that exists nowhere else, and a component that ignored it goes red.
 *
 * THE PANEL DOES NOT CALL THE API. `onSubmit` is the dialog's, so these tests can watch the exact
 * boundary that matters: whether a remark the form already knows is too short ever reaches it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

import AskAccessPanel from '../AskAccessPanel'
import type { ConnectorEntry } from '../../../utils/connectorApi'
import { Dialog, DialogContent } from '../../ui/dialog'

/** The three ticked lines this suite's connector sends, and the whole of what may reach the box. */
const CONSENT = [
  ['Read-only.', 'Nothing you build can change ORBIT data.'],
  [
    'One dataset.',
    'The Flight Fact Report — flight schedules, gates, stands and status. Nothing else in ORBIT.',
  ],
  [
    'Every project you own.',
    'Including ones you have not made yet. You switch it on per project, and pick the days each one reads.',
  ],
] as const

const entry: ConnectorEntry = {
  key: 'orbit',
  displayName: 'ORBIT',
  subtitle: 'Airport operations',
  askSubtitle:
    'ORBIT is BIAL’s airport operations data. An administrator decides who may read it — you are asking once, for yourself.',
  consentLinesRequester: CONSENT.map(([lead, body]) => ({ lead, body })),
  state: 'neverAsked',
  askedAt: null,
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  decidedAt: null,
  decidedByName: null,
  decisionRemarks: null,
}

const A_GOOD_REASON = 'I build the departures board the duty managers use every shift'

interface Handlers {
  /** Defaults to the suite's connector. Overridden by the one test that supplies a second. */
  entry?: ConnectorEntry
  busy?: boolean
  onBack?: () => void
  onClose?: () => void
  onSubmit?: (remarks: string) => Promise<void>
}

/**
 * Mounted inside a real `Dialog`, because that is where it lives: `DialogTitle` is a Radix
 * primitive and reads its id off the dialog's context, so a bare render would throw.
 */
const mount = ({
  entry: shown = entry,
  busy = false,
  onBack,
  onClose,
  onSubmit,
}: Handlers = {}): void => {
  render(
    <Dialog open>
      <DialogContent hideClose aria-describedby={undefined}>
        <AskAccessPanel
          entry={shown}
          busy={busy}
          onBack={onBack ?? (() => {})}
          onClose={onClose ?? (() => {})}
          onSubmit={onSubmit ?? (() => Promise.resolve())}
        />
      </DialogContent>
    </Dialog>,
  )
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => cleanup())

describe('the board copy this panel ships whole', () => {
  it('names the connector from the wire, in the title and the subtitle', () => {
    mount()
    expect(screen.getByText('Ask for access to ORBIT')).toBeTruthy()
    expect(
      screen.getByText(
        'ORBIT is BIAL’s airport operations data. An administrator decides who may read it — you are asking once, for yourself.',
      ),
    ).toBeTruthy()
  })

  it('labels the field REMARKS, marks it REQUIRED, and carries the board’s helper verbatim', () => {
    mount()
    expect(screen.getByText('REMARKS')).toBeTruthy()
    expect(screen.getByText('REQUIRED')).toBeTruthy()
    expect(
      screen.getByText(
        'Say what you need the data for. An administrator reads this and answers it, so it is the whole of what they have to go on.',
      ),
    ).toBeTruthy()
  })

  it('renders ALL THREE consent lines, each with its bold lead and its body — a summary goes red', () => {
    mount()
    expect(screen.getByText('WHAT AN APPROVAL GIVES YOU')).toBeTruthy()

    for (const [lead, body] of CONSENT) {
      const bold = screen.getByText(lead)
      // The lead is genuinely BOLD MARKUP, not a sentence that happens to start the paragraph:
      // the boards set it in `font-weight:700` against the body's grey, and a joined string
      // would force a renderer to guess the split.
      expect(bold.tagName).toBe('B')
      expect(bold.parentElement?.textContent).toBe(`${lead} ${body}`)
    }
  })

  it('THE R18 MUTANT: a second connector renders ITS copy, and the panel adds none of its own', () => {
    // Everything this panel says about a system comes off the entry, so "add a second connector"
    // is a registry entry and nothing else. Compile one sentence of one connector's panel into
    // the component and this goes red — the fixture below shares no wording with the first.
    mount({
      entry: {
        ...entry,
        key: 'ledger',
        displayName: 'LEDGER',
        askSubtitle: 'LEDGER is the finance system of record. Ask an administrator to open it.',
        consentLinesRequester: [
          { lead: 'Read-only.', body: 'Nothing you build can change LEDGER data.' },
        ],
      },
    })

    expect(screen.getByText('Ask for access to LEDGER')).toBeTruthy()
    expect(
      screen.getByText('LEDGER is the finance system of record. Ask an administrator to open it.'),
    ).toBeTruthy()
    // ONE line sent, one line drawn. A hard-coded extra would pass every assertion above it.
    const box = screen.getByText('WHAT AN APPROVAL GIVES YOU').parentElement
    expect(box?.querySelectorAll('b').length).toBe(1)
    // Paired with the three positives above, so a crashed mount cannot satisfy this.
    expect(screen.queryByText('One dataset.')).toBeNull()
  })
})

describe('the form agrees with the server about what is required', () => {
  it('states the word rule BEFORE it is tripped, and counts live', () => {
    mount()
    // Visible with an empty box — a 422 must never be the first news of the bound.
    expect(screen.getByText('Between 5 and 50 words.')).toBeTruthy()
    expect(screen.getByText('0/50 words')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: 'one two three' },
    })
    expect(screen.getByText('3/50 words')).toBeTruthy()
  })

  it('does not post a remark the form already knows is too short', async () => {
    const onSubmit = vi.fn(() => Promise.resolve())
    mount({ onSubmit })

    const ask = screen.getByRole('button', { name: 'Ask an administrator' })
    // Nothing written at all.
    fireEvent.click(ask)
    // Under the floor.
    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: 'need it' },
    })
    fireEvent.click(ask)
    // Over the ceiling.
    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: 'word '.repeat(51) },
    })
    fireEvent.click(ask)

    expect(onSubmit).not.toHaveBeenCalled()
    // Liveness: the control is on screen and clickable throughout — the three no-ops above are
    // the guard refusing, not a missing button.
    expect(ask.getAttribute('aria-disabled')).toBe('true')

    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: A_GOOD_REASON },
    })
    expect(ask.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(ask)
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(A_GOOD_REASON))
  })

  it('says so with `aria-disabled` and never the native `disabled`, so focus is not yanked mid-request', () => {
    // THE REASON IS TYPED FIRST, AND THAT IS THE WHOLE POINT OF THIS TEST. `canSubmit` is
    // `remarksValid && !busy`; with the box empty the word rule alone already disables the
    // button, so this test read `true` even with the `&& !busy` arm deleted — it named the
    // in-flight guard and proved the word rule instead. With a valid reason on the form, `busy`
    // is the only thing left that can disable it, so deleting that arm turns this red.
    // `dialog.tsx`'s own docblock: a disabled control throws focus to `<body>`, which is the
    // strand its focus backstop exists to catch. The attribute says so; the handler does so.
    const onSubmit = vi.fn(() => Promise.resolve())
    mount({ busy: true, onSubmit })
    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: A_GOOD_REASON },
    })

    const ask = screen.getByRole('button', { name: 'Ask an administrator' })
    expect(ask.getAttribute('aria-disabled')).toBe('true')
    expect(ask.hasAttribute('disabled')).toBe(false)

    // And the handler does so, rather than merely looking so: `submit` returns on `!canSubmit`
    // before it awaits anything, so a click that reaches it is refused synchronously. The test
    // above is this one's positive control — the same reason, `busy: false`, and the click lands.
    fireEvent.click(ask)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('shows the failure the submit rejected with, in its own words', async () => {
    mount({ onSubmit: () => Promise.reject(new Error('An administrator has already answered this request.')) })

    fireEvent.change(screen.getByLabelText('Why you need access to ORBIT'), {
      target: { value: A_GOOD_REASON },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Ask an administrator' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('An administrator has already answered this request.')
  })
})

describe('the two ways out', () => {
  it('the back chevron and Cancel both return to the list; the X closes the dialog', () => {
    const onBack = vi.fn()
    const onClose = vi.fn()
    mount({ onBack, onClose })

    fireEvent.click(screen.getByRole('button', { name: 'Back to integrations' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onBack).toHaveBeenCalledTimes(2)
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onBack).toHaveBeenCalledTimes(2)
  })
})
