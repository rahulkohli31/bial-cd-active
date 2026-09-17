/**
 * The two dates a row and a tile show. Formatting is the whole of this module, so the tests are
 * about the cases where a SHORTER form stops being merely terse and starts being wrong: a stamp
 * that will not parse, and a range whose two ends are in different years.
 */
import { describe, it, expect } from 'vitest'
import { dayMonth, listDate, tileDateRange, tileDateTitle, versionStamp } from '../projectDates'

const NOV_2025 = '2025-11-11T09:00:00Z'
const SEP_2026 = '2026-09-14T09:00:00Z'
const AUG_2026 = '2026-08-28T09:00:00Z'

describe('a date that cannot be read says so, rather than printing the words', () => {
  it.each([null, undefined, '', 'not-a-date'])('%s renders as an em dash', (bad) => {
    // `Invalid Date` in a column is worse than a column admitting it has nothing: the first is
    // a fact nobody can act on dressed as a value, and it is what `new Date()` hands back.
    expect(listDate(bad)).toBe('—')
    expect(dayMonth(bad)).toBe('—')
  })

  it('…and a range with one unreadable end still shows the end it has', () => {
    expect(tileDateRange(null, AUG_2026)).toBe('— → 28 Aug')
  })
})

describe('the list carries the year, the tile drops it', () => {
  it('spells the month rather than numbering it, and never abbreviates September to Sept', () => {
    // `Intl` spells it `Sept` in exactly the locales BIAL's browsers are set to, which is why
    // the months do not come from there.
    expect(listDate(SEP_2026)).toBe('14 Sep 2026')
    expect(dayMonth(SEP_2026)).toBe('14 Sep')
  })

  it('writes a same-year range in the tile form the board draws', () => {
    expect(tileDateRange(AUG_2026, SEP_2026)).toBe('28 Aug → 14 Sep')
  })
})

describe('★ a range that crosses a year does not read as an arrow pointing backwards', () => {
  it('brings the year back, and only then', () => {
    // WHAT THIS PREVENTS: an application made in November and touched the following September
    // rendered as `11 Nov → 14 Sep`. Not terse — wrong, and wrong precisely on the oldest
    // applications a person owns.
    expect(tileDateRange(NOV_2025, SEP_2026)).toBe('11 Nov 2025 → 14 Sep 2026')
    // The other half, and what makes the assertion above mean something: the long form is not
    // simply always on.
    expect(tileDateRange(AUG_2026, SEP_2026)).not.toMatch(/2026 →/)
  })
})

describe('the arrow does not say which end is which, so the hover does', () => {
  it('names both dates in full', () => {
    // A tile has no room for the column headings the list carries; without this there is nothing
    // anywhere on a tile saying which of the two dates is the one it was made on.
    expect(tileDateTitle(NOV_2025, SEP_2026)).toBe(
      'Created 11 Nov 2025 · Details updated 14 Sep 2026',
    )
  })
})

describe('versionStamp', () => {
  it('names today and yesterday, and dates anything older', () => {
    const now = new Date()
    now.setHours(14, 2, 0, 0)
    expect(versionStamp(now.toISOString())).toBe('Today, 14:02')

    const yesterday = new Date(now)
    yesterday.setDate(yesterday.getDate() - 1)
    yesterday.setHours(9, 41, 0, 0)
    expect(versionStamp(yesterday.toISOString())).toBe('Yesterday, 09:41')

    // Older than yesterday: the day and month, and the clock alongside it — two saves on one
    // day is the ordinary case, so a date alone could name both.
    const older = new Date(now)
    older.setDate(older.getDate() - 16)
    older.setHours(9, 41, 0, 0)
    expect(versionStamp(older.toISOString())).toMatch(/^\d{1,2} \w{3}, 09:41$/)
  })

  it('renders an unparseable stamp as a dash rather than Invalid Date', () => {
    expect(versionStamp('not-a-date')).toBe('—')
    expect(versionStamp(null)).toBe('—')
  })
})
