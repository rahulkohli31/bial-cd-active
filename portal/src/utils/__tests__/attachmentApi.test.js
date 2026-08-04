import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  uploadAttachment,
  fetchAttachmentObjectUrl,
  revokeAttachmentObjectUrl,
  revokeAllAttachmentUrls,
  AttachmentCapError,
} from '../attachmentApi'

// authFetch deps injection — no real token/network.
const deps = (fetchImpl) => ({ fetchImpl, getToken: () => 'tok', refresh: vi.fn() })

// Stub the object-URL APIs (jsdom doesn't implement them); make each createObjectURL unique.
let urlSeq = 0
beforeEach(() => {
  urlSeq = 0
  // Stub the object-URL APIs FIRST so revokeAllAttachmentUrls (which calls
  // URL.revokeObjectURL) uses the stub, not jsdom's incomplete URL.
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => `blob:mock-${urlSeq++}`), revokeObjectURL: vi.fn() })
  revokeAllAttachmentUrls() // clear any cached urls from a prior test
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('uploadAttachment', () => {
  it('POSTs the file and returns the attachment ref', async () => {
    const attachment = { attachmentId: 'a1', key: 'att/u/a1', kind: 'image', name: 'd.png', mediaType: 'image/png', size: 99 }
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201, json: async () => ({ attachment }) }))
    const ref = await uploadAttachment({ attachmentId: 'a1', name: 'd.png', mediaType: 'image/png', size: 99, base64: 'AAAA' }, deps(fetchImpl))
    expect(ref).toEqual(attachment)
    const [url, opts] = fetchImpl.mock.calls[0]
    expect(url).toBe('/api/attachments')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toMatchObject({ attachmentId: 'a1', mediaType: 'image/png', base64: 'AAAA' })
  })

  it('throws AttachmentCapError on a cap rejection (code ATTACHMENT_STORE_FULL)', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: false,
      status: 413,
      json: async () => ({ error: { message: 'Attachment storage is full.', code: 'ATTACHMENT_STORE_FULL' } }),
    }))
    await expect(uploadAttachment({ attachmentId: 'a1', mediaType: 'image/png', base64: 'AA' }, deps(fetchImpl))).rejects.toBeInstanceOf(AttachmentCapError)
  })

  it('throws a generic Error with the server message on other failures', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 400, json: async () => ({ error: { message: 'bad bytes' } }) }))
    await expect(uploadAttachment({ attachmentId: 'a1', mediaType: 'image/png', base64: 'AA' }, deps(fetchImpl))).rejects.toThrow('bad bytes')
  })
})

describe('fetchAttachmentObjectUrl — cache + revoke', () => {
  it('caches by id: a second call returns the same URL without refetching', async () => {
    const blob = { type: 'image/png' }
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, blob: async () => blob }))
    const url1 = await fetchAttachmentObjectUrl('a1', deps(fetchImpl))
    const url2 = await fetchAttachmentObjectUrl('a1', deps(fetchImpl))
    expect(url1).toBe(url2)
    expect(fetchImpl).toHaveBeenCalledTimes(1) // second call served from cache

    revokeAttachmentObjectUrl('a1') // releasing it forces a refetch next time
    const url3 = await fetchAttachmentObjectUrl('a1', deps(fetchImpl))
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(url3).not.toBe(url1)
  })

  it('returns null on a missing/forbidden object (no crash)', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }))
    expect(await fetchAttachmentObjectUrl('gone', deps(fetchImpl))).toBeNull()
  })

  it('coalesces concurrent calls for the same id onto one fetch (no double-GET, no leaked blob URL)', async () => {
    // Gate the fetch so BOTH callers reach the cache-miss before either resolves —
    // the StrictMode double-mount / same-image-in-two-chips race.
    let release
    const gate = new Promise((r) => { release = r })
    const fetchImpl = vi.fn(async () => {
      await gate
      return { ok: true, status: 200, blob: async () => ({ type: 'image/png' }) }
    })
    const p1 = fetchAttachmentObjectUrl('a1', deps(fetchImpl))
    const p2 = fetchAttachmentObjectUrl('a1', deps(fetchImpl))
    release()
    const [url1, url2] = await Promise.all([p1, p2])
    expect(url1).toBe(url2) // same URL handed to both callers
    expect(fetchImpl).toHaveBeenCalledTimes(1) // one GET, not two
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1) // one blob URL — none orphaned/leaked
  })
})
