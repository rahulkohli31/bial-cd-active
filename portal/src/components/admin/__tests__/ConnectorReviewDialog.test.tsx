/**
 * The administrator's decision dialog — the two writes, and the shape the `AdminReview` board's
 * largest departure took.
 *
 * MOUNTED THROUGH THE PANEL, NOT ON ITS OWN. Half of what this unit promises is about what the
 * QUEUE does afterwards — the decided row leaves `WAITING ON YOU`, a lost race closes into a
 * reloaded queue rather than a stale row — and a dialog rendered by itself with a stub callback
 * would assert that the callback was called, which is not the same claim. The one thing that
 * cannot be reached that way is the exact HTTP shape, so that is tested against the REAL module
 * through `vi.importActual`, with `fetchImpl` handed in.
 *
 * THE FIXTURES NAME NO REAL CONNECTOR (R18). `ORBIT` and its consent copy are invented, so a
 * component that had a connector's sentences compiled into it could not pass — a suite asserting
 * the real registry's words would be green either way.
 *
 * LIVENESS BESIDE EVERY ABSENCE. Five tests here are about something NOT happening — no textarea
 * at rest, no call from a disabled control, no call from a refused form, nothing sent on Cancel —
 * and a component that crashed on mount satisfies all five. Each is paired with a positive
 * assertion for that reason.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'

import type { ConnectorRequestRow } from '../../../utils/adminConnectorApi'
import { ApiError } from '../../../utils/apiError'
import { countWords } from '../../../utils/words'

const h = vi.hoisted(() => ({
  listConnectorRequests: vi.fn(),
  approveConnectorRequest: vi.fn(),
  declineConnectorRequest: vi.fn(),
  getStoredUser: vi.fn(),
}))

// PARTIAL, so `vi.importActual` below is not the only way back to the real module and the row
// parser this file's fixtures are typed against stays the shipped one.
vi.mock('../../../utils/adminConnectorApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/adminConnectorApi')>()
  return {
    ...actual,
    listConnectorRequests: h.listConnectorRequests,
    approveConnectorRequest: h.approveConnectorRequest,
    declineConnectorRequest: h.declineConnectorRequest,
  }
})
// PARTIAL TOO, and for a reason the panel's own suite does not have: `authFetch` reaches back
// into this module for `getAccessToken`, `refreshAccessToken` AND `getCsrfToken`, so the two wire
// tests below — which run the REAL request path — throw at the destructure under a full mock.
// Only the cached profile is stubbed.
vi.mock('../../../utils/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../utils/auth')>()
  return { ...actual, getStoredUser: h.getStoredUser }
})

import IntegrationsPanel from '../IntegrationsPanel'

const ME = '0199a1f0-0000-7000-8000-00000000000a'

const REMARK =
  'I build the stand and turnaround boards for ground ops. All of them need on-block and off-block times.'

/** Three `{lead, body}` pairs, never pre-joined — the shape the board's bold lead needs. */
const CONSENT = [
  { lead: 'Read access to the movements feed.', body: 'and nothing else in this system.' },
  { lead: 'Every project they own.', body: 'including ones they have not made yet.' },
  { lead: 'Up to 30 days of history while they build.', body: 'each project picks its own range.' },
]

/** Midday UTC: every timezone this product is read in still calls it 4 September. */
const PRIYA: ConnectorRequestRow = {
  id: 'req-priya',
  userId: 'user-priya',
  displayName: 'Priya Nair',
  email: 'priya.nair@bial.aero',
  connectorKey: 'orbit',
  connectorDisplayName: 'ORBIT',
  consentLinesApprover: CONSENT,
  requesterRemarks: REMARK,
  askedAt: '2026-09-04T12:00:00.000Z',
  status: 'pending',
  decidedAt: null,
  decidedById: null,
  decidedByName: null,
  decisionRemarks: null,
  usingItIn: null,
}

const PRIYA_APPROVED: ConnectorRequestRow = {
  ...PRIYA,
  status: 'approved',
  decidedAt: '2026-09-06T12:00:00.000Z',
  decidedById: ME,
  decidedByName: 'Anita Rao',
  usingItIn: 0,
}

