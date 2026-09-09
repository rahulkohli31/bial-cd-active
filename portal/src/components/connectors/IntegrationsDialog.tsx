/**
 * Integrations — the one door to connector access, opened from the profile menu (R5) and, from
 * U10, from `Manage integrations →` in the workspace rail. No new route, no Settings link, and no
 * navigation: it lands as a dialog over whatever the citizen was doing.
 *
 * ONE `Dialog` ROOT, THREE BODIES. This component owns the single `<Dialog open>` mount and swaps
 * its body between the connector list, the ask panel, and the projects drill-down — each
 * behind a back chevron. Building the ask as a second `<Dialog>` would unmount one Radix dialog
 * and mount another on every forward and back click: a double backdrop fade, and `dialog.tsx`'s
 * `useFocusBackstop()` firing twice, on what the boards draw as one continuous panel. The test
 * file pins it by asserting the same DOM node survives the transition.
 *
 * IT OWNS THE API, THE LOCK AND THE RELOAD. Every state change re-reads from the server rather
 * than patching a row from a write's response: the person's state is a derivation over their
 * remaining rows, and a client that assembled `pending` itself after a POST would be a second
 * copy of that rule. `loadSeq` discards a stale response — there is no query library in this
 * portal, and `AppRegistryPanel` is where this shape comes from.
 *
 * THE LIST IS THE REGISTRY, AND THE REGISTRY HAS ONE ENTRY. The boards draw a second, greyed
 * `[ANOTHER BIAL SYSTEM]` placeholder row; it is not built (owner ruling, 2026-09-08) and there is
 * no entry behind it. Rendering one anyway turns this file's first test red on purpose.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { notifyConnectorsChanged,
  cancelConnectorRequest,
  listConnectors,
  requestConnectorAccess,
} from '../../utils/connectorApi'
import type { ConnectorEntry } from '../../utils/connectorApi'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import ConnectorRow from './ConnectorRow'
import AskAccessPanel from './AskAccessPanel'
import ConnectorProjectsPanel from './ConnectorProjectsPanel'

/**
 * Which body is showing. The `ask` and `projects` arms carry their own entry rather than an index
 * into the list, so a reload underneath either panel cannot re-point it at a different connector.
 */
type DialogBody =
  | { view: 'list' }
  | { view: 'ask'; entry: ConnectorEntry }
  | { view: 'projects'; entry: ConnectorEntry }

/** The one id `DialogContent` describes itself with, whichever body is rendering it. */
const SUBTITLE_ID = 'integrations-dialog-subtitle'

const LIST_SUBTITLE =
  'Data BIAL already holds. An administrator gives you access once — every project you own can then use it.'

export interface IntegrationsDialogProps {
  /** Conditionally mounted by the caller, per this portal's dialog convention. */
  onClose: () => void
}

