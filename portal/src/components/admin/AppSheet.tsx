import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { fetchHistory } from '../../utils/appRegistryApi'
import type { AppHistory, HistoryVersion, RegistryApp } from '../../utils/appRegistryApi'
import { Button } from '../ui/button'
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from '../ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../ui/tabs'
import AppHistoryTab from './AppHistoryTab'
import AppReviewTab from './AppReviewTab'

const BODY = 'flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-6 py-4'
const TAB =
  'rounded-none border-b-2 border-transparent px-0 pb-[9px] pt-3 text-[13px] font-medium text-neutral shadow-none ' +
  'data-[state=active]:border-primary data-[state=active]:font-bold data-[state=active]:text-primary'

interface AppSheetProps {
  app: RegistryApp
  /** What the app is called on screen. */
  title: string
  /** The app's registry status, as its row draws it. */
  status: ReactNode
  withdrawn: string | null
  /** Why the last Approve or Reject failed, or null. */
  problem: string | null
  onClose: () => void
  onApprove: () => Promise<void>
  onReject: (note: string) => Promise<void>
}

/**
 * One app's side panel, floating over the list. A pending app opens on Review, with History beside
 * it; any other app has nothing to decide, so History is the whole panel.
 *
 * Focus lands on the panel itself rather than its first control, and Radix's own restore is off:
 * the panel is unmounted rather than closed, and the list puts focus back on the row that opened it.
 */
export default function AppSheet({ app, title, status, withdrawn, problem, onClose, onApprove, onReject }: AppSheetProps) {
  const [history, setHistory] = useState<AppHistory | null>(null)
  const [error, setError] = useState<string | null>(null)
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let live = true
    fetchHistory(app.appId)
      .then((next) => {
        if (live) setHistory(next)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      live = false
    }
  }, [app.appId])

  const reviewed = history?.entries.find(
    (entry): entry is HistoryVersion =>
      entry.kind === 'version' && entry.submissionId !== null && entry.submissionId === app.submissionId,
  )
  const historyTab = <AppHistoryTab history={history} error={error} />

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose() }}>
      <SheetContent
        side="right"
        hideClose
        overlayClassName="bg-slate-900/[0.22]"
        ref={panel}
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          panel.current?.focus()
        }}
        onCloseAutoFocus={(e) => e.preventDefault()}
        className="inset-y-3 right-3 flex h-auto w-[760px] max-w-[calc(100vw-24px)] flex-col gap-0 overflow-hidden rounded-2xl border border-bial-border p-0 font-manrope shadow-[0_25px_50px_-12px_rgba(0,0,0,0.3)] focus:outline-none sm:max-w-[760px]"
      >
        <div className="flex items-start gap-3 px-6 pt-5">
          <div className="min-w-0 flex-grow">
            <div className="flex items-center gap-2.5">
              <SheetTitle className="text-lg font-bold text-tertiary">{title}</SheetTitle>
              {status}
            </div>
            <SheetDescription className="mt-1 text-[12.5px] text-neutral">
              {app.ownerUsername ?? 'Owner not recorded'}
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <Button variant="ghost" size="icon" aria-label="Close" className="h-[30px] w-[30px] rounded-lg text-neutral">
              <X size={16} />
            </Button>
          </SheetClose>
        </div>

        {app.status === 'pending' ? (
          <Tabs defaultValue="review" className="flex min-h-0 flex-1 flex-col">
            <TabsList className="mt-2.5 flex justify-start gap-[22px] border-b border-bial-border px-6">
              <TabsTrigger value="review" className={TAB}>
                Review
              </TabsTrigger>
              <TabsTrigger value="history" className={TAB}>
                History
              </TabsTrigger>
            </TabsList>
            <TabsContent value="review" className="mt-0 flex min-h-0 flex-1 flex-col data-[state=inactive]:hidden">
              <AppReviewTab
                app={app}
                number={reviewed?.number ?? null}
                withdrawn={withdrawn}
                problem={problem}
                onClose={onClose}
                onApprove={onApprove}
                onReject={onReject}
              />
            </TabsContent>
            <TabsContent value="history" className={`mt-0 ${BODY} data-[state=inactive]:hidden`}>
              {historyTab}
            </TabsContent>
          </Tabs>
        ) : (
          <>
            <h3 className="mt-1 border-b border-bial-border px-6 pb-2.5 pt-3.5 text-[10.5px] font-bold uppercase tracking-[0.6px] text-neutral">
              History
            </h3>
            <div className={BODY}>{historyTab}</div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
