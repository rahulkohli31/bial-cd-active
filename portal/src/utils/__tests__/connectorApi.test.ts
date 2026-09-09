/**
 * The connector client's PARSE contract, for the fields no component test can reach.
 *
 * WHY THIS MODULE IS STRICTER THAN `marketplaceApi`, and where the line sits. The catalog drops
 * an unreadable row and renders the rest, because one bad app must not blank a list of hundreds.
 * This list is the WHOLE of what Integrations offers: dropping its only row would render
 * "nothing is connected", which is false. So a row we cannot read throws — and these tests pin
 * exactly WHICH fields make a row unreadable, because the module deliberately does not treat
 * them all alike (`subtitle` is a label and may be absent; the ask panel's copy may not be).
 *
 * THE COPY FIELDS ARE THE POINT. `askSubtitle` and `consentLinesRequester` carry what the ask
 * panel says about a system, so that adding a second connector is a registry entry and not a
 * component change. A tolerant parse would trade a loud contract break for a silent one: a
 * consent box with a heading and no promises under it, in front of somebody about to ask.
 */
import { describe, it, expect, vi } from 'vitest'
import { listConnectors } from '../connectorApi'
import { ApiError } from '../apiError'

const deps = (fetchImpl: unknown) =>
  ({ fetchImpl, getToken: () => null, refresh: vi.fn() }) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({
  ...init,
  clone: () => res(init),
})
const ok = (json: unknown) => res({ ok: true, status: 200, json: async () => json })

const CONSENT = [
  { lead: 'Read-only.', body: 'Nothing you build can change ORBIT data.' },
  { lead: 'One dataset.', body: 'The Flight Fact Report. Nothing else in ORBIT.' },
]

const ENTRY = {
  key: 'orbit',
  displayName: 'ORBIT',
  subtitle: 'Airport operations',
  askSubtitle: 'ORBIT is BIAL’s airport operations data. An administrator decides who may read it.',
  consentLinesRequester: CONSENT,
  state: 'neverAsked',
  askedAt: null,
  approvedAt: null,
  approvedByName: null,
  onProjectCount: null,
  decidedAt: null,
  decidedByName: null,
  decisionRemarks: null,
}

const listWith = async (entry: Record<string, unknown>): Promise<unknown> =>
  listConnectors(deps(vi.fn(async () => ok({ connectors: [entry] }))))

describe('the ask panel copy survives the parse intact', () => {
  it('carries the subtitle and every consent line, in the order the server sent them', async () => {
    const [row] = await listConnectors(
      deps(vi.fn(async () => ok({ connectors: [ENTRY] }))),
    )

    expect(row.askSubtitle).toBe(ENTRY.askSubtitle)
    expect(row.consentLinesRequester).toEqual(CONSENT)
    // The row's own four-word label is a DIFFERENT field, not a truncation of the sentence.
    expect(row.subtitle).toBe('Airport operations')
  })
})

describe('a row whose copy cannot be read is a contract break, not a thinner row', () => {
  it('throws when the ask subtitle is missing — unlike the subtitle, which may be', async () => {
    // The contrast IS the test: both are strings on the same object and the module treats them
    // differently on purpose. A parse that made `askSubtitle` optional would ship a panel whose
    // opening sentence is blank, which says nothing about what is being asked for.
    const { askSubtitle: _dropped, ...noSubtitleSentence } = ENTRY
    await expect(listWith(noSubtitleSentence)).rejects.toBeInstanceOf(ApiError)

    const { subtitle: _label, ...noLabel } = ENTRY
    const [row] = await listConnectors(
      deps(vi.fn(async () => ok({ connectors: [noLabel] }))),
    )
    expect(row.subtitle).toBe('')
  })

  it('throws on an EMPTY consent list — a heading with no promises under it is not a panel', async () => {
    await expect(listWith({ ...ENTRY, consentLinesRequester: [] })).rejects.toBeInstanceOf(ApiError)
  })

  it('throws on a half-formed line rather than rendering the promises that did arrive', async () => {
    // Dropping the bad line would leave the citizen reading one promise of two with nothing on
    // screen admitting the other went missing — the failure mode strictness exists to prevent.
    await expect(
      listWith({ ...ENTRY, consentLinesRequester: [CONSENT[0], { lead: 'One dataset.' }] }),
    ).rejects.toBeInstanceOf(ApiError)
  })
})
