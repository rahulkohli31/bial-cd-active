/**
 * DataClassificationModal: the pre-publish form — six weighted Yes/No toggles pre-filled
 * by the automatic review, the notes gate tied to the routing rule (any weighted Yes both
 * flips the action to "Send for review" AND is obliged to explain itself), and a Cancel
 * structurally isolated from the submit (backdrop/Escape/button all resolve to the same
 * `onCancel`, none of them reachable from `onConfirm`).
 *
 * What these do NOT assert, deliberately: that a weighted-Yes total blocks the button.
 * It must not — the running total is informational, the server re-reads the STORED
 * review and merges there, so a test pinning a client-side block would enshrine exactly
 * the bypassable design this avoids. What IS pinned client-side: the review's answers
 * land only on untouched questions (merge, never clobber), responses are filtered by the
 * version stamp the dialog asked about, Escape stays available while the review runs
 * (the busy block covers a submit in flight only), and progress/arrival/failure announce
 * through a polite live region.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import DataClassificationModal from '../DataClassificationModal'
import { ApiError } from '../../utils/apiError'
import * as classificationApi from '../../utils/classificationApi'
import type {
  ClassificationReview,
  QuestionReview,
  ReviewVerdicts,
} from '../../utils/classificationApi'
import type { DataClassificationAnswers } from '../../utils/deployApi'

vi.mock('../../utils/classificationApi', async () => {
  const actual = await vi.importActual<typeof classificationApi>('../../utils/classificationApi')
  return { ...actual, ensureClassificationReview: vi.fn(), getClassificationReview: vi.fn() }
})

const ensureReview = vi.mocked(classificationApi.ensureClassificationReview)
const getReview = vi.mocked(classificationApi.getClassificationReview)

const CATEGORY_KEYS = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

const NO_SIGN = 'We found no sign of this in your app.'

function verdicts(overrides: Partial<ReviewVerdicts> = {}): ReviewVerdicts {
  const no: QuestionReview = { verdict: 'no', reason: NO_SIGN }
  return {
    credentialsSecrets: no,
    healthData: no,
    personalInformation: no,
    financialData: no,
    confidentialBusinessData: no,
    publicData: no,
    ...overrides,
  }
}

function allUnanswered(): ReviewVerdicts {
  const unanswered: QuestionReview = {
    verdict: 'unanswered',
    reason: 'The automatic check could not finish, so this question needs your own answer.',
  }
  return {
    credentialsSecrets: unanswered,
    healthData: unanswered,
    personalInformation: unanswered,
    financialData: unanswered,
    confidentialBusinessData: unanswered,
    publicData: unanswered,
  }
}

const SHA = 'a1b2c3d4e5f6a7b8'
const BASE: ClassificationReview = {
  status: 'complete',
  headSha: SHA,
  savedAt: '2026-08-19T10:15:00Z',
  reviewedSha: SHA,
  verdicts: null,
  failureCode: null,
  failureMessage: null,
  retryable: false,
}

const COMPLETE_ALL_NO: ClassificationReview = { ...BASE, verdicts: verdicts() }
const RUNNING: ClassificationReview = { ...BASE, status: 'running' }
/**
 * The default wiring for the hand-answering scenarios: a settled FAILED review with six
 * unanswered questions. No pre-fill, so every legacy flow (answer six, notes gate,
 * submit) reads exactly as it did before the review existed — while still exercising the
 * real mount-time ensure-POST.
 */
const FAILED_UNANSWERED: ClassificationReview = {
  ...BASE,
  status: 'failed',
  verdicts: allUnanswered(),
  failureCode: 'review_failed',
  failureMessage: "The automatic check couldn't run.",
  retryable: true,
}
const NOTHING_SAVED: ClassificationReview = {
  ...BASE,
  status: 'nothing_to_review',
  headSha: null,
  savedAt: null,
  reviewedSha: null,
}

interface RenderProps {
  onConfirm?: (answers: DataClassificationAnswers) => Promise<void>
  onCancel?: () => void
}

/** Render with the mount-time ensure-POST settled, so gating reflects a landed review. */
async function renderModal(props: RenderProps = {}): Promise<ReturnType<typeof render>> {
  const utils = render(
    <DataClassificationModal
      projectId="p1"
      onConfirm={props.onConfirm ?? vi.fn()}
      onCancel={props.onCancel ?? vi.fn()}
    />,
  )
  await act(async () => {})
  return utils
}