/** The mock queue, mutable so a decision can actually move a row between the two tables. */
let waitingRows: ConnectorRequestRow[] = []
let decidedRows: ConnectorRequestRow[] = []
const onToast = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  waitingRows = [PRIYA]
  decidedRows = []
  h.getStoredUser.mockReturnValue({ id: ME, isAdmin: true })
  h.listConnectorRequests.mockImplementation(async (state: string) => ({
    requests: state === 'waiting' ? waitingRows : decidedRows,
    truncated: false,
  }))
})
afterEach(() => cleanup())

/** Open the console's Integrations tab and press `Review` on Priya's row. */
const openReview = async (): Promise<HTMLElement> => {
  render(<IntegrationsPanel onToast={onToast} />)
  await screen.findByTestId('queue-table-waiting')
  fireEvent.click(screen.getByTestId('review-req-priya'))
  return screen.getByTestId('connector-review-dialog')
}

/** Reveal the decline box and type into it. */
const typeDecline = async (value: string): Promise<HTMLElement> => {
  const dialog = await openReview()
  fireEvent.click(screen.getByTestId('review-decline'))
  fireEvent.change(screen.getByTestId('decline-remarks'), { target: { value } })
  return dialog
}

describe('what the dialog says', () => {
  it('★ renders the person, their remarks and all three approval lines with bold leads', async () => {
    const dialog = within(await openReview())

    expect(dialog.getByText('Give Priya Nair access to ORBIT?')).toBeTruthy()
    // ONE INLINE LINE: name and WORK EMAIL, not the queue's two-line stack and not the board's
    // `department`, which exists nowhere in this product.
    expect(dialog.getByText('Priya Nair — priya.nair@bial.aero')).toBeTruthy()
    // Quoted, in full, as plain text.
    expect(dialog.getByTestId('requester-remarks').textContent).toBe(`“${REMARK}”`)

    const consent = dialog.getByTestId('approval-consent')
    expect(consent.textContent).toContain('WHAT APPROVING GIVES THEM')
    // THE BOLD LEADS ARE STRUCTURE, NOT STYLING: the wire sends `{lead, body}` precisely so the
    // browser never has to guess the split at the first full stop, and one body here starts
    // lowercase mid-sentence, which is what would break that guess.
    expect([...consent.querySelectorAll('b')].map((b) => b.textContent)).toEqual(
      CONSENT.map((line) => line.lead),
    )
    for (const line of CONSENT) expect(consent.textContent).toContain(line.body)
  })

  it('sets the asked-on line without a locale formatter, and in the plural they/their', async () => {
    const dialog = within(await openReview())

    // `4 Sep`, never `Sept` (en-GB/en-IN under current CLDR) and never `Sep 4` (en-US). The month
    // list is spelled once, in `ConnectorRow.tsx`, and every connector surface comes back to it.
    // The DATE half is fixed (midday UTC is 4 September in every timezone); the CLOCK half is
    // local, exactly as the queue's own `ASKED` column reads it, so it is computed the same way
    // rather than pinned to the machine the suite runs on.
    const asked = new Date(PRIYA.askedAt)
    const clock = `${String(asked.getHours()).padStart(2, '0')}:${String(asked.getMinutes()).padStart(2, '0')}`
    expect(
      dialog.getByText(
        `Asked on 4 Sep at ${clock}. One decision, covering every project they own.`,
      ),
    ).toBeTruthy()
    // THE PRONOUN DEPARTURE. The board reads `every project SHE owns` and `Give PRIYA access`
    // because it was drawn for one fictional person; shipped copy infers nobody's gender from
    // their name, and the button's name is interpolated off the row.
    expect(dialog.queryByText(/\bshe owns\b/)).toBeNull()
    expect(dialog.getByTestId('review-approve').textContent).toBe('Give Priya access')
  })

  it('★ shows no remarks box at rest — the approval takes no remark at all', async () => {
    const dialog = within(await openReview())

    // The positive half, so a crashed render cannot pass the absence below it.
    expect(dialog.getByTestId('review-approve').getAttribute('aria-disabled')).toBe('false')
    // THE LARGEST DEPARTURE IN THE PASS. The board draws `YOUR REMARKS` with a permanent
    // `REQUIRED` pill over both outcomes; there is no textarea in the document until `Decline`.
    expect(dialog.queryByTestId('decline-remarks')).toBeNull()
    expect(document.querySelectorAll('textarea').length).toBe(0)
    expect(dialog.queryByText('YOUR REMARKS')).toBeNull()
  })
})

