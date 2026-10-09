/**
 * Small parquet files for the reader's tests: uncompressed, PLAIN-encoded, no dictionary pages and
 * every column required, so a test can compute where each column chunk lies. Each inner array
 * handed to `parquetFile` becomes one row group, so a test decides exactly where one range ends and
 * the next begins.
 */
import { parquetWriteBuffer } from 'hyparquet-writer'

export type Column = { name: string; kind: 'text' | 'time' }

type Row = Record<string, unknown>

/** A parquet file holding `groups`, one row group per inner array, in order. */
export function parquetFile(
  columns: readonly Column[],
  groups: readonly (readonly Row[])[],
): Uint8Array {
  const rows = groups.flat()
  const buffer = parquetWriteBuffer({
    columnData: columns.map((column) => ({
      name: column.name,
      data: rows.map((row) => row[column.name]),
      type: column.kind === 'time' ? 'TIMESTAMP' : 'STRING',
      nullable: false,
      encoding: 'PLAIN',
    })),
    rowGroupSize: groups.length > 0 ? groups.map((group) => group.length) : 1,
    codec: 'UNCOMPRESSED',
    statistics: false,
  })
  return new Uint8Array(buffer)
}
