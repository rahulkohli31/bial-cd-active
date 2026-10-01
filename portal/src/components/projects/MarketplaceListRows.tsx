/**
 * The Marketplace's list view: one table row per published application.
 *
 * Not `AppListRow`: that row opens an application inside the portal from its name button, while a
 * catalog entry opens the published application in a new tab from its own link.
 */
import { ExternalLink } from 'lucide-react'

import { initials } from './SharedAppRow'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../ui/table'
import type { MarketplaceEntry } from '../../utils/marketplaceApi'

export default function MarketplaceListRows({ entries }: { entries: MarketplaceEntry[] }): React.JSX.Element {
  return (
    <div className="bg-white border border-bial-border rounded-2xl overflow-hidden">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow className="border-b border-bial-border bg-bial-bg/60">
            <TableHead className="px-4 py-2.5">Application</TableHead>
            <TableHead className="hidden sm:table-cell w-[230px] px-4 py-2.5">Built by</TableHead>
            <TableHead className="w-32 px-4 py-2.5">
              <span className="sr-only">Open</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((entry, i) => (
            // Index-qualified so a duplicated entry renders twice instead of colliding as a key.
            <TableRow key={`${entry.url}-${i}`} data-testid="marketplace-entry" className="hover:bg-bial-bg/60">
              <TableCell className="px-4 py-3">
                <p className="truncate text-sm font-semibold text-tertiary" title={entry.name}>
                  {entry.name}
                </p>
                {entry.description ? (
                  <p className="truncate text-xs text-neutral mt-0.5" title={entry.description}>
                    {entry.description}
                  </p>
                ) : (
                  <p className="text-xs text-neutral/60 italic mt-0.5">No description yet.</p>
                )}
              </TableCell>
              <TableCell className="hidden sm:table-cell px-4 py-3">
                {entry.builderDisplayName && (
                  <span className="flex items-center gap-2 min-w-0">
                    <span
                      aria-hidden
                      className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-primary-50 text-[9px] font-bold text-primary-dark"
                    >
                      {initials(entry.builderDisplayName)}
                    </span>
                    <span className="truncate text-xs text-neutral" title={entry.builderDisplayName}>
                      {entry.builderDisplayName}
                    </span>
                  </span>
                )}
              </TableCell>
              <TableCell className="px-4 py-3 text-right">
                <a
                  data-testid="marketplace-open"
                  href={entry.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-semibold text-primary hover:underline"
                >
                  Open app
                  <ExternalLink size={13} />
                </a>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