export default function IntegrationsDialog({
  onClose,
}: IntegrationsDialogProps): React.JSX.Element {
  const [entries, setEntries] = useState<ConnectorEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [body, setBody] = useState<DialogBody>({ view: 'list' })
  // WHICH connector is mid-write, not a shared boolean: the moment there is a second registry
  // entry, one row's cancel must not grey out the other row's button.
  const [busyKey, setBusyKey] = useState<string | null>(null)
  // A stale response must not overwrite fresher state — the ref-token variant of `let live`,
  // because `load` is also called imperatively after every write.
  const loadSeq = useRef(0)

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current
    setError(null)
    try {
      const rows = await listConnectors()
      if (loadSeq.current === seq) setEntries(rows)
    } catch (caught) {
      if (loadSeq.current === seq) {
        setError(caught instanceof Error ? caught.message : String(caught))
      }
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * Swaps the body to the projects drill-down.
   *
   * The handler lives here rather than on the row because the dialog is what owns its own body;
   * the row only knows it has a control to offer.
   */
  const openProjects = useCallback((entry: ConnectorEntry): void => {
    setBody({ view: 'projects', entry })
  }, [])

  /**
   * Coming BACK from the drill-down re-reads the list, and that is not tidiness. The row the
   * citizen just left says `On in N projects ›`, and N is exactly what the switches behind it
   * change — returning to a stale count would put the dialog's own two surfaces in disagreement
   * about a number one of them had just moved.
   */
  const backFromProjects = useCallback((): void => {
    setBody({ view: 'list' })
    void load()
  }, [load])

  const cancelRequest = useCallback(
    async (entry: ConnectorEntry): Promise<void> => {
      if (busyKey !== null) return
      setBusyKey(entry.key)
      try {
        await cancelConnectorRequest(entry.key)
        await load()
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setBusyKey(null)
      }
    },
    [busyKey, load],
  )

  /**
   * REJECTS ON FAILURE, deliberately. The ask panel renders the server's own sentence — the
   * duplicate-request 409 explains that an administrator is already looking at it, which a
   * generic failure message would throw away.
   */
  const submitAsk = useCallback(
    async (entry: ConnectorEntry, remarks: string): Promise<void> => {
      setBusyKey(entry.key)
      try {
        await requestConnectorAccess(entry.key, remarks)
        setBody({ view: 'list' })
        await load()
      } finally {
        setBusyKey(null)
      }
    },
    [load],
  )

  /**
   * THE ONE WAY OUT OF THIS DIALOG, and it does two things every exit needs.
   *
   * IT HONOURS THE IN-FLIGHT HOLD. The corner X used to call `onClose` directly while
   * `onOpenChange` held Escape and the overlay press — so the comment below claiming "the
   * header's X all arrive here" was false, and the one control a citizen is most likely to reach
   * for was the one that could close the dialog mid-ask. The request lands either way; a dialog
   * that vanished would leave them with no idea whether they had asked.
   *
   * IT ANNOUNCES THAT CONNECTOR STATE MAY HAVE MOVED. This dialog has two doors — the profile
   * menu, present on every authed screen, and `Manage integrations →` in the workspace rail — and
   * the drill-down inside it can switch the connector for the very project the rail is
   * describing. Only the rail's door knew to re-read on close, so entering from the avatar menu
   * left the rail asserting `Reading 30 days of flight data` about a project just switched off.
   * Signalling from HERE fixes both doors and any door added later, because the fact that a write
   * happened is the dialog's knowledge, not its opener's.
   */
  const close = useCallback((): void => {
    if (busyKey !== null) return
    notifyConnectorsChanged()
    onClose()
  }, [busyKey, onClose])

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // Escape and the overlay press arrive here; the header's X calls `close` itself. Both
        // routes go through the same hold — see `close` above.
        if (!next) close()
      }}
    >
      <DialogContent
        hideClose
        data-testid="integrations-dialog"
        // The board's softened scrim (`rgba(15,23,42,.16)` over a 3px blur), not shadcn's flat
        // `bg-black/80` — the same override every other dialog in this portal passes.
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        // `p-0 gap-0`: each body owns its own padding, because the list and the ask panel do not
        // share one (the ask panel's footer is full-bleed above a hairline).
        className="font-manrope w-full max-w-[640px] gap-0 rounded-2xl border-0 bg-white p-0 shadow-2xl"
        aria-describedby={SUBTITLE_ID}
      >
        {body.view === 'projects' ? (
          <ConnectorProjectsPanel
            entry={body.entry}
            onBack={backFromProjects}
            onClose={close}
          />
        ) : body.view === 'ask' ? (
          <AskAccessPanel
            entry={body.entry}
            busy={busyKey === body.entry.key}
            onBack={() => setBody({ view: 'list' })}
            onClose={close}
            onSubmit={(remarks) => submitAsk(body.entry, remarks)}
          />
        ) : (
          <div data-testid="connector-list-body">
            <div className="flex items-start gap-2.5 px-6 pt-[22px]">
              <div className="min-w-0 flex-1">
                <DialogTitle className="text-base font-extrabold tracking-[-0.2px] text-primary-900">
                  Integrations
                </DialogTitle>
                <p id={SUBTITLE_ID} className="mt-[5px] text-xs leading-[1.6] text-neutral">
                  {LIST_SUBTITLE}
                </p>
              </div>
              <button
                type="button"
                onClick={close}
                aria-label="Close"
                className="flex-shrink-0 p-0.5 text-neutral transition hover:text-primary-900"
              >
                <X size={17} />
              </button>
            </div>

            <div className="px-6 pb-5 pt-4">
              {error !== null && (
                <div
                  role="alert"
                  className="mb-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
                >
                  <p className="text-xs text-red-600">{error}</p>
                  <button
                    type="button"
                    onClick={() => void load()}
                    className="mt-1.5 text-xs font-semibold text-primary underline-offset-2 hover:underline"
                  >
                    Try again
                  </button>
                </div>
              )}

              {entries === null ? (
                error === null && (
                  // A skeleton, never a blank gap: this list is one row tall, so an empty box
                  // between the subtitle and the bottom edge reads as "nothing is connected"
                  // rather than as "still loading".
                  <div
                    data-testid="connector-list-loading"
                    className="rounded-xl border border-bial-border bg-white px-[13px] py-3"
                  >
                    <div className="flex items-center gap-2.5">
                      <span className="h-[26px] w-[26px] flex-shrink-0 animate-pulse rounded-lg bg-canvas-tile" />
                      <div className="min-w-0 flex-1">
                        <span className="block h-3 w-24 animate-pulse rounded bg-canvas-tile" />
                        <span className="mt-1.5 block h-2.5 w-40 animate-pulse rounded bg-canvas-tile" />
                      </div>
                    </div>
                    <span className="sr-only">Loading your integrations…</span>
                  </div>
                )
              ) : entries.length === 0 ? (
                <p className="rounded-xl border border-bial-border px-[13px] py-3 text-[11.5px] text-neutral">
                  Nothing is connected to the platform yet.
                </p>
              ) : (
                <ul className="divide-y divide-bial-border overflow-hidden rounded-xl border border-bial-border bg-white">
                  {entries.map((entry) => (
                    <ConnectorRow
                      key={entry.key}
                      entry={entry}
                      busy={busyKey === entry.key}
                      onRequestAccess={() => setBody({ view: 'ask', entry })}
                      onCancelRequest={() => void cancelRequest(entry)}
                      onOpenProjects={() => openProjects(entry)}
                    />
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
