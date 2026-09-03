/**
 * THE TOOLBAR ROW (plan 002, U2) — one 54px row under the navbar, on both workspace screens.
 *
 * ═══ WHY IT IS DRAWN BY THE SHELL AND NOT BY THE RAIL ═══
 *
 * It used to be three separate headers: the rail's own (back control, project name, status chip,
 * rename), the conversation panel's (a bordered breadcrumb), and the framed preview's (device
 * widths, Reload, Save). Three headers, three widths, three sets of contents — and the project
 * name lived inside the 400px rail, so it truncated at the rail's width and DISAPPEARED entirely
 * when the rail was collapsed. The collapse board draws the opposite: hide the details and the
 * title is still there.
 *
 * Drawing it once, above the two-column grid, is what makes that true by construction rather than
 * by a rule someone has to keep. It also means the row is a SINGLE ELEMENT across a route change
 * from the project screen to one of its chats — its contents change, its position never does, and
 * nothing in it remounts.
 *
 * ═══ WHICH CHANNEL CELLS FEED IT, WHICH THE PLAN ASKED TO HAVE RECORDED ═══
 *
 * The heading comes from `heading`, published by the ROUTES rather than by the surfaces, so a cold
 * open of a chat address renders the row at its full height with its back control working while
 * the conversation and the project are both still resolving. See `WorkspaceHeading`.
 *
 * It deliberately does NOT read the `pane` cell, which is the obvious place chrome already lives:
 * that cell is republished on every keystroke in the composer and is cleared to nothing when its
 * publisher unmounts. Either one alone would disqualify it.
 *
 * The save control reads its VALUES from `save` and its ACTION from `actions`, at press time —
 * so a handler whose identity changes on every render of the conversation surface costs this row
 * nothing, and a stale closure is not reachable. See `useWorkspaceActions`.
 *
 * ═══ WHAT IS DELIBERATELY NOT HERE ═══
 *
 * THE HISTORY CONTROL. Four boards draw a clock icon in this row's right cluster and the history
 * drawer behind it is a later feature by the owner's decision — so the control is not built, and
 * not left as a disabled stub either. A control that implies a drawer nobody can open is worse
 * than its absence.
 */
import { useMemo } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Monitor,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  RotateCcw,
  Save,
  Smartphone,
  Tablet,
  type LucideIcon,
} from 'lucide-react'
import PublishStatusChip from '../PublishStatusChip'
import { chatKindFor } from '../../utils/chatKind'
import { useWorkspaceActions, useWorkspaceAddress, useWorkspaceHeading, useWorkspacePaneVisible, useWorkspaceSave } from './workspaceChannel'
import type { SaveSlot, WorkspaceActions } from './workspaceChannel'
import { WORKSPACE_RAIL_ID } from './WorkspaceShell'

/**
 * THE THREE WIDTHS THE PANE CAN FRAME AT, and their one home.
 *
 * They were `LivePreview`'s private `DEVICES` map, chosen there because the switcher lived in that
 * component's own toolbar. The switcher is in this row now and the widths are read by the device
 * card the pane draws, so the map moves up with the control and the pane imports it from here —
 * one table, not two that can disagree about what "Tablet" means.
 */
export const DEVICES = {
  Desktop: { icon: Monitor as LucideIcon, width: null as number | null },
  Tablet: { icon: Tablet as LucideIcon, width: 834 }, // iPad Pro 11" portrait — Chrome DevTools preset
  Mobile: { icon: Smartphone as LucideIcon, width: 390 }, // iPhone 12/13/14-class width
}

export type DeviceName = keyof typeof DEVICES

export interface WorkspaceToolbarProps {
  /** The rail's collapse, owned by the shell — the control that undoes it cannot live in the rail. */
  collapsed: boolean
  onToggleCollapsed: () => void
  device: DeviceName
  onDevice: (device: DeviceName) => void
  /** "What I am looking at is out of date" — a judgement only the person looking can make. */
  onReload: () => void
  /** Where the back control goes, already routed through the workspace's unsaved-work guard. */
  onBack: () => void
}