function answerAll(value: 'yes' | 'no', except: (typeof CATEGORY_KEYS)[number][] = []): void {
  for (const key of CATEGORY_KEYS) {
    if (except.includes(key)) continue
    fireEvent.click(screen.getByTestId(`dc-question-${key}-${value}`))
  }
}

function confirmButton(): HTMLButtonElement {
  const button = screen.getByTestId('dc-confirm')
  if (!(button instanceof HTMLButtonElement)) throw new Error('dc-confirm is not a button')
  return button
}

beforeEach(() => {
  vi.clearAllMocks()
  ensureReview.mockResolvedValue(FAILED_UNANSWERED)
  getReview.mockResolvedValue(FAILED_UNANSWERED)
})
afterEach(cleanup)

describe('DataClassificationModal', () => {
  it('starts with Confirm disabled and no warning line (unanswered, not "no")', async () => {
    await renderModal()
    expect(confirmButton().disabled).toBe(true)
    expect(screen.queryByTestId('dc-warning')).toBeNull()
  })

  it('requires all six answers before Confirm enables', async () => {
    await renderModal()
    answerAll('no', ['publicData'])
    expect(confirmButton().disabled).toBe(true)
    expect(screen.queryByTestId('dc-warning')).toBeNull() // still not all-answered

    fireEvent.click(screen.getByTestId('dc-question-publicData-no'))
    expect(confirmButton().disabled).toBe(false)
    expect(screen.getByTestId('dc-warning').textContent).toMatch(/nothing sensitive was recorded/i)
  })

  it('distinguishes "answered, all No" from "unanswered" — the warning only ever appears once complete', async () => {
    await renderModal()
    answerAll('no', ['healthData'])
    expect(screen.queryByTestId('dc-warning')).toBeNull()
  })

  it('Credentials/Secrets alone flips the action to "Send for review" and gates it on notes (weight 40)', async () => {
    await renderModal()
    answerAll('no', ['credentialsSecrets'])
    fireEvent.click(screen.getByTestId('dc-question-credentialsSecrets-yes'))

    expect(screen.getByTestId('dc-warning').textContent).toMatch(/sensitive data/i)
    expect(confirmButton().textContent).toContain('Send for review')
    expect(confirmButton().disabled).toBe(true)

    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vaulted, never logged.' } })
    expect(confirmButton().disabled).toBe(false)
    expect(confirmButton().textContent).toContain('Send for review')
  })

  it('ties the warning to the explanation field for assistive tech, and clears invalid once written', async () => {
    // R10 obliges the explanation, and the warning is the copy saying so — a screen
    // reader has to reach it FROM the field, not stumble on it. Asserted both ways so
    // a hardcoded `aria-invalid` cannot pass.
    await renderModal()
    answerAll('no', ['credentialsSecrets'])
    fireEvent.click(screen.getByTestId('dc-question-credentialsSecrets-yes'))

    const notes = screen.getByTestId('dc-notes')
    expect(notes.getAttribute('aria-describedby')).toBe('dc-warning')
    expect(screen.getByTestId('dc-warning').id).toBe('dc-warning')
    expect(notes.getAttribute('aria-required')).toBe('true')
    expect(notes.getAttribute('aria-invalid')).toBe('true')

    fireEvent.change(notes, { target: { value: 'Vaulted, never logged.' } })
    expect(notes.getAttribute('aria-invalid')).toBe('false')
  })

  it('Health Data alone also crosses the threshold (weight 25)', async () => {
    await renderModal()
    answerAll('no', ['healthData'])
    fireEvent.click(screen.getByTestId('dc-question-healthData-yes'))
    expect(confirmButton().disabled).toBe(true)
  })

  it('any nonzero total requires the explanation — the notes gate and the routing rule are one condition', async () => {
    // Public Data (0) + Confidential Business Data (15): the lowest nonzero total the
    // questionnaire can produce, so the sharpest case to pin the tie-together on.
    await renderModal()
    answerAll('no', ['publicData', 'confidentialBusinessData'])
    fireEvent.click(screen.getByTestId('dc-question-publicData-yes'))
    fireEvent.click(screen.getByTestId('dc-question-confidentialBusinessData-yes'))

    expect(screen.getByTestId('dc-warning').textContent).toMatch(/sensitive data/i)
    expect(confirmButton().disabled).toBe(true)

    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Vendor contact list only.' } })
    expect(confirmButton().disabled).toBe(false)
  })

  // --- the action's label states which of the two things it will do -------------

  it('with no weighted Yes the action reads Publish and no explanation is demanded', async () => {
    await renderModal()
    answerAll('no')
    const button = confirmButton()
    expect(button.textContent).toContain('Publish')
    expect(button.textContent).not.toContain('Send for review')
    expect(button.disabled).toBe(false) // notes untouched — not demanded

    const score = screen.getByTestId('dc-score')
    expect(score.textContent).toContain('0')
    expect(score.textContent).toMatch(/can publish without review/i)
  })

  it('the retired "ask an administrator" dead end is GONE — the score line promises the review the platform now performs', async () => {
    // The old copy told the citizen this "can't publish automatically; ask an
    // administrator to review this app" — an out-of-band dead end rendered inches from
    // the button that now does it for them. Guard the retirement, don't just delete
    // the old assertion (repo convention: cleanly-removing-dead-ui-controls).
    await renderModal()
    answerAll('no', ['healthData'])
    fireEvent.click(screen.getByTestId('dc-question-healthData-yes'))

    const score = screen.getByTestId('dc-score')
    expect(score.textContent).toContain('25')
    expect(score.textContent).not.toMatch(/ask an administrator/i)
    expect(score.textContent).not.toMatch(/can't publish automatically/i)
    expect(score.textContent).toMatch(/sent to an administrator for review/i)
  })

  it('Confirm calls onConfirm with the complete answer set, notes trimmed to null when blank', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    await renderModal({ onConfirm })
    answerAll('no')
    fireEvent.click(confirmButton())

    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
    expect(onConfirm).toHaveBeenCalledWith({
      credentialsSecrets: false,
      healthData: false,
      personalInformation: false,
      financialData: false,
      confidentialBusinessData: false,
      publicData: false,
      notes: null,
    })
  })

  it('a rejected Confirm keeps the modal open and shows the error, answers intact', async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error('Could not submit — try again.'))
    await renderModal({ onConfirm })
    answerAll('no')
    fireEvent.click(confirmButton())

    expect((await screen.findByRole('alert')).textContent).toContain('Could not submit')
    expect(screen.getByTestId('data-classification-modal')).toBeTruthy()
    expect(confirmButton().disabled).toBe(false)
  })

  // --- Cancel is structural ----------------------------------------------------

  it('the Cancel button calls onCancel only — never onConfirm', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    await renderModal({ onConfirm, onCancel })
    answerAll('no')
    fireEvent.click(screen.getByTestId('dc-cancel'))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('a backdrop click calls onCancel only', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const { container } = await renderModal({ onConfirm, onCancel })
    const backdrop = container.querySelector('.bg-black\\/40')
    expect(backdrop).toBeTruthy()
    fireEvent.click(backdrop as Element)
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('Escape calls onCancel only (and is inert while a SUBMIT is in flight)', async () => {
    const onCancel = vi.fn()
    let release: () => void = () => {}
    const onConfirm = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { release = resolve }),
    )
    const { container } = await renderModal({ onConfirm, onCancel })
    answerAll('no')
    // The Tab/Escape trap is on the focusable CARD (tabIndex={-1}), one level inside the
    // `data-classification-modal` overlay — React's onKeyDown only sees events dispatched
    // on itself or bubbling up from a descendant, so the key must target the card.
    const card = container.querySelector('[tabindex="-1"]') as Element

    fireEvent.click(confirmButton())
    await waitFor(() => expect(confirmButton().disabled).toBe(true))
    fireEvent.keyDown(card, { key: 'Escape' })
    expect(onCancel).not.toHaveBeenCalled() // busy: Escape is inert

    release()
    await waitFor(() => expect(confirmButton().disabled).toBe(false))
    fireEvent.keyDown(card, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).toHaveBeenCalledTimes(1) // never called a second time by Escape
  })
})

