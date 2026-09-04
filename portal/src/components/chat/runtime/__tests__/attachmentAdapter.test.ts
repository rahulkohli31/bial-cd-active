/**
 * THE ADAPTER'S CLAIM LIST, tested where the composer cannot reach.
 *
 * The caps are exercised end to end in `ComposerBox.test.tsx`, through real drops on a real
 * runtime, and that is where they belong. What a mounted composer cannot do is hold one file's
 * read OPEN while the citizen acts on another — every gesture in a browser starts its reads
 * together and settles them together, so the window in which the claim list is the only thing
 * counting is a window no `fireEvent` can stop inside.
 *
 * The adapter takes its staged list as a function, which is exactly the seam that window needs:
 * this suite drives `add` and `remove` directly and moves the staged list by hand, so the claim
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
    // THE TRANSIENT THIS IS WRITTEN AGAINST. A claim is retired inside `countable()`, which runs
    // only when the NEXT file arrives — so between the composer taking a file and anything else
    // being attached, the claim and the staged file are two records of one file. That is harmless
    // until the citizen removes the chip: the file leaves the staged list, the claim does not, and
    // its slot in the cap and its bytes in the text budget stay spent for as long as any read is
    // still running.
    //
    // 512 KB is the whole conversation's text budget. Two 200 KB sheets are in the composer's
    // hands, one of them is taken back, and a third must therefore fit. Mutation receipt: drop the
    // `claimed.delete` from `remove` and this refuses it at 600 KB, over a file that is not there.
    const { adapter, staged, refusals } = makeAdapter()

    // Both reads start in the same tick, which is what keeps the second one open below.
    const first = adapter.add({ file: sheet('january.csv', 200) })
    const second = adapter.add({ file: sheet('february.csv', 200) })

    // January lands and the composer is holding it…
    const january = await settle(first)
    staged.push(january)
    // …and the citizen takes it straight back off, while February is still being read.
    staged.length = 0
    await adapter.remove(january)

    const march = await settle(adapter.add({ file: sheet('march.csv', 200) }))

    expect(refusals).toEqual([])
    expect(march.name).toBe('march.csv')
    await second
  })

  it('still refuses the file that genuinely does not fit', async () => {
    // THE OTHER HALF, and the reason the release above is not simply "count less". Nothing is
    // removed here, so all three sheets are real and the third is over the budget — the release
    // must not have turned the text budget into a suggestion.
    const { adapter, staged, refusals } = makeAdapter()

    const first = adapter.add({ file: sheet('january.csv', 200) })
    const second = adapter.add({ file: sheet('february.csv', 200) })
    staged.push(await settle(first))

    await expect(adapter.add({ file: sheet('march.csv', 200) })).rejects.toThrow(/512 KB total limit/)
    expect(refusals).toHaveLength(1)
    await second
  })
})
