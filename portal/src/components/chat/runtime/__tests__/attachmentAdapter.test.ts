/**
 * THE ADAPTER'S CLAIM LIST, tested where the composer cannot reach.
 *
 * The caps are exercised end to end in `ComposerBox.test.tsx`; what a mounted composer cannot do
 * is hold one file's read OPEN while the citizen acts on another — every browser gesture starts
 * and settles its reads together, a window no `fireEvent` can stop inside.
 *
 * The adapter takes its staged list as a function, which is exactly the seam that window needs:
 * this suite drives `add`/`remove` directly and moves the staged list by hand, so the claim
 * lifecycle can be read one step at a time.
 */
import { describe, it, expect } from 'vitest'
import type { Attachment, PendingAttachment } from '@assistant-ui/react'

import { ACCEPT_ATTR } from '../../../../utils/attachmentInput'
import { createAttachmentAdapter } from '../attachmentAdapter'

/** A text file of `kb` kilobytes. Text is what the byte budget counts, so it is what these use. */
const sheet = (name: string, kb: number) =>
  new File([new Uint8Array(kb * 1024)], name, { type: 'text/csv' })

function makeAdapter() {
  const staged: Attachment[] = []
  const refusals: string[] = []
  const adapter = createAttachmentAdapter({
    accept: ACCEPT_ATTR,
    staged: () => staged,
    onRefused: (message) => refusals.push(message),
  })
  return { adapter, staged, refusals }
}

/**
 * The library types `add` as "a promise OR an async generator"; ours is always the promise. This
 * narrows to the one shape without a cast, and says so out loud if that ever stops being true.
 */
async function settle(added: ReturnType<ReturnType<typeof makeAdapter>['adapter']['add']>): Promise<PendingAttachment> {
  const attachment = await added
  if (!('id' in attachment)) throw new Error('This adapter returns a promise, never a generator.')
  return attachment
}

describe('★ a claim is given back when the citizen takes the chip back', () => {
  it('stops counting a removed file while another read is still out', async () => {
    // A claim is retired inside `countable()`, which runs only when the NEXT file arrives — so a
    // removed file's slot in the cap and its bytes in the 512 KB text budget stay spent for as
    // long as any read is still in flight. Mutation receipt: drop the `claimed.delete` from
    // `remove` and this refuses the third file at 600 KB, over a file that is not there.
    const { adapter, staged, refusals } = makeAdapter()

    // Both reads start in the same tick, which is what keeps the second one open below.
    const first = adapter.add({ file: sheet('january.csv', 200) })
    const second = adapter.add({ file: sheet('february.csv', 200) })

    const january = await settle(first)
    staged.push(january)
    // February is still being read while January is removed.
    staged.length = 0
    await adapter.remove(january)

    const march = await settle(adapter.add({ file: sheet('march.csv', 200) }))

    expect(refusals).toEqual([])
    expect(march.name).toBe('march.csv')
    await second
  })

  it('still refuses the file that genuinely does not fit', async () => {
    // THE OTHER HALF, and the reason the release above is not simply 'count less'. Nothing is
    // removed here, so every file is real and the last one is over a cap that still exists.
    //
    // The cap CHANGED: the 512 KB cumulative text budget went with the inline lane -
    // a spreadsheet is an uploaded file now and never enters the prompt - so what bounds a
    // gesture is the per-message FILE COUNT. The release must not have turned that into a
    // suggestion either, which is the invariant this test has always been about.
    const { adapter, staged, refusals } = makeAdapter()

    for (let i = 0; i < 5; i += 1) {
      staged.push(await settle(adapter.add({ file: sheet(`m${i}.csv`, 10) })))
    }

    await expect(adapter.add({ file: sheet('sixth.csv', 10) })).rejects.toThrow(/at most 5 files/)
    expect(refusals).toHaveLength(1)
  })
})
