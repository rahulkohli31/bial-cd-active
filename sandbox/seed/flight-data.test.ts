/**
 * The pure helpers in `flight-data.ts`, asserted without Azure.
 *
 * WHY THESE AND NOT OTHERS. Every test below pins one of the seven mistakes the reference file
 * names, or one of the two rules that decide whether an app's numbers are right. They are the
 * reason the head of the generated file is allowed to say "verified": the traps are not just
 * described, they are demonstrated failing and then demonstrated fixed. Where a trap has a real
 * negative — the spread that overflows, the pooled buffer that hands the reader the wrong bytes
 * — the test proves the WRONG code is wrong too, because a helper that is merely green tells you
 * nothing about whether the trap it guards is real.
 *
 * The module reads its two coordinates at import time and throws when either is missing, so this
 * file supplies them before importing it. That ordering is itself the first assertion: it is
 * exactly what a citizen hits when the connector is off, and it must read as "switched off",
 * never as a DNS fault.
 *
 * Runner: vitest — see package.json. `npm test` from this directory.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

const URL_ENV = 'BIAL_DICE_URL'
const CLIENT_ENV = 'BIAL_DICE_CLIENT_ID'

process.env[URL_ENV] = 'https://bialdata.blob.core.windows.net/lake/flight/fact/'
process.env[CLIENT_ENV] = '11111111-2222-3333-4444-555555555555'

const {
  FLIGHT_KEY,
  FLIGHT_TIME,
  LOAD_TIME,
  appendAll,
  classify,
  currentRecordsOnly,
  flightsBetween,
  label,
  newestReadableDay,
  ownBytes,
  splitLakeUrl,
} = await import('./flight-data')

type Row = Record<string, unknown>

/** A load-date-stamped file name in the real object-name shape. */
const objectName = (yyyymmdd: string, month = 'SEPTEMBER'): string =>
  `flight/fact/2026/${month}/tb_flight_fact_report_${yyyymmdd}.parquet`

const lakeFile = (yyyymmdd: string, size = 900_000) => {
  const listed = classify(objectName(yyyymmdd), size)
  if (listed.kind !== 'file') throw new Error(`fixture is not a flight file: ${yyyymmdd}`)
  return listed.file
}

