import { Database } from 'lucide-react'

/** The connector's teal tile, at the 26px the settings row draws. */
export function ConnectorGlyph(): React.JSX.Element {
  return (
    <span
      aria-hidden
      className="w-[26px] h-[26px] rounded-lg flex-shrink-0 bg-primary-50 border border-primary-100 inline-flex items-center justify-center"
    >
      <Database size={13} strokeWidth={1.8} className="text-primary-dark" />
    </span>
  )
}
