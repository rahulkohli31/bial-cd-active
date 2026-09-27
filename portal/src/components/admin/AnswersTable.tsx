import { cn } from '../../lib/utils'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../ui/table'
import type { JudgedClass } from './declaration'

const ANSWER_HEAD = 'py-2 pr-3 tracking-[0.6px]'
const ANSWER_CELL = 'py-2 pr-3'

function Answer({ yes, className }: { yes: boolean | null; className?: string }) {
  if (yes === null) return <span className="text-neutral">—</span>
  return <span className={cn(yes && 'font-bold', className)}>{yes ? 'Yes' : 'No'}</span>
}

/** Each class as it was judged, the agent's answer beside the owner's. The owner's column is a
 *  dash for a hard block, and for every class when owners could not change answers. */
export default function AnswersTable({ classes }: { classes: JudgedClass[] }) {
  return (
    <div className="overflow-hidden rounded-[10px] border border-bial-border">
      <Table className="table-fixed text-[12.5px] leading-[normal] text-primary-900">
        <TableHeader className="bg-bial-bg/60">
          <TableRow className="border-b border-bial-border">
            <TableHead className={cn(ANSWER_HEAD, 'pl-3.5')}>Class</TableHead>
            <TableHead className={cn(ANSWER_HEAD, 'w-[122px]')}>Kind</TableHead>
            <TableHead className={cn(ANSWER_HEAD, 'w-[102px]')}>Agent</TableHead>
            <TableHead className={cn(ANSWER_HEAD, 'w-[104px] pr-3.5')}>Owner</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {classes.map((entry) => (
            <TableRow key={entry.key} data-testid={`answer-${entry.key}`} className={cn(entry.changed && 'bg-amber-50')}>
              <TableCell className={cn(ANSWER_CELL, 'pl-3.5 font-semibold text-tertiary')}>{entry.title}</TableCell>
              <TableCell className={cn(ANSWER_CELL, 'text-neutral')}>
                {entry.kind === 'hard_block' ? 'Hard block' : `Scored${entry.weight === null ? '' : ` · ${entry.weight}`}`}
              </TableCell>
              <TableCell className={ANSWER_CELL}>
                <Answer yes={entry.agent} className={cn(entry.kind === 'hard_block' && entry.agent && 'text-red-700')} />
              </TableCell>
              <TableCell className={cn(ANSWER_CELL, 'pr-3.5')}>
                <Answer yes={entry.owner} className={cn(entry.changed && 'font-bold text-amber-700')} />
                {entry.changed && <span className="sr-only">changed by the owner</span>}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
