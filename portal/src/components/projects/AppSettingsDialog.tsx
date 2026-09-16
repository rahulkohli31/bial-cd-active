import { useState } from 'react'
import { Trash2, X } from 'lucide-react'
import { motion } from 'motion/react'
import { patchProject } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'
import { countWords, MAX_PROJECT_NAME_WORDS } from '../../utils/words'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../ui/tabs'
import { BusyGlyph } from '../ui/Waiting'
import { DURATION, LAYOUT_EASE } from '../../lib/motion'
import IntegrationsTab from './IntegrationsTab'
import ProductionTab from './ProductionTab'
import ProjectDescriptionEditor from './ProjectDescriptionEditor'
import { SharePanelBody } from './SharePanel'

/**
 * ONE DIALOG FOR AN APPLICATION'S SETTINGS, opened from the home row's `⋯` and from inside the
 * application itself. It is the drain the workspace rail and the toolbar empty into: what used to
 * be scattered across a permanent rail, a toolbar and a profile menu is four tabs here.
 *
 * IT IS AN OVERLAY, NOT A DESTINATION, and that is load-bearing rather than cosmetic. Opening it
 * from inside a running application tears no sandbox down and costs no cold start — which is why
 * the per-application Integrations tab exists at all beside the Integrations page: a citizen who
 * wants their connector switch never has to leave to reach it.
 *
 * IT SURVIVES THE NARROW WIDTHS THE REST OF THE PORTAL SURVIVES. This product's standing promise
 * is that every control stays reachable at 360px, and the workspace toolbar carries a
 * scroll-on-overflow fix precisely because that promise was once broken. Below the stacking
 * threshold the tab rail becomes a horizontal strip above the body and the panel goes full-bleed;
 * no tab and no control becomes unreachable.
 */

/** The title truncates rather than pushing the close control off a fixed-width panel — the same
 *  treatment the row and the toolbar already give a long application name. */
export type SettingsTab = 'general' | 'sharing' | 'integrations' | 'production'

export interface AppSettingsDialogProps {
  project: Project
  onProjectUpdate: (project: Project) => void
  onClose: () => void
  /** Opening straight onto a tab, for the doors that mean a particular one. */
  initialTab?: SettingsTab
  /** Opens the delete confirmation. The dialog collects nothing itself: deleting is the page's,
   *  which owns the cascade, its 404-versus-500 reconciliation and the list underneath. */
  onDelete: () => void
  /** Told when a production operation settles, so the list underneath can stop showing a chip
   *  that is now out of date. */
  onProductionSettled?: () => void
}

const TABS: { value: SettingsTab; label: string }[] = [
  { value: 'general', label: 'General' },
  { value: 'sharing', label: 'Sharing' },
  { value: 'integrations', label: 'Integrations' },
  { value: 'production', label: 'Production' },
]

/** The VARCHAR(120) column width — a paste backstop, not the rule a person is told about. */
const NAME_MAX_CHARS = 120

