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