describe('approving', () => {
  it('★ approves with the request id and nothing else, then moves the row to ALREADY DECIDED', async () => {
    h.approveConnectorRequest.mockImplementation(async () => {
      waitingRows = []
      decidedRows = [PRIYA_APPROVED]
    })
    await openReview()

    fireEvent.click(screen.getByTestId('review-approve'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    // NOTHING STORED. One argument, the id — no remark, no options object. The mutant is adding
    // one, and this is where it goes red at the component boundary; the HTTP shape has its own
    // assertion further down.
    expect(h.approveConnectorRequest.mock.calls).toEqual([['req-priya']])
    expect(onToast).toHaveBeenCalledWith('Priya Nair can now reach ORBIT.', 'ok')

    // THE QUEUE RELOADED AND THE ROW MOVED. A decided row left in `WAITING ON YOU` is how one
    // administrator answers the same request twice.
    const decided = within(await screen.findByTestId('queue-table-decided'))
    expect(within(decided.getByTestId('queue-row-req-priya')).getByText('Approved')).toBeTruthy()
    expect(screen.getByTestId('waiting-empty')).toBeTruthy()
  })

  it('★ posts with NO BODY — the wire shape, against the real module', async () => {
    const real = await vi.importActual<typeof import('../../../utils/adminConnectorApi')>(
      '../../../utils/adminConnectorApi',
    )
    const fetchImpl = vi.fn(
      async (_url: string, _opts?: RequestInit) => new Response('{}', { status: 200 }),
    )

    await real.approveConnectorRequest('req-priya', { fetchImpl })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/connector-requests/req-priya/approve')
    // Ordered so a missing options object fails here rather than silently satisfying the
    // absence assertion under it.
    expect(init?.method).toBe('POST')
    // THE ASSERTION THE APPROVAL DEPARTURE RESTS ON. An approval stores nothing, so it sends
    // nothing — not even the empty `{}` the citizen's cancel route is given. Add a remark field
    // to that function and this is the test that goes red.
    expect(init?.body).toBeUndefined()
  })

  it('declining sends the remark as a JSON body, so the empty approve body is a choice', async () => {
    const real = await vi.importActual<typeof import('../../../utils/adminConnectorApi')>(
      '../../../utils/adminConnectorApi',
    )
    const fetchImpl = vi.fn(
      async (_url: string, _opts?: RequestInit) => new Response('{}', { status: 200 }),
    )

    await real.declineConnectorRequest('req-priya', 'Not something ground ops needs today.', {
      fetchImpl,
    })

    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/admin/connector-requests/req-priya/decline')
    expect(init?.body).toBe(JSON.stringify({ remarks: 'Not something ground ops needs today.' }))
  })
})

describe('the revealed decline state', () => {
  it('★ reveals the textarea on Decline and puts focus in it', async () => {
    const dialog = within(await openReview())

    fireEvent.click(screen.getByTestId('review-decline'))

    const box = dialog.getByTestId('decline-remarks')
    expect(box).toBeTruthy()
    expect(document.activeElement).toBe(box)
    // The board's pill, kept — but only in the state where it is true.
    expect(dialog.getByText('REQUIRED')).toBeTruthy()
    // Nothing was sent by revealing it.
    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
  })

  it('★ closes the approve path: it goes aria-disabled and clicking it calls nothing', async () => {
    await openReview()
    fireEvent.click(screen.getByTestId('review-decline'))

    const approve = screen.getByTestId('review-approve')
    // MUTANT: drop `!declining` from `canApprove` and both halves of this go red. An
    // administrator composing a decline must not be one stray click from granting access.
    expect(approve.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(approve)

    expect(h.approveConnectorRequest).not.toHaveBeenCalled()
    // Liveness: the dialog is still open and still on this person, so "nothing was called"
    // cannot be satisfied by a crash.
    expect(screen.getByTestId('connector-review-dialog')).toBeTruthy()
    expect(screen.getByTestId('decline-remarks')).toBeTruthy()
  })

  it('★ refuses a four-word remark in the words the server would use, and calls nothing', async () => {
    const dialog = within(await typeDecline('Not enough detail here'))

    expect(dialog.getByTestId('decline-count').textContent).toBe('4/50 words')
    expect(dialog.getByTestId('decline-rule').textContent).toBe(
      'Give a little more detail — at least 5 words.',
    )
    const submit = dialog.getByTestId('review-decline')
    expect(submit.getAttribute('aria-disabled')).toBe('true')

    fireEvent.click(submit)

    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
    expect(screen.getByTestId('connector-review-dialog')).toBeTruthy()
  })

  it('★ refuses whitespace, and the counter reads zero rather than counting the spaces', async () => {
    const dialog = within(await typeDecline('    \t  \n '))

    // `countWords` splits by PYTHON's whitespace set, which is why this is 0 and not 1 — the
    // server's own empty check is `value.strip()`, and a JS `.trim()` here would agree with it
    // almost everywhere, which is the worst kind of agreement to rely on.
    expect(dialog.getByTestId('decline-count').textContent).toBe('0/50 words')
    expect(dialog.getByTestId('decline-rule').textContent).toBe(
      'Say why you are declining this request.',
    )
    fireEvent.click(dialog.getByTestId('review-decline'))

    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
    expect(dialog.getByTestId('decline-remarks')).toBeTruthy()
  })

  it('★ counts a non-breaking space and an ideographic space the way the server does', async () => {
    // U+00A0 and U+3000 are both in Python's whitespace set and NOT in a naive `\s`-shaped
    // split; `words.ts` spells the set out for exactly this, and `words.test.ts` /
    // `test_project_name_words.py` pin the two implementations against each other. This asserts
    // the DIALOG reads that shared counter rather than a `split(' ')` of its own.
    const exotic = 'Ground\u00a0ops already read this\u3000data elsewhere'
    expect(countWords(exotic)).toBe(7)
    const dialog = within(await typeDecline(exotic))

    expect(dialog.getByTestId('decline-count').textContent).toBe('7/50 words')
    expect(dialog.getByTestId('review-decline').getAttribute('aria-disabled')).toBe('false')

    fireEvent.click(dialog.getByTestId('review-decline'))

    // VERBATIM, including both exotic spaces: the citizen reads this string as written.
    await waitFor(() =>
      expect(h.declineConnectorRequest.mock.calls).toEqual([['req-priya', exotic]]),
    )
  })

  it('★ declines with a valid remark and moves the row to ALREADY DECIDED', async () => {
    const reason = 'Ground ops already read this data from another system.'
    h.declineConnectorRequest.mockImplementation(async () => {
      waitingRows = []
      decidedRows = [
        {
          ...PRIYA_APPROVED,
          status: 'declined',
          decisionRemarks: reason,
          usingItIn: null,
        },
      ]
    })
    await typeDecline(reason)

    fireEvent.click(screen.getByTestId('review-decline'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    expect(h.declineConnectorRequest.mock.calls).toEqual([['req-priya', reason]])
    expect(onToast).toHaveBeenCalledWith('Priya Nair’s request was declined.', 'ok')
    const decided = within(await screen.findByTestId('queue-table-decided'))
    expect(within(decided.getByTestId('queue-row-req-priya')).getByText('Declined')).toBeTruthy()
  })
})

describe('Cancel', () => {
  it('★ closes with nothing sent, from the resting state', async () => {
    await openReview()

    fireEvent.click(screen.getByTestId('review-cancel'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    expect(h.approveConnectorRequest).not.toHaveBeenCalled()
    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
    expect(onToast).not.toHaveBeenCalled()
    // Liveness: the queue the dialog opened over is still on screen, still holding the row.
    expect(screen.getByTestId('queue-row-req-priya')).toBeTruthy()
  })

  it('★ closes with nothing sent from the revealed decline state too, remark and all', async () => {
    await typeDecline('Ground ops already read this data from another system.')

    fireEvent.click(screen.getByTestId('review-cancel'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    expect(h.declineConnectorRequest).not.toHaveBeenCalled()
    expect(onToast).not.toHaveBeenCalled()
    expect(screen.getByTestId('queue-row-req-priya')).toBeTruthy()
  })
})

describe('the two 409s, told apart by their code and not their status', () => {
  it('★ already_decided names who decided and when, composed from error.detail', async () => {
    // The other administrator got there first: the row is decided by the time this call lands,
    // which is exactly what the reload behind the refusal has to show.
    h.approveConnectorRequest.mockImplementation(async () => {
      waitingRows = []
      decidedRows = [PRIYA_APPROVED]
      throw new ApiError('Rahul Menon has already approved this request.', 409, 'already_decided', {
        code: 'already_decided',
        message: 'Rahul Menon has already approved this request.',
        // THE WHEN RIDES `detail`, never the sentence — the server formats no human-readable
        // date, and this console formats every date on the screen behind the dialog.
        detail: {
          status: 'approved',
          decidedByName: 'Rahul Menon',
          decidedAt: '2026-09-02T13:00:00.000Z',
        },
      })
    })
    await openReview()

    fireEvent.click(screen.getByTestId('review-approve'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    expect(onToast).toHaveBeenCalledWith(
      'Rahul Menon already approved this request on 2 Sep.',
      'problem',
    )
    // CLOSED INTO A RELOADED QUEUE, not left over a stale row: the request is now in
    // `ALREADY DECIDED`, which is where the refusal says it is.
    const decided = within(await screen.findByTestId('queue-table-decided'))
    expect(within(decided.getByTestId('queue-row-req-priya')).getByText('Approved')).toBeTruthy()
  })

  it('★ request_cancelled shows the cancelled sentence and carries no detail at all', async () => {
    // The citizen withdrew between the render and the click: the row is now in NEITHER table.
    h.approveConnectorRequest.mockImplementation(async () => {
      waitingRows = []
      throw new ApiError(
        'This request was cancelled before you decided it.',
        409,
        'request_cancelled',
        // No `detail`: a cancelled row's decision fields are both null, so there is nothing
        // measured to hand over — and the already-decided sentence would name nobody at no time.
        { code: 'request_cancelled', message: 'This request was cancelled before you decided it.' },
      )
    })
    await openReview()

    fireEvent.click(screen.getByTestId('review-approve'))

    await waitFor(() => expect(screen.queryByTestId('connector-review-dialog')).toBeNull())
    expect(onToast).toHaveBeenCalledWith(
      'This request was cancelled before you decided it.',
      'problem',
    )
    // Gone from both tables, as a withdrawn request is — and the panel says so rather than
    // rendering a queue that still looks like it has work in it.
    expect(await screen.findByTestId('waiting-empty')).toBeTruthy()
  })

  it('a refusal that is NOT a lost row leaves the dialog open with the remark still in it', async () => {
    const reason = 'Ground ops already read this data from another system.'
    h.declineConnectorRequest.mockRejectedValue(
      new ApiError('The security check failed. Reload and try again.', 403, 'csrf_failed', {
        code: 'csrf_failed',
        message: 'The security check failed. Reload and try again.',
      }),
    )
    await typeDecline(reason)

    fireEvent.click(screen.getByTestId('review-decline'))

    // A 403 is worth retrying, so the dialog stays and the typed words are not thrown away —
    // which is also what proves the 409 branch above is about the CODE and not about failure.
    expect(await screen.findByTestId('review-error')).toBeTruthy()
    expect(screen.getByTestId('review-error').textContent).toContain('The security check failed')
    expect(screen.getByTestId('connector-review-dialog')).toBeTruthy()
    expect(screen.getByTestId<HTMLTextAreaElement>('decline-remarks').value).toBe(reason)
    expect(onToast).not.toHaveBeenCalled()
  })
})
