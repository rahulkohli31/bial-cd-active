import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import AttachmentChips from '../AttachmentChips.jsx'

// AttachmentChips is the SHARED persisted chip (used on every page, incl. the
// reloaded conversation view). A text attachment must take the labelled-icon
// branch — not the image branch — otherwise a reloaded chat shows a broken <img>.
// The image byte read lives in an effect (not run under SSR), so the structural
// contrast below (text/pdf → no <img>; image → <img>) proves the branches exist.
describe('AttachmentChips — descriptor branches', () => {
  it('renders a text/CSV attachment as a labelled chip: filename shown, no <img>', () => {
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[{ attachmentId: 't1', kind: 'text', name: 'roster.csv', mediaType: 'text/csv' }]} />,
    )
    expect(html).toContain('roster.csv')
    expect(html).not.toContain('<img')
  })


  it('renders a PDF as a clickable labelled chip, no <img>', () => {
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[{ attachmentId: 'd1', kind: 'document', name: 'spec.pdf', mediaType: 'application/pdf' }]} />,
    )
    expect(html).toContain('spec.pdf')
    expect(html).not.toContain('<img')
  })

  it('contrast: an image attachment still goes down the <img> path', () => {
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[{ attachmentId: 'i1', kind: 'image', name: 'shot.png', mediaType: 'image/png' }]} />,
    )
    expect(html).toContain('<img')
  })

  it('renders a Word office attachment as a clickable chip (filename, no <img>)', () => {
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[{ attachmentId: 'w1', kind: 'office', format: 'word', name: 'plan.docx', mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }]} />,
    )
    expect(html).toContain('plan.docx')
    expect(html).toContain('<button')
    expect(html).not.toContain('<img')
  })


})

describe('AttachmentChips — deck (.pptx) chip', () => {
  const PPTX_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  const deck = { attachmentId: 'd1', kind: 'deck', name: 'q3.pptx', mediaType: PPTX_TYPE }

  it('renders a clickable chip with the .pptx filename and the Presentation icon (no <img>)', () => {
    const html = renderToStaticMarkup(<AttachmentChips attachments={[deck]} />)
    expect(html).toContain('q3.pptx')
    expect(html).toContain('<button')
    expect(html).not.toContain('<img')
    expect(html).toContain('lucide-presentation') // not file-text / file-spreadsheet
    expect(html).not.toContain('lucide-file-spreadsheet')
    expect(html).not.toContain('lucide-file-text')
  })

  it('never reveals the PDF conversion in the chip (invisible conversion)', () => {
    const html = renderToStaticMarkup(<AttachmentChips attachments={[deck]} />)
    expect(html.toLowerCase()).not.toContain('pdf')
    expect(html).toContain('Download q3.pptx') // the tooltip is about the .pptx
  })

  it('office (word/excel) chips are unchanged — they do NOT use the Presentation icon', () => {
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[{ attachmentId: 'w1', kind: 'office', format: 'word', name: 'plan.docx', mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }]} />,
    )
    expect(html).not.toContain('lucide-presentation')
  })
})

// --- pressing a chip does something, and says what -------------------------

describe('every chip is a control that says which file and what it does', () => {
  const chip = (over) => ({
    attachmentId: 'a1', kind: 'file', name: 'roster.xlsx',
    mediaType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ...over,
  })

  it('★ a code-lane chip is a BUTTON, not the dead span it used to be', () => {
    // THE DEFECT. A text chip was a `<span>` with a `title` and no handler: pressing it did
    // nothing at all, because the content rode inline in the prompt and there was nothing to
    // fetch. Every attachment is an uploaded file now, so there always is — and a control that
    // absorbs a press silently is the one thing a control must never be.
    const html = renderToStaticMarkup(<AttachmentChips attachments={[chip()]} />)

    expect(html).toContain('<button')
    expect(html).toContain('roster.xlsx')
    expect(html).not.toContain('<img')
  })

  it('★ names the file AND what pressing it does, which is the whole of R23c', () => {
    // A chip that downloads and a chip that opens are the same shape to a screen reader. Without
    // the verb, both announce as "roster.xlsx" and the citizen cannot tell them apart.
    const download = renderToStaticMarkup(<AttachmentChips attachments={[chip()]} />)
    const open = renderToStaticMarkup(
      <AttachmentChips attachments={[chip({ kind: 'document', name: 'spec.pdf', mediaType: 'application/pdf' })]} />,
    )
    const view = renderToStaticMarkup(
      <AttachmentChips attachments={[chip({ kind: 'image', name: 'gate.png', mediaType: 'image/png' })]} />,
    )

    expect(download).toContain('aria-label="Download roster.xlsx"')
    expect(open).toContain('aria-label="Open spec.pdf"')
    expect(view).toContain('aria-label="View gate.png"')
  })

  it('routes each of the five code-lane formats to the download branch', () => {
    // The kind comes from the server (`chip_kind_for`), but the media type is matched too, so a
    // part staged in the composer — which assigns its own kind locally — behaves identically.
    for (const mediaType of [
      'text/csv',
      'text/tab-separated-values',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    ]) {
      const html = renderToStaticMarkup(
        <AttachmentChips attachments={[chip({ kind: undefined, mediaType, name: `f-${mediaType.slice(-6)}` })]} />,
      )
      expect(html, mediaType).toContain('<button')
      expect(html, mediaType).not.toContain('<img')
    }
  })

  it('an image is still a thumbnail, so the code lane did not swallow the model lane', () => {
    // The liveness half: every assertion above is about the absence of `<img>`, and all of them
    // would pass just as happily against a component that had stopped rendering images at all.
    const html = renderToStaticMarkup(
      <AttachmentChips attachments={[chip({ kind: 'image', name: 'gate.png', mediaType: 'image/png' })]} />,
    )

    expect(html).toContain('<img')
  })
})
