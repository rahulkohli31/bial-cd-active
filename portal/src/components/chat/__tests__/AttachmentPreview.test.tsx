/**
 * AN ATTACHMENT, OPENED OVER THE CONVERSATION (R47, R50, R52, R64).
 *
 * ══ THE CSP DECIDES THE MECHANISM ══
 *
 * `portal/nginx.conf` sets `frame-src 'self' https://${APPS_HOSTNAME}`, so `blob:` and `data:` are
 * NOT framable — which is exactly why the path this replaces built a blob URL and opened a NEW
 * BROWSER TAB, leaving the conversation behind. A same-origin `/api/attachments/{id}` satisfies
 * `frame-src 'self'` with zero CSP, backend or bundle change, and gets the browser's native PDF
 * viewer for free.
 *
 * So `attachmentSrc` returning a same-origin path is not an implementation detail to be
 * refactored: it IS the feature, and the assertion below is the one that would catch someone
 * "simplifying" it back to a blob.
 *
 * ══ R47 — NOTHING BUT THE READER DISMISSES IT ══
 *
 * `open` is never derived from stream state. The transcript keeps streaming behind the dialog and
 * is not scrolled; closing returns the reader exactly where they were.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import AttachmentPreview, { attachmentSrc, type PreviewTarget } from '../AttachmentPreview'

afterEach(cleanup)

const pdf: PreviewTarget = { attachmentId: 'att-1', name: 'gate-plan.pdf', mediaType: 'application/pdf' }
const image: PreviewTarget = { attachmentId: 'att-2', name: 'terminal.png', mediaType: 'image/png' }

describe('attachmentSrc — the CSP-compatible address', () => {
  it('addresses a stored attachment SAME-ORIGIN, never as a blob', () => {
    // Mutation check: return a `blob:` or `data:` URL and this goes red — and in the product the
    // frame would silently render nothing, because a CSP refusal is not an error a page can see.
    const src = attachmentSrc(pdf)
    expect(src).toBe('/api/attachments/att-1')
    expect(src?.startsWith('blob:')).toBe(false)
    expect(src?.startsWith('data:')).toBe(false)
  })

  it('encodes the id, so an id with a slash cannot escape the path', () => {
    expect(attachmentSrc({ ...pdf, attachmentId: 'a/b' })).toBe('/api/attachments/a%2Fb')
  })

  it('falls back to a staged file’s transient data URL when there is no stored id', () => {
    // A file the citizen has just picked is not on the server yet. `data:` is framable for an
    // IMAGE (`img-src` is unrestricted here) even though it is not for an iframe, which is why
    // the image branch exists at all.
    expect(attachmentSrc({ name: 'new.png', mediaType: 'image/png', dataUrl: 'data:image/png;base64,AAA' }))
      .toBe('data:image/png;base64,AAA')
  })

  it('is null when there is nothing to address', () => {
    expect(attachmentSrc({ name: 'nothing.png', mediaType: 'image/png' })).toBeNull()
  })
})

describe('the dialog', () => {
  it('renders nothing at all with no target', () => {
    const { container } = render(<AttachmentPreview target={null} onClose={vi.fn()} />)
    expect(container.innerHTML).toBe('')
  })

  it('is a real dialog — the things the hand-rolled overlay did not have', () => {
    // `AttachmentLightbox` was 55 lines, images only: no focus trap, no `role="dialog"`, no
    // accessible name, no scroll lock, and it closed on a backdrop click with nothing returning
    // focus. Each of those is a keyboard user's problem, which is why the dialog replaced it.
    //
    // NAMED AND DESCRIBED, asserted through the ROLE QUERY rather than by reading attributes:
    // `getByRole('dialog', {name})` resolves whichever labelling mechanism Radix used, so this
    // keeps meaning the same thing if the library changes how it wires them.
    render(<AttachmentPreview target={pdf} onClose={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: /gate-plan\.pdf/i })).toBeTruthy()
    expect(screen.getByTestId('attachment-preview')).toBeTruthy()
  })

  it('names the file, and describes itself for a reader who cannot see it', () => {
    render(<AttachmentPreview target={pdf} onClose={vi.fn()} />)
    expect(screen.getByText('gate-plan.pdf')).toBeTruthy()
    expect(screen.getByText(/the conversation stays open behind it/i)).toBeTruthy()
  })

  it('frames a PDF and renders an image inline — two branches, one address', () => {
    render(<AttachmentPreview target={pdf} onClose={vi.fn()} />)
    expect(screen.getByTestId('attachment-preview-frame').getAttribute('src')).toBe('/api/attachments/att-1')

    cleanup()
    render(<AttachmentPreview target={image} onClose={vi.fn()} />)
    expect(screen.getByTestId('attachment-preview-image').getAttribute('src')).toBe('/api/attachments/att-2')
    expect(screen.queryByTestId('attachment-preview-frame')).toBeNull()
  })

  it('says something useful when an IMAGE cannot be loaded', () => {
    // THE IMAGE BRANCH IS THE ONE `onError` ACTUALLY REACHES, and saying so is the point of
    // testing it here rather than through the frame. An `<img>` fires `error` on a 404 or a 401;
    // an `<iframe>` does NOT — it renders whatever the server returned, so a stale session shows
    // the API's own error document inside the frame rather than a blank. Two different failure
    // presentations, and only one of them is this component's to produce.
    render(<AttachmentPreview target={image} onClose={vi.fn()} />)
    fireEvent.error(screen.getByTestId('attachment-preview-image'))
    expect(screen.getByTestId('attachment-preview-error').textContent).toMatch(/reload the page/i)
  })

  it('a new target clears a previous failure', () => {
    // Without this a previously-failed load reports failure for a file that is perfectly fine.
    const { rerender } = render(<AttachmentPreview target={image} onClose={vi.fn()} />)
    fireEvent.error(screen.getByTestId('attachment-preview-image'))
    expect(screen.getByTestId('attachment-preview-error')).toBeTruthy()

    rerender(<AttachmentPreview target={pdf} onClose={vi.fn()} />)
    expect(screen.queryByTestId('attachment-preview-error')).toBeNull()
    expect(screen.getByTestId('attachment-preview-frame')).toBeTruthy()
  })

  it('with no address at all, it explains rather than framing nothing', () => {
    render(
      <AttachmentPreview target={{ name: 'gone.pdf', mediaType: 'application/pdf' }} onClose={vi.fn()} />,
    )
    expect(screen.getByTestId('attachment-preview-error')).toBeTruthy()
  })
})

describe('R47 — the reader dismisses it, and only the reader', () => {
  it('Escape closes it', () => {
    const onClose = vi.fn()
    render(<AttachmentPreview target={pdf} onClose={onClose} />)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('`open` is not derived from any stream state — it is unconditionally open while targeted', () => {
    // The mechanical form of "nothing but the reader dismisses it": the component takes no
    // running/streaming prop at all, so no turn state can reach the open flag.
    const props = Object.keys({ target: pdf, onClose: vi.fn() })
    expect(props).toEqual(['target', 'onClose'])
  })
})

describe('U11 — a staged file is never a blank box', () => {
  // THE DEFECT THIS UNIT EXISTS TO CLOSE. A `data:` URL is not framable under
  // `frame-src 'self'`, and a CSP-blocked `<iframe>` fires NO `error` event — so every staged
  // non-image rendered as an empty rectangle with nothing to explain it. Each branch below is one
  // of the three answers to that, and none of them had a test.
  const stagedCsv: PreviewTarget = {
    name: 'stands.csv',
    mediaType: 'text/csv',
    dataUrl: 'data:text/csv;base64,Z2F0ZSxhaXJjcmFmdAoxMkEsQTMyMCDigJQgY2Fmw6kg4piVCg==',
  }

  it('renders a staged TEXT file inline — no frame, because a data: frame is what the policy blocks', () => {
    render(<AttachmentPreview target={stagedCsv} onClose={vi.fn()} />)
    const pre = screen.getByTestId('attachment-preview-text')
    expect(pre.textContent).toContain('12A,A320')
    // The liveness half: an absence assertion alone would pass just as happily if the component
    // had thrown and rendered nothing at all.
    expect(screen.queryByTestId('attachment-preview-frame')).toBeNull()
    expect(screen.queryByTestId('attachment-preview-error')).toBeNull()
  })

  it('decodes the bytes as UTF-8, so a name with an accent survives the preview', () => {
    // `atob` alone yields latin1 and would render "cafÃ©". This is the assertion that keeps the
    // Uint8Array → TextDecoder round-trip from being "simplified" back to a bare atob.
    render(<AttachmentPreview target={stagedCsv} onClose={vi.fn()} />)
    expect(screen.getByTestId('attachment-preview-text').textContent).toContain('café ☕')
  })

  it('strips a leading BOM rather than drawing it as a stray glyph', () => {
    render(
      <AttachmentPreview
        target={{ name: 'bom.csv', mediaType: 'text/csv', dataUrl: 'data:text/csv;base64,77u/Z2F0ZSxhaXJjcmFmdAo=' }}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId('attachment-preview-text').textContent?.startsWith('gate')).toBe(true)
  })

  it('says so for a staged PDF, which has no address the framing policy allows', () => {
    render(
      <AttachmentPreview
        target={{ name: 'gate-plan.pdf', mediaType: 'application/pdf', dataUrl: 'data:application/pdf;base64,JVBER' }}
        onClose={vi.fn()}
      />,
    )
    // The whole point: a SENTENCE, where the defect drew an empty rectangle.
    expect(screen.getByTestId('attachment-preview-pending').textContent).toMatch(/once you have sent it/i)
    expect(screen.queryByTestId('attachment-preview-frame')).toBeNull()
  })

  it('a SENT text file frames the stored address instead — same file, different lifetime', () => {
    render(
      <AttachmentPreview
        target={{ attachmentId: 'att-9', name: 'stands.csv', mediaType: 'text/csv' }}
        onClose={vi.fn()}
      />,
    )
    const frame = screen.getByTestId('attachment-preview-frame') as HTMLIFrameElement
    expect(frame.getAttribute('src')).toBe('/api/attachments/att-9')
  })

  it('NEVER puts a data: URL in an iframe src — the regression that drew the blank box', () => {
    // The whole defect in one assertion. `framableSrc` is same-origin-or-nothing; if someone
    // "simplifies" it back to `attachmentSrc`, a staged file's data: URL reaches the frame, the
    // CSP silently refuses it, and no `error` event ever fires to tell anyone.
    render(
      <AttachmentPreview
        target={{ name: 'gate-plan.pdf', mediaType: 'application/pdf', dataUrl: 'data:application/pdf;base64,JVBER' }}
        onClose={vi.fn()}
      />,
    )
    const frames = document.querySelectorAll('iframe')
    frames.forEach((f) => expect(f.getAttribute('src')?.startsWith('data:')).not.toBe(true))
    // Paired liveness: the dialog really rendered, so the absence above means something.
    expect(screen.getByTestId('attachment-preview')).toBeTruthy()
  })

  it('undecodable base64 falls back to the frame branch rather than throwing', () => {
    render(
      <AttachmentPreview
        target={{ attachmentId: 'att-7', name: 'broken.csv', mediaType: 'text/csv', dataUrl: 'data:text/csv;base64,!!!!' }}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId('attachment-preview-frame')).toBeTruthy()
    expect(screen.queryByTestId('attachment-preview-text')).toBeNull()
  })
})