describe('the review pre-fill', () => {
  it('a complete review pre-fills all six answers and renders six reasons', async () => {
    ensureReview.mockResolvedValue({
      ...COMPLETE_ALL_NO,
      verdicts: verdicts({
        personalInformation: { verdict: 'yes', reason: 'Names and emails are stored.' },
      }),
    })
    await renderModal()

    expect(
      screen.getByTestId('dc-question-personalInformation-yes').getAttribute('aria-checked'),
    ).toBe('true')
    for (const key of CATEGORY_KEYS) {
      if (key === 'personalInformation') continue
      expect(screen.getByTestId(`dc-question-${key}-no`).getAttribute('aria-checked')).toBe('true')
      expect(screen.getByTestId(`dc-reason-${key}`).textContent).toBe(NO_SIGN)
    }
    expect(screen.getByTestId('dc-reason-personalInformation').textContent).toBe(
      'Names and emails are stored.',
    )
    // Pre-filled six-of-six with a weighted Yes: the action already says which thing
    // it will do, and the explanation is demanded before it enables.
    expect(confirmButton().textContent).toContain('Send for review')
    expect(confirmButton().disabled).toBe(true)
  })

  it('names the version being reviewed and when it was saved, from the moment it opens', async () => {
    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    await renderModal()

    const version = screen.getByTestId('dc-review-version')
    expect(version.textContent).toContain(`Version ${SHA.slice(0, 7)}`)
    expect(version.textContent).not.toContain(SHA) // short stamp, not the full hash
    expect(version.textContent).toMatch(/saved/i)
  })

  it('re-opening for an unchanged version renders the stored answers and never polls (AE4)', async () => {
    // The server answers the ensure-POST with the stored COMPLETE row — settled, so the
    // dialog has nothing to poll and issues no GET. One ask per open, no run implied.
    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    const first = await renderModal()
    expect(screen.getByTestId('dc-question-publicData-no').getAttribute('aria-checked')).toBe(
      'true',
    )
    first.unmount()

    await renderModal()
    expect(screen.getByTestId('dc-question-publicData-no').getAttribute('aria-checked')).toBe(
      'true',
    )
    expect(ensureReview).toHaveBeenCalledTimes(2) // once per open
    expect(getReview).not.toHaveBeenCalled() // settled: nothing to poll
  })

  it('a question the review left unanswered blocks submission until answered, visibly distinct from a No', async () => {
    ensureReview.mockResolvedValue({
      ...COMPLETE_ALL_NO,
      verdicts: verdicts({
        healthData: { verdict: 'unanswered', reason: 'We could not tell from the code.' },
      }),
    })
    await renderModal()

    // Marked as needing the citizen — NOT selected as No.
    expect(screen.getByTestId('dc-unanswered-healthData')).toBeTruthy()
    expect(screen.getByTestId('dc-question-healthData-no').getAttribute('aria-checked')).toBe(
      'false',
    )
    expect(screen.getByTestId('dc-question-healthData-yes').getAttribute('aria-checked')).toBe(
      'false',
    )
    // Five pre-filled No's, one hole: the submit stays blocked by the hole alone.
    expect(confirmButton().disabled).toBe(true)

    fireEvent.click(screen.getByTestId('dc-question-healthData-no'))
    expect(screen.queryByTestId('dc-unanswered-healthData')).toBeNull() // answered now
    expect(confirmButton().disabled).toBe(false)
  })

  it('renders a leaked-credential reason verbatim — no file name, no value, no markdown mangling (AE1)', async () => {
    // Server-shaped reason: the backend strips locations and values before this body is
    // built (R3). The render path's obligation is to pass it through VERBATIM in a
    // whitespace-preserving plain element — the shared markdown renderer would collapse
    // the single newline (documented repo bug) and transform the asterisks.
    const reason =
      'Your saved code contains a real password for an outside service.\n' +
      'Anyone who can open the app can read it — move it into a *setting* and save again.'
    ensureReview.mockResolvedValue({
      ...COMPLETE_ALL_NO,
      verdicts: verdicts({ credentialsSecrets: { verdict: 'yes', reason } }),
    })
    await renderModal()

    const rendered = screen.getByTestId('dc-reason-credentialsSecrets')
    expect(rendered.textContent).toBe(reason) // byte-for-byte: newline kept, nothing added
    expect(rendered.className).toContain('whitespace-pre-wrap')
    // Nothing anywhere in the dialog names a file or a secret value the server withheld.
    const modalText = screen.getByTestId('data-classification-modal').textContent ?? ''
    expect(modalText).not.toMatch(/src\/|\.tsx?|\.env|hunter2/)
  })
})