export default function AppSettingsDialog({
  project,
  onProjectUpdate,
  onClose,
  initialTab = 'general',
  onDelete,
  onProductionSettled,
}: AppSettingsDialogProps) {
  const [tab, setTab] = useState<SettingsTab>(initialTab)
  const [name, setName] = useState(project.name)
  const [nameError, setNameError] = useState<string | null>(null)
  const [savingName, setSavingName] = useState(false)

  const trimmed = name.trim()
  const words = countWords(name)
  // A LEGACY NAME OVER THE CAP MUST NOT OPEN ALREADY REFUSING. The word rule is not retroactive,
  // so a stored nine-word name keeps working; only a name the citizen has actually changed is
  // measured against it.
  const unchanged = trimmed === project.name
  const tooManyWords = words > MAX_PROJECT_NAME_WORDS && !unchanged

  const saveName = () => {
    // A second press while the first is in flight does nothing, and the check lives here rather
    // than on the control: the control carries `aria-disabled` and never `disabled`, because a
    // disabled control throws focus to the document body.
    if (savingName) return
    if (trimmed === '') {
      setNameError('Name cannot be empty.')
      return
    }
    if (unchanged) return
    if (tooManyWords) {
      setNameError('Keep the title short — about 6 to 8 words.')
      return
    }
    setNameError(null)
    setSavingName(true)
    void (async () => {
      try {
        onProjectUpdate(await patchProject(project.id, { name: trimmed }))
      } catch (err) {
        // THE CITIZEN'S TEXT SURVIVES A FAILED SAVE. Resetting the field to the stored value
        // here would throw away what they typed at the exact moment they most want it back.
        setNameError(err instanceof ApiError ? err.message : 'Could not save. Try again.')
      } finally {
        setSavingName(false)
      }
    })()
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !savingName) onClose()
      }}
    >
      <DialogContent
        hideClose
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="font-manrope w-full max-w-3xl gap-0 rounded-2xl border-0 bg-white p-0 shadow-2xl"
        data-testid="app-settings-dialog"
      >
        <div className="flex items-start justify-between gap-3 border-b border-bial-border px-6 py-4">
          <DialogTitle className="min-w-0 truncate text-base font-bold text-tertiary">
            {project.name || 'Untitled project'}
          </DialogTitle>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex-shrink-0 rounded-lg p-1.5 text-neutral transition hover:bg-bial-bg hover:text-tertiary"
          >
            <X size={18} />
          </button>
        </div>

        <Tabs
          value={tab}
          onValueChange={(next) => setTab(next as SettingsTab)}
          // A RAIL BESIDE THE BODY, AND A STRIP ABOVE IT WHEN NARROW (`narrow` is this product's
          // existing stacking threshold). Below it the rail would be competing for a width
          // neither half can spare, so it lies down and the panel goes full-bleed — nothing
          // becomes unreachable, it simply stops being a column.
          className="flex flex-row narrow:flex-col"
        >
          <TabsList className="flex w-48 flex-shrink-0 flex-col items-stretch justify-start gap-1 border-r border-bial-border bg-bial-bg/40 p-2 narrow:w-full narrow:flex-row narrow:border-b narrow:border-r-0">
            {TABS.map(({ value, label }) => (
              <TabsTrigger
                key={value}
                value={value}
                data-testid={`settings-tab-${value}`}
                className="justify-start rounded-lg px-3 py-2 text-left text-[13.5px] font-medium text-neutral data-[state=active]:bg-primary/10 data-[state=active]:font-semibold data-[state=active]:text-primary data-[state=active]:ring-1 data-[state=active]:ring-inset data-[state=active]:ring-primary/30"
              >
                {label}
              </TabsTrigger>
            ))}
          </TabsList>

          {/* One height for every panel, so choosing a tab does not resize the dialog under the
              pointer that chose it. */}
          <motion.div
            layout
            transition={{ duration: DURATION.layout, ease: LAYOUT_EASE }}
            className="min-h-[24rem] min-w-0 flex-1 overflow-y-auto p-6"
          >
            <TabsContent value="general" className="mt-0 flex flex-col gap-6">
              <label className="block">
                <span className="text-xs font-semibold text-tertiary">Name</span>
                <input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value)
                    setNameError(null)
                  }}
                  onBlur={saveName}
                  maxLength={NAME_MAX_CHARS}
                  aria-label="Application name"
                  aria-invalid={nameError !== null}
                  className="mt-1.5 w-full rounded-xl border border-bial-border px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
                <span className="mt-1.5 flex items-center gap-2 text-[11px]">
                  {savingName && <BusyGlyph size={12} className="text-neutral" />}
                  <span className={tooManyWords ? 'font-semibold text-danger' : 'text-neutral'}>
                    {words} of about {MAX_PROJECT_NAME_WORDS} words
                  </span>
                </span>
                {nameError !== null && (
                  <span role="alert" className="mt-1 block text-[11px] font-semibold text-danger">
                    {nameError}
                  </span>
                )}
              </label>

              <div>
                <span className="text-xs font-semibold text-tertiary">Description</span>
                <div className="mt-1.5">
                  <ProjectDescriptionEditor
                    projectId={project.id}
                    description={project.description}
                    onProjectUpdate={onProjectUpdate}
                  />
                </div>
              </div>

              {/* THE ONLY DELETE AFFORDANCE IN THE PRODUCT, and it is at the foot of the tab the
                  dialog opens on — two deliberate steps from a list, which is what an
                  irreversible action is owed. It opens the existing confirmation, which names the
                  whole cascade and asks why before it will proceed. */}
              <div className="mt-auto rounded-2xl border border-danger/30 bg-red-50/40 p-4">
                <p className="text-sm font-semibold text-tertiary">Delete this application</p>
                <p className="mt-1 text-xs leading-relaxed text-neutral">
                  Its app, its data, its files and every chat in it go with it. This cannot be
                  undone.
                </p>
                <button
                  type="button"
                  onClick={onDelete}
                  data-testid="settings-delete"
                  className="mt-3 inline-flex items-center gap-2 rounded-xl border border-danger/40 px-3 py-2 text-xs font-semibold text-danger transition hover:bg-red-50"
                >
                  <Trash2 size={14} />
                  Delete application
                </button>
              </div>
            </TabsContent>

            <TabsContent value="sharing" className="mt-0">
              <SharePanelBody projectId={project.id} />
            </TabsContent>

            {/* NO `forceMount` ON EITHER OF THESE, deliberately: Radix unmounts an unchosen
                panel, so neither read fires until its tab is chosen — which is what stops the
                production read polling behind another tab, and what keeps opening Settings from
                costing a connector read nobody asked for. */}
            <TabsContent value="integrations" className="mt-0">
              <IntegrationsTab projectId={project.id} />
            </TabsContent>

            <TabsContent value="production" className="mt-0">
              <ProductionTab projectId={project.id} onSettled={onProductionSettled} />
            </TabsContent>
          </motion.div>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}
