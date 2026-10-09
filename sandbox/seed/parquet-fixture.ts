/**
 * Small parquet files for the reader's tests, written by hand: the four packages the module uses
 * only read parquet, and this workspace installs nothing else.
 *
 * The simplest shape the reader accepts: uncompressed, PLAIN-encoded, every column REQUIRED, one
 * data page per column chunk. Each inner array handed to `parquetFile` becomes one row group, so a
 * test decides exactly where one range ends and the next begins.
 */

export type Column = { name: string; kind: 'text' | 'time' }

type Row = Record<string, unknown>

const I32 = 5
const I64 = 6
const BINARY = 8
const LIST = 9
const STRUCT = 12

type Value =
  | { type: typeof I32 | typeof I64; value: number }
  | { type: typeof BINARY; value: string }
  | { type: typeof LIST; of: number; items: Value[] }
  | { type: typeof STRUCT; fields: Array<[number, Value]> }

const i32 = (value: number): Value => ({ type: I32, value })
const i64 = (value: number): Value => ({ type: I64, value })
const text = (value: string): Value => ({ type: BINARY, value })
const list = (of: number, items: Value[]): Value => ({ type: LIST, of, items })
const struct = (...fields: Array<[number, Value]>): Value => ({ type: STRUCT, fields })

const PHYSICAL = { text: 6, time: 2 } // BYTE_ARRAY, INT64
const CONVERTED = { text: 0, time: 9 } // UTF8, TIMESTAMP_MILLIS
const REQUIRED = 0
const DATA_PAGE = 0
const PLAIN = 0
const RLE = 3
const UNCOMPRESSED = 0
const MAGIC = new TextEncoder().encode('PAR1')

function append(out: number[], bytes: Uint8Array): void {
  for (const byte of bytes) out.push(byte)
}

function varint(out: number[], value: bigint): void {
  let rest = value
  while (rest >= 0x80n) {
    out.push(Number(rest & 0x7fn) | 0x80)
    rest >>= 7n
  }
  out.push(Number(rest))
}

/** Thrift's compact protocol, for the few types parquet's metadata needs. */
function encode(out: number[], node: Value): void {
  switch (node.type) {
    case I32:
    case I64: {
      const value = BigInt(node.value)
      varint(out, value >= 0n ? value << 1n : (-value << 1n) - 1n)
      return
    }
    case BINARY: {
      const bytes = new TextEncoder().encode(node.value)
      varint(out, BigInt(bytes.length))
      append(out, bytes)
      return
    }
    case LIST: {
      const size = node.items.length
      if (size < 15) out.push((size << 4) | node.of)
      else {
        out.push(0xf0 | node.of)
        varint(out, BigInt(size))
      }
      for (const item of node.items) encode(out, item)
      return
    }
    case STRUCT: {
      let previous = 0
      for (const [id, field] of node.fields) {
        out.push(((id - previous) << 4) | field.type)
        encode(out, field)
        previous = id
      }
      out.push(0)
    }
  }
}

function plain(column: Column, rows: readonly Row[]): number[] {
  const out: number[] = []
  for (const row of rows) {
    const value = row[column.name]
    if (column.kind === 'time') {
      const bytes = new Uint8Array(8)
      new DataView(bytes.buffer).setBigInt64(0, BigInt((value as Date).getTime()), true)
      append(out, bytes)
    } else {
      const bytes = new TextEncoder().encode(String(value))
      const length = new Uint8Array(4)
      new DataView(length.buffer).setUint32(0, bytes.length, true)
      append(out, length)
      append(out, bytes)
    }
  }
  return out
}

/** A parquet file holding `groups`, one row group per inner array, in order. */
export function parquetFile(
  columns: readonly Column[],
  groups: readonly (readonly Row[])[],
): Uint8Array {
  const out: number[] = []
  append(out, MAGIC)
  const rowGroups: Value[] = []
  let rowCount = 0

  for (const rows of groups) {
    const groupStart = out.length
    const chunks: Value[] = []
    for (const column of columns) {
      const data = plain(column, rows)
      const offset = out.length
      encode(
        out,
        struct(
          [1, i32(DATA_PAGE)],
          [2, i32(data.length)],
          [3, i32(data.length)],
          [5, struct([1, i32(rows.length)], [2, i32(PLAIN)], [3, i32(RLE)], [4, i32(RLE)])],
        ),
      )
      for (const byte of data) out.push(byte)
      const size = out.length - offset
      chunks.push(
        struct(
          [2, i64(offset)],
          [
            3,
            struct(
              [1, i32(PHYSICAL[column.kind])],
              [2, list(I32, [i32(PLAIN)])],
              [3, list(BINARY, [text(column.name)])],
              [4, i32(UNCOMPRESSED)],
              [5, i64(rows.length)],
              [6, i64(size)],
              [7, i64(size)],
              [9, i64(offset)],
            ),
          ],
        ),
      )
    }
    rowGroups.push(
      struct([1, list(STRUCT, chunks)], [2, i64(out.length - groupStart)], [3, i64(rows.length)]),
    )
    rowCount += rows.length
  }

  const schema = [
    struct([4, text('schema')], [5, i32(columns.length)]),
    ...columns.map((column) =>
      struct(
        [1, i32(PHYSICAL[column.kind])],
        [3, i32(REQUIRED)],
        [4, text(column.name)],
        [6, i32(CONVERTED[column.kind])],
      ),
    ),
  ]
  const footer: number[] = []
  encode(
    footer,
    struct(
      [1, i32(1)],
      [2, list(STRUCT, schema)],
      [3, i64(rowCount)],
      [4, list(STRUCT, rowGroups)],
    ),
  )
  for (const byte of footer) out.push(byte)
  const length = new Uint8Array(4)
  new DataView(length.buffer).setUint32(0, footer.length, true)
  append(out, length)
  append(out, MAGIC)
  return Uint8Array.from(out)
}