describe('while the review runs', () => {
  /** Let the poll interval fire once, with the GET's promise settled. */
  async function tickPoll(): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
  }

  it('waits with a spinner and copy that says they may close and come back, and sets the ~20s expectation', async () => {
    ensureReview.mockResolvedValue(RUNNING)
    await renderModal()

    const status = screen.getByTestId('dc-review-status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toMatch(/close this and come back/i)
    expect(status.textContent).toMatch(/20 seconds/)
    expect(status.textContent).toMatch(/up to a minute/i)
    // OD-A: never claim closing loses the result — it is stored against the version.
    // (Word-bounded: "close" contains "lose".)
    expect(status.textContent).not.toMatch(/\b(lose|lost|losing)\b|start over|again from/i)
    // The version is named from the moment it opens, review or no review.
    expect(screen.getByTestId('dc-review-version').textContent).toContain(SHA.slice(0, 7))
    // Nothing to submit yet: the label cannot yet say which thing it would do.
    expect(confirmButton().disabled).toBe(true)
  })

  it('Escape closes the dialog WHILE the review runs — the busy block covers a submit only', async () => {
    // The existing rule blocks Escape during a submit in flight. Widening it to the
    // review window would trap someone for up to a minute, and closing costs nothing.
    ensureReview.mockResolvedValue(RUNNING)
    const onCancel = vi.fn()
    const { container } = await renderModal({ onCancel })
    const card = container.querySelector('[tabindex="-1"]') as Element

    fireEvent.keyDown(card, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    // Cancel stays clickable too.
    const cancel = screen.getByTestId('dc-cancel')
    expect(cancel instanceof HTMLButtonElement && cancel.disabled).toBe(false)
  })

  it('re-opening after closing mid-run resumes from the stored result', async () => {
    ensureReview.mockResolvedValue(RUNNING)
    const first = await renderModal()
    expect(screen.getByTestId('dc-review-status').textContent).toMatch(/checking your saved app/i)
    first.unmount()

    // The run finished while the dialog was closed; the result is stored against the
    // version, so re-opening reads it back rather than starting over.
    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    await renderModal()
    expect(screen.getByTestId('dc-question-publicData-no').getAttribute('aria-checked')).toBe(
      'true',
    )
  })

  describe('with the poll clock driven', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('an arriving review does NOT overwrite an answer the citizen already changed, and shows the disagreement', async () => {
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})

      // The citizen answers Yes while the review is still running.
      fireEvent.click(screen.getByTestId('dc-question-financialData-yes'))

      getReview.mockResolvedValue({
        ...COMPLETE_ALL_NO,
        verdicts: verdicts({ financialData: { verdict: 'no', reason: 'No payment data found.' } }),
      })
      await tickPoll()

      // Their answer stands — merge, never clobber — and the disagreement is on screen.
      expect(screen.getByTestId('dc-question-financialData-yes').getAttribute('aria-checked')).toBe(
        'true',
      )
      // The developer's Yes is the one that survives here, and the copy says so — the
      // opposite direction gets a different sentence (pinned below).
      const note = screen.getByTestId('dc-disagreement-financialData').textContent ?? ''
      expect(note).toMatch(/did not find this/i)
      expect(note).toMatch(/your yes is what goes on record/i)
      // Untouched questions DID take the review's answers.
      expect(screen.getByTestId('dc-question-healthData-no').getAttribute('aria-checked')).toBe(
        'true',
      )
      expect(screen.queryByTestId('dc-disagreement-healthData')).toBeNull()
    })

    it('does not let a slow earlier poll walk a finished review back to running', async () => {
      // The interval fires every 5s WITHOUT waiting for the previous request, so two polls
      // overlap the moment one is slow. Here the second tick answers first with a
      // completed review; the first tick's stale `running` arrives afterwards. Applying it
      // would re-disable the button the citizen was about to press and re-show the spinner
      // on a review that had already landed.
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})

      let releaseSlow: (value: ClassificationReview) => void = () => {}
      const slow = new Promise<ClassificationReview>((resolve) => {
        releaseSlow = resolve
      })
      getReview.mockReturnValueOnce(slow) // tick 1 — hangs
      getReview.mockResolvedValueOnce(COMPLETE_ALL_NO) // tick 2 — answers first

      await tickPoll() // fires tick 1 (still pending)
      await tickPoll() // fires tick 2, which settles and paints the completed review
      expect(screen.getByTestId('dc-review-status').textContent).not.toMatch(
        /checking your saved app/i,
      )

      await act(async () => {
        releaseSlow(RUNNING) // the stale earlier response finally lands
        await Promise.resolve()
      })

      // Still complete. Without the ordering guard this reads "checking your saved app".
      expect(screen.getByTestId('dc-review-status').textContent).not.toMatch(
        /checking your saved app/i,
      )
      expect(confirmButton().disabled).toBe(false)
    })

    it('never paints answers stamped a version this dialog did not ask about', async () => {
      // A second tab saved and its newer review landed first. Painting those answers
      // here would describe a version this dialog never named — so it does not, and it
      // stops waiting rather than polling for a row that no longer exists.
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})

      getReview.mockResolvedValue({
        ...COMPLETE_ALL_NO,
        headSha: 'ffffffffffff',
        reviewedSha: 'ffffffffffff',
        verdicts: verdicts({
          credentialsSecrets: { verdict: 'yes', reason: 'A key from the OTHER version.' },
        }),
      })
      await tickPoll()

      expect(
        screen.getByTestId('dc-question-credentialsSecrets-yes').getAttribute('aria-checked'),
      ).toBe('false')
      expect(screen.queryByTestId('dc-reason-credentialsSecrets')).toBeNull()
      // AND it leaves the running phase. The app's one review row was re-stamped, so the
      // review this dialog is waiting for will never arrive: polling on left an immortal
      // spinner with Confirm disabled (the review read as pending) and no "Check again"
      // (the status was not `failed`) — only Cancel got the citizen out, and nothing on
      // screen said so.
      expect(screen.getByTestId('dc-review-status').textContent).toMatch(/saved again/i)
      expect(screen.getByTestId('dc-review-status').textContent).not.toMatch(
        /checking your saved app/i,
      )
    })

    it('announces arrival through the live region when the review lands', async () => {
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})
      expect(screen.getByTestId('dc-review-status').textContent).toMatch(/checking your saved app/i)

      getReview.mockResolvedValue(COMPLETE_ALL_NO)
      await tickPoll()

      const status = screen.getByTestId('dc-review-status')
      expect(status.getAttribute('aria-live')).toBe('polite')
      expect(status.textContent).toMatch(/finished/i)
      expect(status.textContent).toMatch(/you can change any answer/i)
    })

    it('announces the fall-through through the same live region when the review fails mid-run', async () => {
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})

      getReview.mockResolvedValue(FAILED_UNANSWERED)
      await tickPoll()

      const status = screen.getByTestId('dc-review-status')
      expect(status.getAttribute('aria-live')).toBe('polite')
      expect(status.textContent).toContain("The automatic check couldn't run.")
      // And the citizen can now answer for themselves.
      expect(screen.getByTestId('dc-question-healthData-no')).toBeTruthy()
    })

    it('stops polling once the review settles', async () => {
      ensureReview.mockResolvedValue(RUNNING)
      render(
        <DataClassificationModal projectId="p1" onConfirm={vi.fn()} onCancel={vi.fn()} />,
      )
      await act(async () => {})

      getReview.mockResolvedValue(COMPLETE_ALL_NO)
      await tickPoll()
      const settled = getReview.mock.calls.length
      expect(settled).toBeGreaterThan(0) // it DID poll — otherwise this proves nothing

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000)
      })
      expect(getReview.mock.calls.length).toBe(settled)
    })
  })
})

