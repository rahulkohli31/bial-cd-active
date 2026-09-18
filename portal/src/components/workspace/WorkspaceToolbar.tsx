/**
 * THE TOOLBAR ROW — one 54px row across the top of both workspace screens, drawn once by the
 * shell rather than per-surface.
 *
 * WHY THIS EXISTS: it used to be three separate headers (rail, conversation panel, framed
 * preview), and the project name lived inside the 400px rail — so it truncated at the rail's
 * width and disappeared entirely when the rail collapsed. Drawing it once, above the two-column
 * grid, makes "the title survives a collapse" true by construction, and makes the row a single
 * element across a route change (position never remounts, only its contents change).
 *
 * Its SHAPE comes from `rail.mode`, not `heading.chatKind`: mode is derived from the pathname
 * and is right on the first frame, while chatKind is an answer that arrives from a fetch — using
 * it re-shaped the row under the reader on a cold `/chat/{id}` open (bookmark, reload, hand-over
 * from a plan chat). It deliberately does NOT read the `pane` cell (republished every keystroke,
 * cleared on unmount) — either fact alone disqualifies it. `heading` comes from the ROUTES so a
 * cold open still renders the row's full height and back control while data resolves. Save and
 * Discard read their values from the `save` cell and their actions from `actions` at press time, so
 * a handler whose identity changes every render costs nothing and no stale closure is reachable.
 *
 * NOT HERE: the history-drawer control. Four boards draw its icon, but the drawer is a later
 * feature by the owner's decision — an affordance for a drawer nobody can open is worse than
 * no affordance, so it is neither built nor left as a disabled stub.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Layers,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  RotateCcw,
  Save,
  Settings,
  Undo2,
  UserPlus,
} from 'lucide-react'
import PublishStatusChip from '../PublishStatusChip'
import TokenRing from '../layout/TokenRing'
import { useUsageToday } from '../../hooks/useUsageToday'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
} from '../ui/dropdown-menu'
import { NavMenuButton, useNavReveal } from '../layout/NavReveal'
import { BusyGlyph, useElapsedSeconds, ELAPSED_AFTER_MS } from '../ui/Waiting'
import { usePublishState } from '../../hooks/usePublishState'
import { chatKindFor } from '../../utils/chatKind'
import DiscardChangesDialog from './DiscardChangesDialog'
import RollbackVersionDialog from './RollbackVersionDialog'
import SaveVersionDialog from './SaveVersionDialog'
import { fetchVersions } from '../../utils/buildSessionApi'
import type { AppVersion, VersionList } from '../../utils/buildSessionApi'
import { versionStamp } from '../../utils/projectDates'
import { useRailSlot, useWorkspaceActions, useWorkspaceAddress, useWorkspaceHeading, useWorkspacePaneVisible, useWorkspaceSave } from './workspaceChannel'
import type { SaveSlot, WorkspaceActions } from './workspaceChannel'
import { DEVICES, type DeviceName } from './devices'
import { WORKSPACE_RAIL_ID } from './railId'

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

  // A fact about the ADDRESS, not the conversation. It used to be `heading.chatKind !== null`,
  // but kind only ever arrives from the server — so a cold `/chat/{id}` open drew the PROJECT
  // shape instead (a bare "Your project" heading, a back control aimed at the projects list),
  // then re-shaped under the reader once the fetch landed and threw anyone who pressed back
  // right out of the project they were in. `rail.mode` is derived from the pathname alone, so
  // it is right on the first frame; the kind still decides only what the pill says.
  const isChat = useRailSlot().mode === 'conversation'
  const kind = useMemo(() => (heading.chatKind ? chatKindFor(heading.chatKind) : null), [heading.chatKind])
  // Capitalised so JSX reads it as a component rather than as an intrinsic element. `null` is a
  // kind whose pill is the word alone — see `pillIcon`.
  const PillIcon = kind?.pillIcon ?? null

  // A STABLE FALLBACK IN THE NAME SLOT, never a gap. The project fetch can be unresolved (a cold
  // open) or failed (the project was deleted out from under an open chat), and in both cases the
  // row keeps its height, its back control and a word in the slot — which is what stops the layout
  // shifting under someone when the fetch lands.
  const projectName = heading.projectName ?? 'Your application'

  /** The same predicate `WorkspaceShell`'s back handler uses, so the label cannot promise a
   *  destination the press does not go to. */
  const backToProject = isChat && heading.projectId !== null

  // Only when there is something to point at. The device widths and the new-tab link both describe
  // a framed app, and drawing them over an empty pane offers controls that cannot do anything.
  const hasApp = paneVisible && address.url !== null

  // WHETHER A REAL APPLICATION IS BEHIND THIS ADDRESS, which is what the `⋯` menu needs and the
  // `projectId` route param cannot answer: the param is non-null even for a mangled paste that
  // never resolved to anything. The NAME is the only field on this heading that comes from the
  // project's own fetch, so it is what means "a project loaded" — the same test the rename
  // control used to make before it folded into the menu. A chat is not an application screen,
  // and its surface publishes neither action.
  const projectActions = !isChat && heading.projectName !== null ? heading.projectName : null

  const usage = useUsageToday()
  // The panel carries a counter of its own, so the toolbar's is the STAND-IN for it — see
  // where it is drawn below.
  const navOnScreen = useNavReveal()?.open === true

  return (
    <div
      data-testid="workspace-toolbar"
      /* THE ROW SCROLLS SIDEWAYS RATHER THAN BEING CLIPPED.
         Nine occupants do not fit in 360px and never will. The shell's root is `overflow-hidden`
         — a deliberate scroll-containment choice for the rail and the pane, unrelated to narrow
         screens — so what overflowed this row was not merely off to the right, it was CLIPPED,
         with nothing anywhere to bring it back. The right-hand cluster goes first, which puts Save
         itself outside the viewport and outside reach.
         `/projects` and `/marketplace` overflow 360px too and a finger drags to the rest; the
         workspace was the one route where that was not true.
         SCOPED TO THE ROW, NOT TO THE ROOT, because the row's own box never exceeds the root's
         width — only its CONTENTS do — so this is the narrowest element that can own the scroll,
         and the root keeps the containment the two columns depend on.
         `overflow-y-hidden` is not decoration: `overflow-x: auto` alone computes `overflow-y` to
         `auto` as well, which would put a vertical scrollbar in a 54px row the moment a horizontal
         one stole height from it. */
      className="flex h-[54px] flex-shrink-0 items-center gap-2.5 overflow-x-auto overflow-y-hidden border-b border-bial-border bg-white px-5"
    >
      {/* THE NAVIGATION'S ONLY VISIBLE DOOR INSIDE AN APPLICATION, and it leads the row because
          it answers "where am I in the platform" — the question everything else in this row is
          not about. The edge gesture and `⌘\` reach the same panel faster for people who learn
          them; this is the one that does not have to be learned. */}
      <NavMenuButton className="flex-shrink-0 narrow:min-h-[44px] narrow:min-w-[44px]" />

      {/* THE DIVIDER IS WHAT TELLS THE TWO APART. A control that summons the navigation and a
          control that hides the chat pane sat at the same end doing visibly similar things, and
          read as one. A hairline between them, two different glyph families, and tooltips that
          name different KINDS of thing — one a place in the platform, the other this pane — are
          the three ways they stop being mistaken for each other. The divider goes when the
          collapse does, since a separator with one side is just a line. */}
      {paneVisible && (
        <>
          <span className="h-5 w-px flex-shrink-0 bg-bial-border" aria-hidden="true" />
          {/* THE COLLAPSE, ON THE ROW RATHER THAN ON THE PANE OR IN THE RAIL. A collapsed rail is
              invisible and untabbable, so a toggle inside the rail it hides is a one-way door.
              The row is right for the same reason it holds the title: it survives the collapse
              AND it survives the pane going away. It is gated on a PANE existing, not on an app
              existing — which is why it cannot be grouped with the device switcher and the
              new-tab link at the other end, and why the left is the simpler home for it. */}
          <button
            type="button"
            data-testid="toolbar-collapse"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-controls={WORKSPACE_RAIL_ID}
            aria-label={collapsed ? 'Show the chat' : 'Hide the chat'}
            title={collapsed ? 'Show the chat' : 'Hide the chat'}
            /* 28×30, and the one control that undoes a collapse — a target too small to hit is a
               one-way door for exactly the citizen who least wants one. */
            className="inline-flex h-7 w-[30px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
          >
            {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
          </button>
        </>
      )}

      <button
        type="button"
        onClick={onBack}
        // WHAT IT SAYS IS WHAT IT DOES. The shell sends this to the chat's own project only when
        // there IS one to send it to; a chat whose project has not resolved yet goes to the
        // projects list, and must say so rather than promising a project it cannot reach.
        aria-label={backToProject ? 'Back to the application' : 'Back to My Applications'}
        title={backToProject ? 'Back to the application' : 'Back to My Applications'}
        /* THE SMALLEST TARGET IN THE ROW, at 20×20 — a 16px chevron in 2px of padding. Below the
           stacking threshold it presents 44×44. The GLYPH does not move: `min-h`/`min-w`
           grow the box around it and `justify-center` keeps it in the middle, so nothing about the
           row's drawn weight changes — only the area a finger can land on. */
        className="inline-flex flex-shrink-0 items-center justify-center rounded-lg p-0.5 text-neutral transition hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
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
                  chat's kind, so the difference has to live in the one table that holds the
                  kinds. Decorative beside the word it accompanies, hence `aria-hidden`. */}
              {PillIcon && <PillIcon size={11} aria-hidden="true" className="flex-shrink-0" />}
              {kind.word}
              {kind.completion && <span className="sr-only">{kind.completion}</span>}
            </span>
          )}
          <h1
            data-testid="toolbar-title"
            /* THE TITLE STOPS COLLAPSING TO NOTHING.
               Every other occupant of this row is `flex-shrink-0`, and this one carried `min-w-0`
               with no floor — so it was the ONLY flexible participant and 100% of any width
               deficit landed on it, all the way to a measured zero. At 360px the heading a citizen
               needs in order to know where they are simply was not on screen.
               144px is about ten characters and the ellipsis: enough to tell two projects apart.
               Past that the deficit goes to the row's own sideways scroll, where everything that
               no longer fits stays reachable instead of being clipped.
               WHY IT IS GATED AT `narrow:` AND NOT UNCONDITIONAL. `min-width` in flex does not
               only stop shrinking — it also GROWS an item whose content is narrower than the
               floor. Ungated, a short name would be padded out to 144px at every width and shove
               the chip away from the name it belongs to. The floor lives
               where the squeeze does. */
            className="min-w-0 truncate text-[15px] font-extrabold tracking-[-0.25px] text-primary-900 narrow:min-w-[9rem]"
          >
            {/* A CHAT WITH NO TITLE YET IS THE ORDINARY CASE, not an error: the row is created by
                the first send and its title is derived from that message. Naming the kind is more
                use than an empty slot or a spinner.

                WITH NO KIND EITHER, the slot stays empty for that one fetch. "New chat" would be a
                claim — this chat is brand new — about a chat that is far more often an existing
                one still loading, and the row holds its 54px height regardless, so nothing shifts
                by waiting the moment out. */}
            {heading.chatTitle || (kind ? `New ${kind.word.toLowerCase()}` : '')}
          </h1>
        </>
      ) : (
        <h1
          data-testid="toolbar-title"
          /* The same floor as the chat shape's heading, for the same reason — see above. */
          className="min-w-0 truncate text-[15.5px] font-extrabold tracking-[-0.3px] text-primary-900 narrow:min-w-[9rem]"
        >
          {projectName}
        </h1>
      )}

      {/* THE STATE IS ON SCREEN WHEREVER THE APPLICATION IS, and this is the only place that says
          it. The rail beside this carries a composer and nothing else, so anything gating the chip
          on the rail's presence leaves a citizen with no way to tell a draft from something live
          except by collapsing the chat. */}
      {heading.projectId && (
        <span className="ms-2.5 flex-shrink-0">
          <PublishStatusChip projectId={heading.projectId} />
        </span>
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
                      /* 28×32 each, the biggest targets in the row and still short of a finger.
                         `min-h`/`min-w` rather than a bigger `h`/`w` so the 14px icon and the
                         hairline dividers between the three keep the exact look the canvas drew;
                         the segmented group simply grows around them below the threshold. */
                      className={`inline-flex h-7 w-8 items-center justify-center transition narrow:min-h-[44px] narrow:min-w-[44px] ${
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
                navigation keeps Marketplace: it is a shipped recourse, not decoration. The automatic
                remount covers what the platform can detect — a turn ending over a live preview —
                and "what I see is out of date" (a dev server restarted, an HMR socket that died
                quietly) is a judgement only the person looking at it can make. Without it their
                only recourse is reloading the whole portal. */}
            <button
              type="button"
              onClick={onReload}
              aria-label="Reload your app"
              title="Reload your app"
              /* NO PADDING CLASS AT ALL: this was a bare 15×15 glyph, a third of a finger, and so
                 was the new-tab link beside it. They are the two worst targets in the row. */
              className="inline-flex items-center justify-center text-neutral transition hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
            >
              <RotateCcw size={15} />
            </button>

            <a
              href={address.url ?? undefined}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open your app in a new tab"
              title="Open your app in a new tab"
              /* An anchor, not a button — and just as pressable, so it carries the same floor. */
              className="inline-flex items-center justify-center text-neutral transition hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
            >
              <ExternalLink size={15} />
            </a>

            <span className="h-5 w-px bg-bial-border" aria-hidden="true" />
          </>
        )}

        <DiscardControl save={save} readActions={readActions} projectId={heading.projectId} />
        <SaveControl save={save} readActions={readActions} projectId={heading.projectId} />

        {/* THE TOKEN COUNTER FOLLOWS THE CITIZEN INTO THE WORKSPACE, which is the one screen
            where tokens are actually spent. It lives in the navigation panel, and the panel is
            hidden here — so without this the reading the client requires to stay visible is
            exactly absent where it matters most. Same hook, same figures, a smaller ring.
            IT STANDS IN FOR THE PANEL'S, it does not join it: while the panel is on screen —
            revealed over the work, or docked beside it — both drew the same figures twice in one
            view, which is how two renderings of one number start to disagree. */}
        {usage && !navOnScreen && (
          <span className="flex-shrink-0">
            <TokenRing usage={usage} compact />
          </span>
        )}

        {/* SETTINGS AND SHARE, AND NOTHING ELSE. Two controls that are about the APPLICATION
            rather than about the app running in the pane, which is why they fold together and
            why neither is gated on `hasApp`. Delete is deliberately not here: it is inside
            Settings, two steps from any list and any workspace. Send for review is not here
            either — Settings › Production is its one owner, and a second submit control would
            be a second place to disagree about whether there is anything to submit. */}
        {projectActions !== null && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                data-testid="workspace-menu"
                aria-label={`More actions for ${projectName}`}
                className="inline-flex h-7 w-[30px] flex-shrink-0 items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-primary data-[state=open]:bg-bial-bg data-[state=open]:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
              >
                <MoreHorizontal size={16} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="min-w-[180px] rounded-md border-bial-border bg-white p-1 shadow-lg"
            >
              <DropdownMenuItem
                data-testid="workspace-menu-settings"
                onSelect={() => readActions().settings?.()}
                className="gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted"
              >
                <Settings size={15} />
                Settings…
              </DropdownMenuItem>
              <DropdownMenuItem
                data-testid="workspace-menu-share"
                onSelect={() => readActions().share?.()}
                className="gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted"
              >
                <UserPlus size={15} />
                Share…
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  )
}

/**
 * THE SAVE CONTROL, in the board's three states. Clean is an outlined "Saved" chip; dirty is a
 * teal outline on pale teal with a 6px amber dot, one of only two accent-colour uses on the
 * canvas — a loud filled control gets ignored. `dirty === null` means "could not tell" and shows
 * no label at all, never "Saved": the git check costs two executions, so a stopped project has no
 * answer. It no longer renders NOTHING in that state, though — see the caret below.
 * With no action published it is a real `<span>`, not a button, so nothing invites a no-op press.
 * While it works it now SHOWS that too — through `BusyGlyph`, which owns both motion registers:
 * a spinning glyph where motion is allowed, and NO spinner at all where it is not, because a
 * stationary loading spinner reads as a hang rather than as an accommodation. Past five seconds a
 * live elapsed count appears beside it, which is the only signal that proves liveness without
 * moving; a production save was measured at forty seconds.
 */
function SaveControl({
  save,
  readActions,
  projectId,
}: {
  save: SaveSlot
  readActions: () => WorkspaceActions
  projectId: string | null
}) {
  const { dirty, saving, discarding, rollingBack, error, canSave } = save
  // Before the early return: hooks may not sit behind a conditional, and `dirty === null` is a
  // real render path here rather than an edge case.
  // ★ THE COUNTER COVERS BOTH WAITS. A rollback replaces every file over the same container and
  // takes the same tens of seconds as a save; a counter that only ran for saves would leave the
  // longer of the two waits with nothing moving and nothing counting.
  const busy = saving || rollingBack
  const elapsed = useElapsedSeconds(busy)
  const showElapsed = elapsed * 1000 >= ELAPSED_AFTER_MS
  const [naming, setNaming] = useState(false)
  // ★ THE SHELL SURVIVES A STOPPED WORKSPACE, and only the LABEL goes. Save renders no answer
  // there because it has none; a caret bolted onto Save would vanish with it, taking the list
  // with it — and a stopped workspace is exactly when someone goes looking for a version to go
  // back to. With no saves at all there is nothing behind the caret either, so the whole control
  // goes: a caret that opens onto nothing invites a press that goes nowhere.
  const versionsOnly = dirty === null
  if (versionsOnly && !save.hasSavedVersion) return null

  const look = dirty
    ? 'border-primary bg-canvas-savedirty text-primary font-bold'
    : 'border-bial-border bg-white text-neutral font-semibold'
  // ~31px tall and comfortably past 44px wide on its own words, so only the HEIGHT needs a floor
  // below the stacking threshold. The floor is on the shared shell rather than on the
  // button alone: the pressable and the unpressable rendering of this control are meant to be the
  // same object in two states, and one of them quietly changing height would say otherwise.
  // ONE SHELL, TWO HALVES. The border, the radius and the ground belong to the object; each half
  // carries its own padding and its own 44px floor below the stacking threshold, because two
  // press targets inside one 44px box is not two press targets. The label half keeps the
  // padding the control always had, so nothing about Save moves.
  const shell = `inline-flex items-stretch overflow-hidden whitespace-nowrap rounded-[9px] border text-[12.5px] ${look}`
  // HEIGHT ONLY, as the worded controls have always had it: the label half is comfortably past
  // 44px wide on its own words, and a width floor on it would be a floor nothing needs. The CARET
  // half is a glyph, so it carries both.
  const labelHalf = `inline-flex items-center gap-[7px] px-[13px] py-1.5 narrow:min-h-[44px]`
  const body = (
    <>
      {/* THE WAIT'S BOX — the spinner and the one sentence that describes it, together inside the
          polite region. Wrapping the sentence is how it is announced; a second `sr-only` copy of a
          sentence already on screen is that sentence read twice (`Announcer.tsx`). The region is a
          PERMANENT child of the control — the glyph is always in it — so it is in the accessibility
          tree before any text arrives, which is the order several reader-and-browser combinations
          need (`TurnBanner.tsx`); only its TEXT appears and disappears.

          "Save" and "Saved" stay outside it on purpose: they are the control's label, not the wait,
          and `dirty` flips on its own while a turn edits files — announcing every flip would be
          noise in the same region the wait needs to cut through. */}
      <span role="status" aria-live="polite" className="inline-flex items-center gap-[7px]">
        {busy ? (
          <BusyGlyph size={14} testId="save-spinner" />
        ) : versionsOnly ? (
          // A LAYERS GLYPH WHERE THE LABEL WOULD BE. The control still says what it is for
          // when it cannot say what the workspace holds.
          <Layers size={14} />
        ) : (
          <Save size={14} />
        )}
        {/* ONE REGION, TWO WAITS, AND THEY MUST NOT BORROW EACH OTHER'S WORD. "Saving…" during a
            rollback would tell a reader their work is being stored at the moment it is being
            replaced — the opposite of what is happening. */}
        {saving ? 'Saving…' : rollingBack ? 'Going back…' : null}
        {/* THE NUMBER, once the wait has earned it. A save measured at forty seconds in production
            spent all of them showing one unchanging word; under `prefers-reduced-motion` the glyph
            beside it did not turn either, and the control was reported as dead. */}
        {/* `aria-hidden` for the reason `WaitingLine`'s count is: this span sits INSIDE the
            polite region above, and a number changing once a second is announced once a second.
            "Saving…" is what a reader needs; the count is for the eye. */}
        {busy && showElapsed ? (
          <span aria-hidden="true" className="tabular-nums">
            {elapsed}s
          </span>
        ) : null}
      </span>
      {!busy && !versionsOnly && (dirty ? 'Save' : 'Saved')}
      {dirty && !busy && <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent" aria-hidden="true" />}
    </>
  )

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span role="alert" className="max-w-[220px] text-right text-[11px] text-danger">
          {error}
        </span>
      )}
      {/* ONE SHELL, TWO HALVES, AND THE OUTER ELEMENT PRESSES NOTHING. The border, the radius and
          the ground are the object; the label and the caret are each their own target inside it. */}
      <span className={`${shell} ${busy ? 'opacity-70' : ''}`}>
        {canSave ? (
          // THE ACTION IS READ AT PRESS TIME, never held across a render — see `useWorkspaceActions`. A
          // `null` read means the publisher unmounted between this render and the click, which is a
          // press with nothing to do rather than a crash.
          <button
            type="button"
            data-testid="save-project"
            // `aria-disabled`, NEVER `disabled`: a disabled control throws focus to the document body.
            aria-disabled={busy || discarding || dirty === false}
            // THE THIRD REGISTER, and a silent one: `aria-busy` is what a reader consults when asked
            // rather than something it speaks, so it costs the wait's sentence nothing. `undefined`
            // when idle — `aria-busy={false}` would ship a permanent `aria-busy="false"` on a control
            // that is not waiting, which is a state where the honest answer is no answer.
            aria-busy={busy || undefined}
            onClick={() => {
              if (busy || discarding || dirty === false) return
              // THE DIALOG IS THE SAVE NOW. It asks for the version's description and names what
              // the save drops; the request itself is what it confirms into.
              setNaming(true)
            }}
            className={`${labelHalf} transition`}
          >
            {body}
          </button>
        ) : (
          <span data-testid="save-state" className={labelHalf} aria-busy={busy || undefined}>
            {body}
          </span>
        )}
        {/* THE CARET, only where there is something behind it. A hairline rather than a gap: the
            two halves are one object, and a gap would read as two controls that happen to touch. */}
        {save.hasSavedVersion && projectId !== null && (
          <>
            <span aria-hidden="true" className="w-px flex-shrink-0 self-stretch bg-current opacity-20" />
            <VersionsMenu
              save={save}
              readActions={readActions}
              projectId={projectId}
              caretClass="inline-flex items-center px-2 transition narrow:min-h-[44px] narrow:min-w-[44px] narrow:justify-center"
            />
          </>
        )}
      </span>
      {naming && projectId !== null && (
        <SaveNaming
          projectId={projectId}
          onClose={() => setNaming(false)}
          onConfirm={(description) => readActions().save?.(description)}
        />
      )}
      {/* NO PROJECT ID MEANS NO LIST AND NO DIALOG, so the save keeps its old bodyless shape
          rather than opening a dialog that cannot say what it would drop. */}
      {naming && projectId === null && (
        <SaveVersionDialog
          evicting={null}
          onClose={() => setNaming(false)}
          onConfirm={(description) => readActions().save?.(description)}
        />
      )}
    </span>
  )
}

/** The dialog, with what the save would drop read from the same list the menu shows. */
function SaveNaming({
  projectId,
  onClose,
  onConfirm,
}: {
  projectId: string
  onClose: () => void
  onConfirm: (description: string) => void | Promise<void>
}) {
  const { list } = useVersions(projectId, true)
  return <SaveVersionDialog evicting={list?.evicting ?? null} onClose={onClose} onConfirm={onConfirm} />
}

/**
 * The version list, read when it is asked for rather than polled.
 *
 * WHY NOT THE SAVE-STATE POLL. That poll only runs while the preview reports the workspace alive,
 * and this list has to answer on a STOPPED workspace too — which is exactly when someone goes
 * looking for a version to go back to. So it is its own read, made when the menu opens.
 *
 * `open` gates the fetch rather than the caller mounting this conditionally, because a hook may
 * not sit behind a condition and the list must survive the menu closing and reopening without a
 * blank frame in between.
 */
function useVersions(projectId: string, open: boolean) {
  const [list, setList] = useState<VersionList | null>(null)
  const [error, setError] = useState<string | null>(null)
  // One token per read: a slow answer for a menu that has since closed, or for another project,
  // must not paint over a newer one.
  const read = useRef(0)

  useEffect(() => {
    if (!open) return
    const mine = ++read.current
    setError(null)
    void fetchVersions(projectId)
      .then((next) => {
        if (read.current === mine) setList(next)
      })
      .catch((err: unknown) => {
        if (read.current !== mine) return
        setError(err instanceof Error ? err.message : 'Could not read your versions')
      })
  }, [projectId, open])

  return { list, error }
}

/** One marker, as a chip. LIVE is the only one that carries colour — it is the only one that is a
 *  fact about BIAL staff rather than about this workspace. */
function MarkerChip({ marker }: { marker: string }) {
  const live = marker === 'live'
  const look = live
    ? 'border-primary-100 bg-primary-50 text-primary'
    : 'border-bial-border bg-surface-muted text-neutral'
  return (
    <span
      className={`rounded-full border px-1.5 py-px text-[9.5px] font-bold uppercase tracking-[0.04em] ${look}`}
    >
      {marker === 'current' ? 'Current' : live ? 'Live' : 'Previous'}
    </span>
  )
}

/**
 * THE CARET AND WHAT IT OPENS — the two most recent versions, plus whatever is live.
 *
 * A NON-CURRENT ROW IS THE ACTION. The whole row presses rather than carrying a button inside it:
 * there is exactly one thing to do with a version, and a row that shows a control beside itself
 * invites the question of what pressing the rest of the row does. The current row is inert — it is
 * what the workspace already holds — and an unavailable one states its reason before the press
 * rather than failing after it.
 */
function VersionsMenu({
  save,
  readActions,
  projectId,
  caretClass,
}: {
  save: SaveSlot
  readActions: () => WorkspaceActions
  projectId: string
  caretClass: string
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState<AppVersion | null>(null)
  const { list, error } = useVersions(projectId, open)

  const rows = list?.versions ?? null

  return (
    <>
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="versions-caret"
            aria-label="Versions"
            // `aria-disabled`, never `disabled`, for the label half's reason.
            aria-disabled={save.rollingBack || undefined}
            className={caretClass}
          >
            <ChevronDown size={14} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          data-testid="versions-menu"
          className="w-[290px] rounded-md border-bial-border bg-white p-1 shadow-lg"
        >
          <DropdownMenuLabel className="px-2.5 pb-1 pt-1.5 text-[11px] font-bold uppercase tracking-[0.04em] text-neutral">
            Versions
          </DropdownMenuLabel>
          {error !== null ? (
            <p role="alert" className="px-2.5 py-2 text-[12px] text-danger">
              {error}
            </p>
          ) : rows === null ? (
            // D8 — the menu opens NOW and says it is reading, rather than opening on a read that
            // has already landed. A menu that waits to appear reads as a press that did nothing.
            <p className="px-2.5 py-2 text-[12px] text-neutral" data-testid="versions-loading">
              Reading your versions…
            </p>
          ) : rows.length === 0 ? (
            <p className="px-2.5 py-2 text-[12px] text-neutral">
              You have not saved a version of this app yet.
            </p>
          ) : (
            rows.map((version, index) => {
              const press = version.available && version.id !== null && !save.rollingBack
              return (
                <DropdownMenuItem
                  key={version.id ?? `live-${index}`}
                  data-testid="version-row"
                  disabled={!press}
                  onSelect={() => {
                    if (press) setConfirming(version)
                  }}
                  className={`flex flex-col items-start gap-1 rounded-sm px-2.5 py-2 ${
                    press
                      ? 'cursor-pointer text-primary-900 focus:bg-surface-muted'
                      : 'cursor-default opacity-60 focus:bg-transparent'
                  }`}
                >
                  <span className="flex w-full items-center gap-1.5">
                    <span className="text-[12.5px] font-semibold text-tertiary">
                      {version.savedAt !== null ? versionStamp(version.savedAt) : 'Deployed earlier'}
                    </span>
                    <span className="ml-auto flex items-center gap-1">
                      {version.markers.map((marker) => (
                        <MarkerChip key={marker} marker={marker} />
                      ))}
                    </span>
                  </span>
                  {version.description !== null && (
                    <span className="w-full truncate text-[12px] text-neutral">
                      {version.description}
                    </span>
                  )}
                  {/* THE REASON, BEFORE THE PRESS. `discardRefusal`'s rule: a control that
                      refuses silently teaches nothing, and one that refuses after the press
                      teaches it too late. */}
                  {version.unavailableReason !== null && (
                    <span className="w-full text-[11px] text-neutral">
                      {version.unavailableReason}
                    </span>
                  )}
                </DropdownMenuItem>
              )
            })
          )}
          <p className="border-t border-bial-border px-2.5 py-2 text-[11px] leading-relaxed text-neutral">
            Your two most recent versions are listed, plus whatever BIAL staff are running. Going
            back keeps the version you are on now.
          </p>
        </DropdownMenuContent>
      </DropdownMenu>
      {confirming !== null && (
        <RollbackConfirmation
          projectId={projectId}
          version={confirming}
          dirty={save.dirty === true}
          onClose={() => setConfirming(null)}
          onConfirm={async () => {
            const versionId = confirming.id
            // CLOSED BEFORE THE AWAIT, unlike the save dialog: the wait belongs in the Save shell
            // where the elapsed counter is, not behind a modal covering the thing it changes.
            setConfirming(null)
            if (versionId !== null) await readActions().rollback?.(versionId)
          }}
        />
      )}
    </>
  )
}

/**
 * The rollback confirmation, with its conditional clauses read from the deployment.
 *
 * ★ THE DEPLOYMENT READ MOUNTS WITH THE DIALOG, NOT WITH THE MENU. `usePublishState` is a READER:
 * every mounted one answers the shared nudge, so putting it on the menu would have added a second
 * deployment read to every screen the toolbar draws and doubled the fan-out of every save —
 * `ProjectWorkspace.test.tsx`'s stampede test is what says so, and it caught exactly this. The
 * clauses are only needed once the dialog is on screen, so that is where the read belongs.
 */
function RollbackConfirmation({
  projectId,
  version,
  dirty,
  onClose,
  onConfirm,
}: {
  projectId: string
  version: AppVersion
  dirty: boolean
  onClose: () => void
  onConfirm: () => void | Promise<void>
}) {
  const { deployment } = usePublishState(projectId)
  return (
    <RollbackVersionDialog
      savedAt={version.savedAt}
      description={version.description}
      dirty={dirty}
      live={(deployment?.url ?? null) !== null && (deployment?.unpublishedAt ?? null) === null}
      // THE PIN, not the status. `approvedCommitSha` is the exact fact a rollback invalidates: the
      // publish path compares it against the head, so a rollback that moves the head off it is
      // what costs the app its approval. A status word would be a proxy for that.
      approved={(deployment?.approval?.approvedCommitSha ?? null) !== null}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  )
}

/** Why Discard cannot be pressed right now, or `null` when it can. */
function discardRefusal({ discarding, saving, replying, dirty, hasSavedVersion }: SaveSlot): string | null {
  if (discarding) return 'Discarding your changes'
  if (saving) return 'Wait for the save to finish'
  if (replying) return 'Wait for the reply to finish'
  if (dirty === false) return 'No unsaved changes'
  if (!hasSavedVersion) return 'Nothing saved yet to go back to'
  return null
}

/**
 * DISCARD, immediately left of Save and drawn in the same shell. It is on screen whenever Save is a
 * pressable control, and pressable only with unsaved work over a saved version; every other state
 * is dimmed and says why in its tooltip. `aria-disabled`, never `disabled`, for Save's reason.
 */
function DiscardControl({
  save,
  readActions,
  projectId,
}: {
  save: SaveSlot
  readActions: () => WorkspaceActions
  projectId: string | null
}) {
  const [confirming, setConfirming] = useState(false)
  if (save.dirty === null || !save.canDiscard) return null

  const refusal = discardRefusal(save)
  const look = refusal === null ? 'text-tertiary transition hover:text-danger' : 'cursor-not-allowed text-neutral opacity-50'
  const close = () => setConfirming(false)
  const confirm = async () => {
    await readActions().discard?.()
    setConfirming(false)
  }
  return (
    <>
      <button
        type="button"
        data-testid="discard-changes"
        aria-disabled={refusal !== null}
        aria-busy={save.discarding || undefined}
        title={refusal ?? 'Go back to the version you last saved'}
        onClick={() => {
          if (refusal === null) setConfirming(true)
        }}
        className={`inline-flex items-center gap-[7px] whitespace-nowrap rounded-[9px] border border-bial-border bg-white px-[13px] py-1.5 text-[12.5px] font-semibold narrow:min-h-[44px] ${look}`}
      >
        <span role="status" aria-live="polite" className="inline-flex items-center gap-[7px]">
          {save.discarding ? <BusyGlyph size={14} testId="discard-spinner" /> : <Undo2 size={14} />}
          {save.discarding ? 'Discarding…' : null}
        </span>
        {!save.discarding && 'Discard'}
      </button>
      {confirming &&
        (projectId === null ? (
          <DiscardChangesDialog savedAt={null} onClose={close} onConfirm={confirm} />
        ) : (
          <DiscardConfirmation projectId={projectId} onClose={close} onConfirm={confirm} />
        ))}
    </>
  )
}

/** The dialog, dated from the same deployment read as the rail's LAST SAVED row. */
function DiscardConfirmation({
  projectId,
  onClose,
  onConfirm,
}: {
  projectId: string
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  const { deployment } = usePublishState(projectId)
  return <DiscardChangesDialog savedAt={deployment?.savedAt ?? null} onClose={onClose} onConfirm={onConfirm} />
}