const flight = (key: string, loadedAt: string, scheduledAt: string, extra: Row = {}): Row => ({
  [FLIGHT_KEY]: key,
  [LOAD_TIME]: new Date(loadedAt),
  [FLIGHT_TIME]: new Date(scheduledAt),
  ...extra,
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ── The connection ───────────────────────────────────────────────────────────────────────────

describe('the connector being switched off', () => {
  it('fails with the reason, not with a DNS error', async () => {
    vi.resetModules()
    const held = process.env[URL_ENV]
    delete process.env[URL_ENV]
    try {
      await expect(import('./flight-data')).rejects.toThrow(
        /BIAL_DICE_URL is not set.*not switched on for this project/s,
      )
    } finally {
      process.env[URL_ENV] = held
    }
  })
})

describe('splitLakeUrl', () => {
  const expected = {
    endpoint: 'https://bialdata.blob.core.windows.net',
    container: 'lake',
    prefix: 'flight/fact/',
  }

  it('reads the same address with or without a trailing slash', () => {
    expect(splitLakeUrl('https://bialdata.blob.core.windows.net/lake/flight/fact/')).toEqual(
      expected,
    )
    expect(splitLakeUrl('https://bialdata.blob.core.windows.net/lake/flight/fact')).toEqual(
      expected,
    )
  })

  it('normalises the prefix to end in a slash, so a sibling folder cannot join the window', () => {
    // Without the added slash the prefix `flight` also lists `flight-archive/...`, whose files
    // carry the SAME name pattern and would be read as if they belonged here.
    const { prefix } = splitLakeUrl('https://bialdata.blob.core.windows.net/lake/flight')
    expect(prefix).toBe('flight/')
    expect(objectName('20260907').startsWith(prefix)).toBe(true)
    expect('flight-archive/2026/SEPTEMBER/x.parquet'.startsWith(prefix)).toBe(false)
  })

  it('leaves an empty prefix empty when the files sit at the container root', () => {
    expect(splitLakeUrl('https://bialdata.blob.core.windows.net/lake')).toEqual({
      endpoint: 'https://bialdata.blob.core.windows.net',
      container: 'lake',
      prefix: '',
    })
    expect(splitLakeUrl('https://bialdata.blob.core.windows.net/lake/').prefix).toBe('')
  })
})

// ── MISTAKE 3 — the directory that looks like a broken file ───────────────────────────────────

describe('classify', () => {
  it('drops a directory placeholder by its NAME, not by its zero length', () => {
    // A hierarchical-namespace flat listing returns directories as zero-length entries. Checking
    // the size first would call this one a broken file and hide the real ones behind the noise.
    expect(classify('flight/fact/2026/SEPTEMBER/', 0)).toEqual({ kind: 'not-a-flight-file' })
    expect(classify('flight/fact/2026/', 0)).toEqual({ kind: 'not-a-flight-file' })
  })

  it('reports a zero-byte file that DOES match the name as a failed load', () => {
    const name = objectName('20260903')
    expect(classify(name, 0)).toEqual({ kind: 'empty-stub', name })
  })

  it('keeps the two distinguishable — that is the whole point of the ordering', () => {
    const directory = classify('flight/fact/2026/SEPTEMBER/', 0)
    const stub = classify(objectName('20260903'), 0)
    expect(directory.kind).not.toBe(stub.kind)
  })

  it('reads the load date out of the file name', () => {
    const listed = classify(objectName('20260831'), 1_234)
    expect(listed.kind).toBe('file')
    if (listed.kind !== 'file') return
    expect(listed.file.loadDate.toISOString()).toBe('2026-08-31T00:00:00.000Z')
    expect(listed.file.size).toBe(1_234)
  })

  it('accepts the 31 August file from the SEPTEMBER folder — the month is the LOAD month', () => {
    // MISTAKE 2's fixture: a constructed `2026/08/...` path would have found nothing here.
    const listed = classify(objectName('20260831', 'SEPTEMBER'), 900_000)
    expect(listed.kind).toBe('file')
  })

  it.each([
    'flight/fact/2026/SEPTEMBER/old_tb_flight_fact_report_20260901.parquet',
    'flight/fact/2026/SEPTEMBER/backup_tb_flight_fact_report_20260901.parquet',
    'flight/fact/2026/SEPTEMBER/copy-of-tb_flight_fact_report_20260901.parquet',
  ])('rejects a PREFIXED name that would otherwise match as a suffix: %s', (name) => {
    // The regex is anchored on a path-segment boundary. Without the left anchor these all match,
    // an archived or hand-copied file joins the window, and every flight in it is counted twice —
    // silently, because the file parses perfectly well. Found in review; the platform's own
    // selector in backend/src/services/lake/window.py anchors the same way.
    expect(classify(name, 900_000)).toEqual({ kind: 'not-a-flight-file' })
  })

  it('still accepts a bare file name with no folder in front of it', () => {
    // The left anchor is `(?:^|/)`, not `/` — a name at the container root must still match, or
    // the anchor would fix one bug by introducing another.
    const listed = classify('tb_flight_fact_report_20260901.parquet', 900_000)
    expect(listed.kind).toBe('file')
  })
})

// ── The newest day you can actually read ─────────────────────────────────────────────────────

describe('newestReadableDay', () => {
  it('returns the newest file that survived the listing filter', () => {
    const days = newestReadableDay([lakeFile('20260901'), lakeFile('20260907'), lakeFile('20260903')])
    expect(days?.toISOString()).toBe('2026-09-07T00:00:00.000Z')
  })

  it('returns yesterday rather than today when today has not loaded yet', () => {
    // The lake runs a day behind. A computed `now - 1 day` would say the same thing here and be
    // WRONG on any day the load failed or the calendar is sparse — see the next test.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-08T09:00:00Z'))
      const newest = newestReadableDay([lakeFile('20260906'), lakeFile('20260907')])
      expect(newest?.toISOString()).toBe('2026-09-07T00:00:00.000Z')
      expect(newest?.getTime()).toBeLessThan(Date.now())
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not invent a day the lake never loaded', () => {
    // The sparse calendar: 6 September never landed, so on the 8th the newest readable day is the
    // 5th — two days back, not one. Arithmetic would have named a file that does not exist.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-08T09:00:00Z'))
      expect(newestReadableDay([lakeFile('20260904'), lakeFile('20260905')])?.toISOString()).toBe(
        '2026-09-05T00:00:00.000Z',
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('returns null for an empty listing instead of a date nobody can read', () => {
    expect(newestReadableDay([])).toBeNull()
  })
})

// ── MISTAKE 7 — grouping text without trimming it ────────────────────────────────────────────

describe('label', () => {
  it('collapses the padded duplicate onto the same key', () => {
    expect(label('AKASA AIR')).toBe('AKASA AIR')
    expect(label('AKASA AIR                     ')).toBe('AKASA AIR')
    expect(label('Indigo ')).toBe('Indigo')

    const counts = new Map<string, number>()
    for (const raw of ['AKASA AIR', 'AKASA AIR    ', 'STAR AIR', 'STAR AIR ']) {
      const key = label(raw) ?? 'Unknown'
      counts.set(key, (counts.get(key) ?? 0) + 1)
    }
    expect([...counts.keys()].sort()).toEqual(['AKASA AIR', 'STAR AIR'])
  })

  it('maps the empty string to absent — the case `!= null` lets through', () => {
    expect(label('')).toBeNull()
    expect(label('   ')).toBeNull()
    expect(label(null)).toBeNull()
    expect(label(undefined)).toBeNull()
    // The trap itself, stated as an assertion: the raw value passes a null check and then draws
    // an unlabelled bar on the chart.
    expect('' != null).toBe(true)
  })

  it('stringifies a non-string rather than dropping it', () => {
    expect(label(7)).toBe('7')
    expect(label(false)).toBe('false')
  })
})

// ── The amendment rows ───────────────────────────────────────────────────────────────────────

describe('currentRecordsOnly', () => {
  it('keeps the row with the highest load time for each flight', () => {
    const rows: Row[] = [
      flight('URNO-1', '2026-09-05T02:00:00Z', '2026-09-04T18:00:00Z', { STATUS: 'SCHEDULED' }),
      flight('URNO-1', '2026-09-07T02:00:00Z', '2026-09-04T18:00:00Z', { STATUS: 'LANDED' }),
      flight('URNO-1', '2026-09-06T02:00:00Z', '2026-09-04T18:00:00Z', { STATUS: 'AIRBORNE' }),
    ]
    const kept = currentRecordsOnly(rows)
    expect(kept).toHaveLength(1)
    expect(kept[0]?.STATUS).toBe('LANDED')
  })

  it('is order-independent — a later file may arrive first in the loop', () => {
    const newest = flight('URNO-1', '2026-09-07T02:00:00Z', '2026-09-04T18:00:00Z', { N: 2 })
    const older = flight('URNO-1', '2026-09-05T02:00:00Z', '2026-09-04T18:00:00Z', { N: 1 })
    expect(currentRecordsOnly([newest, older])[0]?.N).toBe(2)
    expect(currentRecordsOnly([older, newest])[0]?.N).toBe(2)
  })

  it('collapses a multi-amendment fixture to the flight count, not the row count', () => {
    // The measured shape, scaled down: 100 flights, 11 of them re-stated once by a later load.
    // 111 rows -> 100 flights. Counting the rows would report 11% more flights than the airport
    // operated, which is the measured 75,551 -> 67,147 overcount in miniature.
    const rows: Row[] = []
    for (let i = 0; i < 100; i += 1) {
      rows.push(flight(`URNO-${i}`, '2026-09-05T02:00:00Z', '2026-09-04T18:00:00Z', { rev: 1 }))
      if (i < 11) {
        rows.push(flight(`URNO-${i}`, '2026-09-06T02:00:00Z', '2026-09-04T18:00:00Z', { rev: 2 }))
      }
    }
    expect(rows).toHaveLength(111)
    const kept = currentRecordsOnly(rows)
    expect(kept).toHaveLength(100)
    expect(kept.filter((row) => row.rev === 2)).toHaveLength(11)
  })
})

// ── MISTAKE 5 — the departure time that is null on every arrival ─────────────────────────────

describe('flightsBetween', () => {
  const from = new Date('2026-09-01T00:00:00Z')
  const to = new Date('2026-09-30T23:59:59Z')

  it('keeps arrivals, whose SCHEDULED_OFF_BLOCK_TIME_SOBT is null', () => {
    // Half the table. Filtering on the departure column drops every one of these silently.
    const arrivals: Row[] = [
      flight('A-1', '2026-09-08T02:00:00Z', '2026-09-07T05:30:00Z', {
        ARR_DEP_FLG_ADID: 'A',
        SCHEDULED_OFF_BLOCK_TIME_SOBT: null,
      }),
      flight('A-2', '2026-09-08T02:00:00Z', '2026-09-07T06:10:00Z', {
        ARR_DEP_FLG_ADID: 'A',
        SCHEDULED_OFF_BLOCK_TIME_SOBT: null,
      }),
    ]
    expect(flightsBetween(arrivals, from, to)).toHaveLength(2)

    // ...and the mistake, so the guard above is not merely green. Filtering the same rows on the
    // departure column returns nothing at all, with no error anywhere.
    const wrong = arrivals.filter((row) => {
      const when = new Date(Number(row.SCHEDULED_OFF_BLOCK_TIME_SOBT))
      return when >= from && when <= to
    })
    expect(wrong).toHaveLength(0)
  })

  it('does not confuse the load date with the flight date', () => {
    // One measured file loaded in August 2026 carried flights scheduled from July 2022 to
    // October 2026. The file is in the window; the flight is not.
    const ancient = flight('OLD-1', '2026-09-08T02:00:00Z', '2022-07-14T09:00:00Z')
    const future = flight('FUT-1', '2026-09-08T02:00:00Z', '2026-10-30T09:00:00Z')
    const inside = flight('NOW-1', '2026-09-08T02:00:00Z', '2026-09-07T09:00:00Z')
    expect(flightsBetween([ancient, future, inside], from, to).map((r) => r[FLIGHT_KEY])).toEqual([
      'NOW-1',
    ])
  })

  it('includes both ends of the window', () => {
    const first = flight('E-1', '2026-09-08T02:00:00Z', from.toISOString())
    const last = flight('E-2', '2026-09-08T02:00:00Z', to.toISOString())
    expect(flightsBetween([first, last], from, to)).toHaveLength(2)
  })
})

// ── MISTAKE 6 — the spread that overflows the stack ──────────────────────────────────────────

describe('appendAll', () => {
  const chunk = Array.from({ length: 200_000 }, (_, i) => i)

  it('appends a backfill-sized chunk without overflowing the stack', () => {
    const rows: number[] = [-1]
    appendAll(rows, chunk)
    expect(rows).toHaveLength(200_001)
    expect(rows[200_000]).toBe(199_999)
  })

  it('proves the trap is real — the spread it replaces throws on the same input', () => {
    // Without this assertion the test above is just "an array works". `push(...chunk)` passes
    // 200,000 SEPARATE ARGUMENTS and the engine runs out of call stack. It works fine on an
    // ordinary day's file and fails only when a backfill lands.
    const rows: number[] = []
    expect(() => rows.push(...chunk)).toThrow(RangeError)
  })
})

// ── The pooled buffer the reader cannot parse ────────────────────────────────────────────────

describe('ownBytes', () => {
  it('slices a pooled view to its own bounds', () => {
    // Deliberately allocate the payload INSIDE a larger buffer, which is exactly what Node's
    // allocator does for you: a Buffer is a view, and `buffer.buffer` is the whole pool.
    const pool = Buffer.alloc(4_096, 0x7f)
    const payload = Buffer.from([0x50, 0x41, 0x52, 0x31]) // "PAR1", a parquet footer magic
    payload.copy(pool, 1_000)
    const view = pool.subarray(1_000, 1_000 + payload.byteLength)

    // The trap: the view's own ArrayBuffer is a thousand times too big and starts in the wrong
    // place, so a reader handed `view.buffer` looks for the footer at the end of the POOL.
    expect(view.buffer.byteLength).toBe(4_096)
    expect(view.byteOffset).toBe(1_000)

    const bytes = ownBytes(view)
    expect(bytes.byteLength).toBe(payload.byteLength)
    expect([...new Uint8Array(bytes)]).toEqual([...payload])
  })

  it('returns a copy, so the pool can be reused underneath it', () => {
    const pool = Buffer.alloc(64)
    const view = pool.subarray(8, 12)
    view.set([1, 2, 3, 4])
    const bytes = ownBytes(view)
    pool.fill(0)
    expect([...new Uint8Array(bytes)]).toEqual([1, 2, 3, 4])
  })
})