describe('the failure buckets', () => {
  function failure(
    code: string,
    message: string,
    retryable: boolean,
  ): ClassificationReview {
    return {
      ...BASE,
      status: 'failed',
      verdicts: allUnanswered(),
      failureCode: code,
      failureMessage: message,
      retryable,
    }
  }

  // The server owns the copy and the retry decision (already AND-ed with its attempt
  // cap); the dialog renders the sentence it was handed and offers the re-check only
  // where the server said one could help.
  const BUCKETS: readonly [string, string, boolean][] = [
    ['bundle_unreadable', "Your saved app couldn't be read. Tell an administrator.", false],
    [
      'storage_unavailable',
      "We can't reach your saved app right now. Please try again in a moment.",
      true,
    ],
    ['review_failed', "The automatic check couldn't run.", true],
    ['review_abandoned', "The automatic check couldn't run.", true],
    [
      'version_drift',
      "Your app was saved again while the check was running, so the result doesn't match what's saved now. Ask for a fresh check.",
      true,
    ],
  ]

  for (const [code, message, retryable] of BUCKETS) {
    it(`renders the ${code} sentence and ${retryable ? 'offers' : 'withholds'} a re-check`, async () => {
      ensureReview.mockResolvedValue(failure(code, message, retryable))
      await renderModal()

      expect(screen.getByTestId('dc-review-status').textContent).toContain(message)
      if (retryable) {
        expect(screen.getByTestId('dc-recheck')).toBeTruthy()
      } else {
        expect(screen.queryByTestId('dc-recheck')).toBeNull()
      }
      // A failure is never stored as an answer (R19): six unanswered questions the
      // citizen must answer, and the submit stays blocked until they do.
      expect(confirmButton().disabled).toBe(true)
      expect(screen.getByTestId('dc-question-healthData-no').getAttribute('aria-checked')).toBe(
        'false',
      )
    })
  }

  it('a retryable re-check calls the ensure-POST again, not a separate retry verb', async () => {
    ensureReview.mockResolvedValue(failure('review_failed', "The automatic check couldn't run.", true))
    await renderModal()
    expect(ensureReview).toHaveBeenCalledTimes(1)

    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    fireEvent.click(screen.getByTestId('dc-recheck'))
    await act(async () => {})

    expect(ensureReview).toHaveBeenCalledTimes(2)
    expect(ensureReview).toHaveBeenLastCalledWith('p1')
    expect(getReview).not.toHaveBeenCalled() // the GET never starts a run
    // The answers arrive on the retry.
    expect(screen.getByTestId('dc-question-publicData-no').getAttribute('aria-checked')).toBe(
      'true',
    )
  })

  it('the two non-retryable buckets differ from the retryable ones ONLY by the affordance, not by silence', async () => {
    // Mutation receipt: render the re-check unconditionally and this goes red.
    ensureReview.mockResolvedValue(
      failure('bundle_unreadable', "Your saved app couldn't be read. Tell an administrator.", false),
    )
    await renderModal()

    expect(screen.queryByTestId('dc-recheck')).toBeNull()
    expect(screen.getByTestId('dc-review-status').textContent).toMatch(/couldn't be read/i)
  })

  it('an app with nothing saved says so and offers no questions (R21)', async () => {
    ensureReview.mockResolvedValue(NOTHING_SAVED)
    await renderModal()

    expect(screen.getByTestId('dc-review-status').textContent).toMatch(
      /nothing saved to check yet/i,
    )
    // No questions, no score, no explanation field, and nothing to submit.
    for (const key of CATEGORY_KEYS) {
      expect(screen.queryByTestId(`dc-question-${key}-yes`)).toBeNull()
    }
    expect(screen.queryByTestId('dc-score')).toBeNull()
    expect(screen.queryByTestId('dc-notes')).toBeNull()
    expect(screen.queryByTestId('dc-confirm')).toBeNull()
    // There is no version to name either.
    expect(screen.queryByTestId('dc-review-version')).toBeNull()
    // Cancel still works — the way out is never removed.
    expect(screen.getByTestId('dc-cancel')).toBeTruthy()
  })

  it('a transport failure of the ask itself is rendered, with a re-check, rather than an empty dialog', async () => {
    ensureReview.mockRejectedValue(
      new ApiError("We can't reach your saved app right now.", 503, 'storage_unavailable'),
    )
    await renderModal()

    expect(screen.getByTestId('dc-review-status').textContent).toContain(
      "We can't reach your saved app right now.",
    )
    expect(screen.getByTestId('dc-recheck')).toBeTruthy()
    // The citizen can still answer by hand — an unreachable check never blocks the form.
    answerAll('no')
    expect(confirmButton().disabled).toBe(false)
  })
})

/**
 * The number on this screen is the one thing it exists to get right, and it used to score
 * the developer's answers ALONE. A developer who set the check's two Yes verdicts back to
 * No was shown "0 — no sensitive data declared — this can publish automatically" with a
 * button reading Publish, moments before the server merged the same answers to 45, refused
 * for a missing explanation, and routed the app.
 *
 * The gate itself was never at risk — the server merges and it decides. What was wrong was
 * every sentence the developer read on the way there.
 */
describe('the total is the ANSWER OF RECORD, not the developer’s answers alone', () => {
  /** The Passenger Feedback Log case: the check finds Health (25) and PII (20). */
  const FOUND_HEALTH_AND_PII: ClassificationReview = {
    ...BASE,
    verdicts: verdicts({
      healthData: { verdict: 'yes', reason: 'A named passenger’s wheelchair assistance is noted.' },
      personalInformation: { verdict: 'yes', reason: 'Names, emails and phone numbers are listed.' },
    }),
  }

  it('keeps the check’s Yes in the total when the developer answers No', async () => {
    ensureReview.mockResolvedValue(FOUND_HEALTH_AND_PII)
    await renderModal()
    expect(screen.getByTestId('dc-score').textContent).toContain('45')

    // The developer overrides both. Their answers are kept on screen — merge, never
    // clobber — but the check's Yes verdicts still stand on the record, so the total holds.
    fireEvent.click(screen.getByTestId('dc-question-healthData-no'))
    fireEvent.click(screen.getByTestId('dc-question-personalInformation-no'))
    expect(screen.getByTestId('dc-question-healthData-no').getAttribute('aria-checked')).toBe('true')

    const score = screen.getByTestId('dc-score')
    expect(score.textContent).toContain('45')
    expect(score.textContent).not.toContain('0nothing')
    expect(score.textContent).toMatch(/sent to an administrator/i)
  })

  it('still demands an explanation, so the server’s 422 is never a surprise', async () => {
    ensureReview.mockResolvedValue(FOUND_HEALTH_AND_PII)
    await renderModal()
    fireEvent.click(screen.getByTestId('dc-question-healthData-no'))
    fireEvent.click(screen.getByTestId('dc-question-personalInformation-no'))

    // The button used to read Publish here and submit straight into a 422.
    const button = confirmButton()
    expect(button.textContent).toContain('Send for review')
    expect(button.disabled).toBe(true)
    expect(screen.getByTestId('dc-warning').textContent).toMatch(/please explain/i)

    fireEvent.change(screen.getByTestId('dc-notes'), { target: { value: 'Names and emails, service desk only.' } })
    expect(confirmButton().disabled).toBe(false)
  })

  it('adds the developer’s own Yes to the check’s — either side may raise a flag', async () => {
    // Financial (20) from the developer, Confidential (15) from the check: 35 together.
    ensureReview.mockResolvedValue({
      ...BASE,
      verdicts: verdicts({
        confidentialBusinessData: { verdict: 'yes', reason: 'Internal fleet condition data.' },
      }),
    })
    await renderModal()
    expect(screen.getByTestId('dc-score').textContent).toContain('15')

    fireEvent.click(screen.getByTestId('dc-question-financialData-yes'))
    expect(screen.getByTestId('dc-score').textContent).toContain('35')

    // And it comes back down when the developer's own Yes is withdrawn — never below the
    // check's 15, which is not theirs to lower.
    fireEvent.click(screen.getByTestId('dc-question-financialData-no'))
    expect(screen.getByTestId('dc-score').textContent).toContain('15')
  })

  it('a check that answered No leaves the total entirely to the developer', async () => {
    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    await renderModal()
    const score = screen.getByTestId('dc-score')
    expect(score.textContent).toContain('0')
    expect(score.textContent).toMatch(/can publish without review/i)

    fireEvent.click(screen.getByTestId('dc-question-credentialsSecrets-yes'))
    expect(screen.getByTestId('dc-score').textContent).toContain('40')
  })
})

describe('the disagreement note says whose answer survives', () => {
  it('names the check’s Yes as the one on record when the developer says No', async () => {
    ensureReview.mockResolvedValue({
      ...BASE,
      verdicts: verdicts({
        personalInformation: { verdict: 'yes', reason: 'Names, emails and phone numbers.' },
      }),
    })
    await renderModal()
    fireEvent.click(screen.getByTestId('dc-question-personalInformation-no'))

    const note = screen.getByTestId('dc-disagreement-personalInformation').textContent ?? ''
    expect(note).toMatch(/found this in your code/i)
    expect(note).toMatch(/its yes is what goes on record/i)
    // The sentence this replaced claimed the developer's answer was kept. It is not:
    // a review Yes stands over a No, which is exactly what the administrator's screen
    // has always said ("The Yes stands"). The two screens now agree.
    expect(note).not.toMatch(/your answer is kept/i)
  })

  it('names the developer’s Yes as the one on record when the check says No', async () => {
    ensureReview.mockResolvedValue(COMPLETE_ALL_NO)
    await renderModal()
    fireEvent.click(screen.getByTestId('dc-question-financialData-yes'))

    const note = screen.getByTestId('dc-disagreement-financialData').textContent ?? ''
    expect(note).toMatch(/did not find this/i)
    expect(note).toMatch(/your yes is what goes on record/i)
  })

  it('gives the two directions DIFFERENT sentences', async () => {
    // The bug was one sentence serving both. If these ever collapse again, this fails.
    ensureReview.mockResolvedValue({
      ...BASE,
      verdicts: verdicts({
        personalInformation: { verdict: 'yes', reason: 'Names and emails.' },
      }),
    })
    await renderModal()
    fireEvent.click(screen.getByTestId('dc-question-personalInformation-no'))
    fireEvent.click(screen.getByTestId('dc-question-financialData-yes'))

    const overruled = screen.getByTestId('dc-disagreement-personalInformation').textContent
    const upheld = screen.getByTestId('dc-disagreement-financialData').textContent
    expect(overruled).not.toBe(upheld)
  })
})
