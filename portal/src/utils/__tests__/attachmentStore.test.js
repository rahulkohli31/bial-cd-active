import { describe, it, expect, vi } from 'vitest'
import {
  partsToText,
  attachmentsFromParts,
  countAttachments,
  wireMessageFromParts,
  buildUserParts,
  releaseUploadedAttachments,
  decodeBase64Text,
} from '../attachmentStore'

const b64Utf8 = (s) => Buffer.from(s, 'utf8').toString('base64')

const textAttachmentPart = (name, content) => ({
  type: 'text',
  text: content,
  attachment: { attachmentId: `${name}-id`, name, mediaType: name.endsWith('.csv') ? 'text/csv' : 'text/plain', size: content.length },
})
const imagePart = (id) => ({ type: 'file', attachmentId: id, key: `att/u/${id}`, kind: 'image', mediaType: 'image/png', name: `${id}.png`, size: 10 })

describe('partsToText', () => {
  it('joins prose text parts and ignores file + inline-attachment parts', () => {
    const parts = [imagePart('a'), textAttachmentPart('roster.csv', 'x,y'), { type: 'text', text: 'hello' }, { type: 'text', text: 'world' }]
    expect(partsToText(parts)).toBe('hello\nworld')
  })
  it('accepts a raw string defensively', () => {
    expect(partsToText('legacy assistant text')).toBe('legacy assistant text')
  })
})

describe('attachmentsFromParts', () => {
  it('extracts file + inline-text descriptors (for AttachmentChips)', () => {
    const parts = [imagePart('img1'), textAttachmentPart('d.csv', 'a,b'), { type: 'text', text: 'caption' }]
    expect(attachmentsFromParts(parts)).toEqual([
      { attachmentId: 'img1', kind: 'image', name: 'img1.png', mediaType: 'image/png' },
      { attachmentId: 'd.csv-id', kind: 'text', name: 'd.csv', mediaType: 'text/csv' },
    ])
  })
})


const PPTX_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
const deckPart = (extra = {}) => ({
  type: 'file', kind: 'deck', mediaType: PPTX_TYPE, attachmentId: 'd1', key: 'att/u/d1',
  name: 'q3.pptx', size: 1234, pdfFileId: 'file_d1', pageCount: 12, ...extra,
})

describe('countAttachments', () => {
  it('sums file + inline-text attachment parts across turns', () => {
    const messages = [
      { role: 'user', parts: [imagePart('1'), textAttachmentPart('r.csv', 'a'), { type: 'text', text: 'hi' }] },
      { role: 'assistant', parts: [{ type: 'text', text: 'ok' }] },
      { role: 'user', parts: [imagePart('2'), { type: 'text', text: 'more' }] },
    ]
    expect(countAttachments(messages)).toBe(3)
  })
  it('is 0 for empty / attachment-free / non-array', () => {
    expect(countAttachments([])).toBe(0)
    expect(countAttachments([{ role: 'user', parts: [{ type: 'text', text: 'x' }] }])).toBe(0)
    expect(countAttachments(null)).toBe(0)
  })
})

describe('wireMessageFromParts — the U7 stateless wire message', () => {
  it('an image part becomes an OWNED REF, never bytes (the server rehydrates at send)', () => {
    const message = wireMessageFromParts([imagePart('img9'), { type: 'text', text: 'look' }])
    expect(message).toEqual({ text: 'look', attachmentIds: ['img9'] })
    expect(JSON.stringify(message)).not.toContain('base64')
  })

  it('an inline text attachment rides as a fence block alongside the prose', () => {
    const message = wireMessageFromParts([
      textAttachmentPart('d.csv', 'a,b\n1,2'),
      imagePart('img'),
      { type: 'text', text: 'turn 1' },
    ])
    expect(message.text).toBe('turn 1')
    expect(message.attachmentTexts).toHaveLength(1)
    expect(message.attachmentTexts[0]).toContain('<attachment name="d.csv" type="text">')
    expect(message.attachmentTexts[0]).toContain('a,b\n1,2')
    expect(message.attachmentIds).toEqual(['img'])
  })

  it('a turn with only prose → bare {text} (no empty arrays on the wire)', () => {
    expect(wireMessageFromParts([{ type: 'text', text: 'just text' }])).toEqual({ text: 'just text' })
  })
})

describe('decodeBase64Text', () => {
  it('round-trips multibyte + strips a leading BOM (not bare atob)', () => {
    expect(decodeBase64Text(b64Utf8('﻿café,€,日本'))).toBe('café,€,日本')
  })
})