export default function WorkspaceToolbar({
  collapsed,
  onToggleCollapsed,
  device,
  onDevice,
  onReload,
  onBack,
}: WorkspaceToolbarProps) {
  const heading = useWorkspaceHeading()
  const save = useWorkspaceSave()
  const readActions = useWorkspaceActions()
  const address = useWorkspaceAddress()
  const paneVisible = useWorkspacePaneVisible()

  // A CHAT IS WHAT HAS A KIND, not what has a title. A freshly minted chat has no row yet and so
  // no title at all, and the row still has to draw it as a chat rather than fall back to the
  // project screen's layout for the seconds before the first message lands.
  const isChat = heading.chatKind !== null
  const kind = useMemo(() => (heading.chatKind ? chatKindFor(heading.chatKind) : null), [heading.chatKind])
  // Capitalised so JSX reads it as a component rather than as an intrinsic element. `null` is a
  // kind whose pill is the word alone — see `pillIcon`.
  const PillIcon = kind?.pillIcon ?? null

  // A STABLE FALLBACK IN THE NAME SLOT, never a gap. The project fetch can be unresolved (a cold
  // open) or failed (the project was deleted out from under an open chat), and in both cases the
  // row keeps its height, its back control and a word in the slot — which is what stops the layout
  // shifting under someone when the fetch lands.
  const projectName = heading.projectName ?? 'Your project'

  // Only when there is something to point at. The device widths and the new-tab link both describe
  // a framed app, and drawing them over an empty pane offers controls that cannot do anything.
  const hasApp = paneVisible && address.url !== null

  return (
    <div
      data-testid="workspace-toolbar"
      className="flex h-[54px] flex-shrink-0 items-center gap-2.5 border-b border-bial-border bg-white px-5"
    >
      <button
        type="button"
        onClick={onBack}
        aria-label={isChat ? 'Back to project' : 'Back to projects'}
        title={isChat ? 'Back to project' : 'Back to projects'}
        className="inline-flex flex-shrink-0 items-center rounded-lg p-0.5 text-neutral transition hover:text-primary"
      >
        <ChevronLeft size={16} />
      </button>

      {isChat ? (
        <>
          {/* ON A CHAT THE PROJECT IS THE BREADCRUMB and the CHAT is the heading — 13px/600 muted,
              then a separator chevron, then the kind, then the title at 15px/800. It is the same
              row and the same slots as the project screen; only which of the two names is the
              <h1> changes. */}
          <span className="min-w-0 flex-shrink truncate text-[13px] font-semibold text-neutral">{projectName}</span>
          <ChevronRight size={13} className="flex-shrink-0 text-canvas-sha" aria-hidden="true" />
          {kind && (
            <span
              data-testid="toolbar-chat-kind"
              className={`inline-flex flex-shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[10.5px] font-bold uppercase tracking-wide ${kind.pill}`}
            >
              {/* THE GLYPH THE BOARD DRAWS INSIDE THE PILL, AND ONLY WHERE IT DRAWS ONE. `PlanChat`
                  puts an 11px message-square in its PLAN pill; every primary board that draws a
                  build chat — `BuildChat`, `NewBuildChat`, `PlainAnswer`, `ChatStarting` — draws
                  BUILD as the word alone. That is why the catalogue answers this with its own
                  `pillIcon` rather than with the picker's `Icon`: the row must not branch on a
                  chat's kind (R72), so the difference has to live in the one table that holds the
                  kinds. Decorative beside the word it accompanies, hence `aria-hidden`. */}
              {PillIcon && <PillIcon size={11} aria-hidden="true" className="flex-shrink-0" />}
              {kind.word}
              {kind.completion && <span className="sr-only">{kind.completion}</span>}
            </span>
          )}
          <h1
            data-testid="toolbar-title"
            className="min-w-0 truncate text-[15px] font-extrabold tracking-[-0.25px] text-primary-900"
          >
            {/* A CHAT WITH NO TITLE YET IS THE ORDINARY CASE, not an error: the row is created by
                the first send and its title is derived from that message. Naming the kind is more
                use than an empty slot or a spinner. */}
            {heading.chatTitle || `New ${kind?.word.toLowerCase() ?? 'chat'}`}
          </h1>
        </>
      ) : (
        <h1
          data-testid="toolbar-title"
          className="min-w-0 truncate text-[15.5px] font-extrabold tracking-[-0.3px] text-primary-900"
        >
          {projectName}
        </h1>
      )}

      {/* ONE PLACE SAYS THE STATE AT A TIME, and which place it is depends on whether the rail is
          showing it. `BuildChat`, `PlanChat` and every other chat board draws the chip beside the
          title, because a chat has no APP STATUS section. `Collapsed` draws it there too, for the
          same reason — the section it lives in has just gone off screen. `PreviewOff`, `Main`,
          `NewProject` and `NothingBuilt` draw the identity cluster as back-chevron + title and
          NOTHING else, because the rail is right there carrying the pill.

          Ungated, the project screen stated the same word twice inside 300px — a `Draft` chip in
          the row and a `Draft` pill in the rail — which is the classic way two renderings of one
          fact start to disagree. */}
      {heading.projectId && (isChat || collapsed) && (
        <span className="ms-2.5 flex-shrink-0">
          <PublishStatusChip projectId={heading.projectId} />
        </span>
      )}

      {/* RENAME SURVIVES THE REBUILD, and it had to be moved rather than dropped. It lived in the
          rail's header, which U3 replaces with the board's three sections — none of which is a
          project name. No board draws a rename control anywhere, but the origin's rule is not to
          delete a shipped capability because an older board omits it, so it comes here, next to
          the name it edits, at the smallest weight the row has. */}
      {!isChat && heading.projectId && (
        <button
          type="button"
          onClick={() => readActions().rename?.()}
          aria-label="Rename project"
          title="Rename project"
          className="flex-shrink-0 rounded-lg p-1 text-neutral transition hover:bg-surface-muted hover:text-primary"
        >
          <Pencil size={13} />
        </button>
      )}

      <div className="ms-auto flex flex-shrink-0 items-center gap-4">
        {hasApp && (
          <>
            <div
              role="group"
              aria-label="Preview device width"
              className="inline-flex items-stretch overflow-hidden rounded-[9px] border border-bial-border bg-white"
            >
              {(Object.entries(DEVICES) as [DeviceName, (typeof DEVICES)[DeviceName]][]).map(
                ([label, { icon: Icon }], index) => (
                  <span key={label} className="inline-flex items-stretch">
                    {index > 0 && <span className="w-px self-stretch bg-bial-border" aria-hidden="true" />}
                    <button
                      type="button"
                      aria-pressed={device === label}
                      aria-label={label}
                      title={label}
                      onClick={() => onDevice(label)}
                      className={`inline-flex h-7 w-8 items-center justify-center transition ${
                        device === label ? 'bg-bial-bg text-primary-900' : 'text-canvas-placeholder hover:text-primary-900'
                      }`}
                    >
                      <Icon size={14} />
                    </button>
                  </span>
                ),
              )}
            </div>

            {/* RELOAD IS A FOURTH OCCUPANT the boards do not draw, kept for the same reason the
                navbar keeps Marketplace: it is a shipped recourse, not decoration. The automatic
                remount covers what the platform can detect — a turn ending over a live preview —
                and "what I see is out of date" (a dev server restarted, an HMR socket that died
                quietly) is a judgement only the person looking at it can make. Without it their
                only recourse is reloading the whole portal. */}
            <button
              type="button"
              onClick={onReload}
              aria-label="Reload your app"
              title="Reload your app"
              className="inline-flex items-center text-neutral transition hover:text-primary"
            >
              <RotateCcw size={15} />
            </button>

            <a
              href={address.url ?? undefined}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open your app in a new tab"
              title="Open your app in a new tab"
              className="inline-flex items-center text-neutral transition hover:text-primary"
            >
              <ExternalLink size={15} />
            </a>

            <span className="h-5 w-px bg-bial-border" aria-hidden="true" />
          </>
        )}

        <SaveControl save={save} readActions={readActions} />

        {/* THE COLLAPSE, ON THE ROW RATHER THAN ON THE PANE. It was drawn by `AppPane`, which was
            already an improvement on living inside the rail it hides — a collapsed rail is
            invisible and untabbable, so a toggle in it is a one-way door. The row is better still
            for the same reason it holds the title: it survives the collapse AND it survives the
            pane going away, so the control has one home in every state instead of appearing and
            disappearing with the pane. */}
        {paneVisible && (
          <button
            type="button"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-controls={WORKSPACE_RAIL_ID}
            aria-label={collapsed ? 'Show details' : 'Hide details'}
            title={collapsed ? 'Show details' : 'Hide details'}
            className="inline-flex h-7 w-[30px] items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-primary"
          >
            {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * THE SAVE CONTROL, in the board's three states.
 *
 * Clean is an outlined chip reading "Saved". Dirty is a TEAL OUTLINE on a pale teal ground with a
 * 6px amber dot — not a filled teal button, which is what the code had: a permanently loud control
 * is one people learn to ignore, and the dot is what the eye actually catches. That dot is one of
 * exactly two places the whole canvas uses the accent colour.
 *
 * `dirty === null` MEANS "COULD NOT TELL" AND RENDERS NOTHING. It is not clean. The check costs two
 * `git` executions inside the container, so a stopped project has no answer at all, and a chip
 * reading "Saved" over an unknown is the one thing this control must never say.
 *
 * WITH NO ACTION PUBLISHED IT IS A STATUS, NOT A BUTTON — a real `<span>`, so nothing invites a
 * press that would do nothing. That is today's project screen, whose surface deliberately has no
 * `onSave`; U11 gives it one and the same control becomes pressable there.
 */
function SaveControl({ save, readActions }: { save: SaveSlot; readActions: () => WorkspaceActions }) {
  const { dirty, saving, error, canSave } = save
  if (dirty === null) return null

  const look = dirty
    ? 'border-primary bg-canvas-savedirty text-primary font-bold'
    : 'border-bial-border bg-white text-neutral font-semibold'
  const shell = `inline-flex items-center gap-[7px] whitespace-nowrap rounded-[9px] border px-[13px] py-1.5 text-[12.5px] ${look}`
  const body = (
    <>
      <Save size={14} />
      {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
      {dirty && !saving && <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent" aria-hidden="true" />}
    </>
  )

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span role="alert" className="max-w-[220px] text-right text-[11px] text-danger">
          {error}
        </span>
      )}
      {canSave ? (
        // THE ACTION IS READ AT PRESS TIME, never held across a render — see `useWorkspaceActions`. A
        // `null` read means the publisher unmounted between this render and the click, which is a
        // press with nothing to do rather than a crash.
        <button
          type="button"
          data-testid="save-project"
          // `aria-disabled`, NEVER `disabled`: a disabled control throws focus to the document body.
          aria-disabled={saving || dirty === false}
          onClick={() => {
            if (saving || dirty === false) return
            readActions().save?.()
          }}
          className={`${shell} transition ${saving ? 'opacity-70' : ''}`}
        >
          {body}
        </button>
      ) : (
        <span data-testid="save-state" className={shell}>
          {body}
        </span>
      )}
    </span>
  )
}