describe('buildUserParts', () => {
  it('inlines text attachments and uploads binaries, prose text last', async () => {
    const upload = vi.fn(async (a) => ({ attachmentId: a.attachmentId, key: `att/u/${a.attachmentId}`, kind: 'image', name: a.name, mediaType: a.mediaType, size: a.size }))
    const pending = [
      { id: 'csv1', name: 'r.csv', mediaType: 'text/csv', size: 5, base64: b64Utf8('a,b\n1') },
      { id: 'img1', name: 'p.png', mediaType: 'image/png', size: 10, base64: 'AAAA' },
    ]
    const parts = await buildUserParts('analyze these', pending, upload)
    expect(parts[0]).toEqual({ type: 'text', text: 'a,b\n1', attachment: { attachmentId: 'csv1', name: 'r.csv', mediaType: 'text/csv', size: 5 } })
    expect(parts[1]).toMatchObject({ type: 'file', attachmentId: 'img1', kind: 'image', mediaType: 'image/png' })
    expect(parts[2]).toEqual({ type: 'text', text: 'analyze these' })
    expect(upload).toHaveBeenCalledTimes(1) // only the binary uploaded
  })

  it('propagates an upload failure so the caller can abort the send', async () => {
    const upload = vi.fn(async () => {
      throw new Error('cap hit')
    })
    await expect(buildUserParts('x', [{ id: 'i', name: 'p.png', mediaType: 'image/png', size: 1, base64: 'AA' }], upload)).rejects.toThrow('cap hit')
  })
})

describe('releaseUploadedAttachments', () => {
  it('deletes every file part (passing pdfFileId only for decks) and ignores non-file parts', () => {
    const del = vi.fn(async () => {})
    const parts = [
      deckPart({ attachmentId: 'd1', pdfFileId: 'file_d1' }),
      imagePart('img1'),
      { type: 'text', text: 'prose' },
      textAttachmentPart('r.csv', 'a,b'),
    ]
    releaseUploadedAttachments(parts, del)
    expect(del).toHaveBeenCalledTimes(2) // deck + image; text parts skipped
    expect(del).toHaveBeenCalledWith('d1', { pdfFileId: 'file_d1' })
    expect(del).toHaveBeenCalledWith('img1', { pdfFileId: undefined })
  })

  it('swallows a delete rejection (best-effort, never throws into the send path)', () => {
    const del = vi.fn(async () => {
      throw new Error('gone')
    })
    expect(() => releaseUploadedAttachments([imagePart('x')], del)).not.toThrow()
  })

  it('is a no-op for a non-array', () => {
    const del = vi.fn()
    releaseUploadedAttachments(null, del)
    expect(del).not.toHaveBeenCalled()
  })
})

/**
 * THE PRODUCERS ARE GONE — inertness, not absence (R46, L8).
 *
 * `attachmentStore` used to MINT deck parts under a comment claiming the server converted them,
 * while the wire builder dropped them again a hundred lines away. Both went with the office
 * producer and the media types that fed them. What replaces five office cases and two deck cases
 * is the guarantee that neither part shape can be produced at all — a producer nothing consumes is
 * exactly the residual a removal is meant to close.
 */
describe('no conversion-dependent part can be produced', () => {
  it('buildUserParts mints no office or deck part for any accepted type', async () => {
    const upload = vi.fn(async (a) => ({
      attachmentId: a.attachmentId, key: 'k/' + a.attachmentId, name: a.name,
      mediaType: a.mediaType, size: a.size,
      kind: a.mediaType.startsWith('image/') ? 'image' : 'document',
    }))
    const parts = await buildUserParts(
      'here you go',
      [
        { id: '1', name: 'shot.png', mediaType: 'image/png', size: 10, base64: 'AAA' },
        { id: '2', name: 'plan.pdf', mediaType: 'application/pdf', size: 10, base64: 'AAA' },
      ],
      upload,
    )
    const kinds = parts.filter((p) => p.type === 'file').map((p) => p.kind)
    expect(kinds).not.toContain('office')
    expect(kinds).not.toContain('deck')
    // Liveness: the accepted types still produce their parts, so the two absences mean
    // "narrowed" and not "the builder stopped working".
    expect(kinds).toEqual(['image', 'document'])
  })

  it('the wire builder has no deck arm left to exercise', () => {
    const wire = wireMessageFromParts([
      { type: 'file', kind: 'image', attachmentId: 'a1', key: 'k', name: 'x.png', mediaType: 'image/png', size: 1 },
      { type: 'text', text: 'hello' },
    ])
    expect(wire.attachmentIds).toEqual(['a1'])
    expect(wire.text).toBe('hello')
    expect(JSON.stringify(wire)).not.toMatch(/deck|pdfFileId|pageCount/)
  })
})
