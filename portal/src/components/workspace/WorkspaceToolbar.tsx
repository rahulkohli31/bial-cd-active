/**
 * THE TOOLBAR ROW — one 54px row under the navbar, on both workspace screens, drawn once by
 * the shell rather than per-surface.
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
 * cold open still renders the row's full height and back control while data resolves. Save reads
 * its values/action from the `save`/`actions` cells at press time, so a handler whose identity
 * changes every render costs nothing and no stale closure is reachable.
 *
 * NOT HERE: the history-drawer control. Four boards draw its icon, but the drawer is a later
 * feature by the owner's decision — an affordance for a drawer nobody can open is worse than
 * no affordance, so it is neither built nor left as a disabled stub.
 */
import { useMemo } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  RotateCcw,
  Save,
  UserPlus,
} from 'lucide-react'
import PublishStatusChip from '../PublishStatusChip'
import { BusyGlyph, useElapsedSeconds, ELAPSED_AFTER_MS } from '../ui/Waiting'
import { chatKindFor } from '../../utils/chatKind'
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
  const projectName = heading.projectName ?? 'Your project'

  /** The same predicate `WorkspaceShell`'s back handler uses, so the label cannot promise a
   *  destination the press does not go to. */
  const backToProject = isChat && heading.projectId !== null

  // Only when there is something to point at. The device widths and the new-tab link both describe
  // a framed app, and drawing them over an empty pane offers controls that cannot do anything.
  const hasApp = paneVisible && address.url !== null

  return (
    <div
      data-testid="workspace-toolbar"
      /* THE ROW SCROLLS SIDEWAYS RATHER THAN BEING CLIPPED.
         Nine occupants do not fit in 360px and never will. The shell's root is `overflow-hidden`
         — a deliberate scroll-containment choice for the rail and the pane, unrelated to narrow
         screens — so what overflowed this row was not merely off to the right, it was CLIPPED,
         with nothing anywhere to bring it back. The right-hand cluster goes first, which puts Save
         itself outside the viewport and outside reach.
         `/projects`, `/marketplace` and `/help` overflow 360px too and a finger drags to the rest;
         the workspace was the one route where that was not true.
         SCOPED TO THE ROW, NOT TO THE ROOT, because the row's own box never exceeds the root's
         width — only its CONTENTS do — so this is the narrowest element that can own the scroll,
         and the root keeps the containment the two columns depend on.
         `overflow-y-hidden` is not decoration: `overflow-x: auto` alone computes `overflow-y` to
         `auto` as well, which would put a vertical scrollbar in a 54px row the moment a horizontal
         one stole height from it. */
      className="flex h-[54px] flex-shrink-0 items-center gap-2.5 overflow-x-auto overflow-y-hidden border-b border-bial-border bg-white px-5"
    >
      <button
        type="button"
        onClick={onBack}
        // WHAT IT SAYS IS WHAT IT DOES. The shell sends this to the chat's own project only when
        // there IS one to send it to; a chat whose project has not resolved yet goes to the
        // projects list, and must say so rather than promising a project it cannot reach.
        aria-label={backToProject ? 'Back to project' : 'Back to projects'}
        title={backToProject ? 'Back to project' : 'Back to projects'}
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
               the chip and the rename pencil away from the name they belong to. The floor lives
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

      {/* ONE PLACE SAYS THE STATE AT A TIME. Chat boards (which have no app-status section) and
          `Collapsed` (whose section just went off screen) draw the chip beside the title;
          `PreviewOff`, `Main`, `NewProject` and `NothingBuilt` draw only chevron + title, since
          the rail is right there carrying the pill. Ungated, the project screen stated the same
          word twice inside 300px — a `Draft` chip here and a `Draft` pill in the rail — the
          classic way two renderings of one fact start to disagree. */}
      {heading.projectId && (isChat || collapsed) && (
        <span className="ms-2.5 flex-shrink-0">
          <PublishStatusChip projectId={heading.projectId} />
        </span>
      )}

      {/* RENAME SURVIVES THE REBUILD, moved here from the rail's header once that header was
          replaced by the board's three sections, none of which is a project name. No board draws
          a rename control anywhere, but a shipped capability is not deleted because an older
          board omits it, so it lives next to the name it edits, at the smallest weight the row
          has.

          WHAT GATES IT IS THE NAME, NOT THE ID. `projectId` is the route param — non-null even for
          an address that never resolved to a project at all, including a mangled paste that 422s
          at the boundary — so gating on it drew a pencil over a page with no project behind it,
          whose press was a measured no-op (rename is nullable and optional at the call site). The
          NAME is the only field on this heading that is an answer from the project's own fetch, so
          it is the one that means "a project loaded", on both routes that publish a heading.
          Explicitly against `null` rather than truthy: a name is a string, and `'' && …` renders a
          stray text node instead of nothing.

          IT MUST NOT SPREAD TO THE BACK CONTROL ABOVE. That control's whole job is to survive the
          branch where nothing loaded — it is the way out of a dead address — and gating it on the
          same fact would strand the citizen on the page. For the same reason the breadcrumb keeps
          its "Your project" fallback: a missing name silences the pencil, and nothing else in the
          row. */}
      {!isChat && heading.projectName !== null && (
        <button
          type="button"
          onClick={() => readActions().rename?.()}
          aria-label="Rename project"
          title="Rename project"
          /* 21×21 — a 13px pencil in 4px of padding — and the second-smallest thing in the row.
             `inline-flex` centres the glyph once the box grows past it below the threshold; on a
             plain `<button>` the 44px box would have left the pencil hard against its top-left.
             Above the threshold this is byte-identical geometry: 13 + 4 + 4, laid out the same. */
          className="inline-flex flex-shrink-0 items-center justify-center rounded-lg p-1 text-neutral transition hover:bg-surface-muted hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
        >
          <Pencil size={13} />
        </button>
      )}

      {/* SHARE (#198), the project screen's own control — gated identically to Rename above
          and for the same reason: it is a fact about THIS project, not about a chat, and the
          NAME (not the id) is what proves a real project loaded. Placed in the left-hand
          cluster beside Rename rather than the right-hand action group, since both are
          "about this project" controls and neither depends on an app existing to serve. */}
      {!isChat && heading.projectName !== null && (
        <button
          type="button"
          onClick={() => readActions().share?.()}
          aria-label="Share project"
          title="Share project"
          className="inline-flex flex-shrink-0 items-center justify-center rounded-lg p-1 text-neutral transition hover:bg-surface-muted hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
        >
          <UserPlus size={13} />
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

        <SaveControl save={save} readActions={readActions} />

        {/* THE COLLAPSE, ON THE ROW RATHER THAN ON THE PANE OR IN THE RAIL. A collapsed rail is
            invisible and untabbable, so a toggle inside the rail it hides is a one-way door. The
            row is right for the same reason it holds the title: it survives the collapse AND it
            survives the pane going away, so the control has one home in every state instead of
            appearing and disappearing with the pane. */}
        {paneVisible && (
          <button
            type="button"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-controls={WORKSPACE_RAIL_ID}
            aria-label={collapsed ? 'Show details' : 'Hide details'}
            title={collapsed ? 'Show details' : 'Hide details'}
            /* 28×30, and the one control that undoes a collapse — a target too small to hit is a
               one-way door for exactly the citizen who least wants one. */
            className="inline-flex h-7 w-[30px] items-center justify-center rounded-lg text-neutral transition hover:bg-bial-bg hover:text-primary narrow:min-h-[44px] narrow:min-w-[44px]"
          >
            {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * THE SAVE CONTROL, in the board's three states. Clean is an outlined "Saved" chip; dirty is a
 * teal outline on pale teal with a 6px amber dot, one of only two accent-colour uses on the
 * canvas — a loud filled control gets ignored. `dirty === null` means "could not tell" and renders
 * nothing, never "Saved": the git check costs two executions, so a stopped project has no answer.
 * With no action published it is a real `<span>`, not a button, so nothing invites a no-op press.
 * While it works it now SHOWS that too — through `BusyGlyph`, which owns both motion registers:
 * a spinning glyph where motion is allowed, and NO spinner at all where it is not, because a
 * stationary loading spinner reads as a hang rather than as an accommodation. Past five seconds a
 * live elapsed count appears beside it, which is the only signal that proves liveness without
 * moving; a production save was measured at forty seconds.
 */
function SaveControl({ save, readActions }: { save: SaveSlot; readActions: () => WorkspaceActions }) {
  const { dirty, saving, error, canSave } = save
  // Before the early return: hooks may not sit behind a conditional, and `dirty === null` is a
  // real render path here rather than an edge case.
  const elapsed = useElapsedSeconds(saving)
  const showElapsed = elapsed * 1000 >= ELAPSED_AFTER_MS
  if (dirty === null) return null

  const look = dirty
    ? 'border-primary bg-canvas-savedirty text-primary font-bold'
    : 'border-bial-border bg-white text-neutral font-semibold'
  // ~31px tall and comfortably past 44px wide on its own words, so only the HEIGHT needs a floor
  // below the stacking threshold. The floor is on the shared shell rather than on the
  // button alone: the pressable and the unpressable rendering of this control are meant to be the
  // same object in two states, and one of them quietly changing height would say otherwise.
  const shell = `inline-flex items-center gap-[7px] whitespace-nowrap rounded-[9px] border px-[13px] py-1.5 text-[12.5px] narrow:min-h-[44px] ${look}`
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
        {saving ? <BusyGlyph size={14} testId="save-spinner" /> : <Save size={14} />}
        {saving ? 'Saving…' : null}
        {/* THE NUMBER, once the wait has earned it. A save measured at forty seconds in production
            spent all of them showing one unchanging word; under `prefers-reduced-motion` the glyph
            beside it did not turn either, and the control was reported as dead. */}
        {/* `aria-hidden` for the reason `WaitingLine`'s count is: this span sits INSIDE the
            polite region above, and a number changing once a second is announced once a second.
            "Saving…" is what a reader needs; the count is for the eye. */}
        {saving && showElapsed ? (
          <span aria-hidden="true" className="tabular-nums">
            {elapsed}s
          </span>
        ) : null}
      </span>
      {!saving && (dirty ? 'Save' : 'Saved')}
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
          // THE THIRD REGISTER, and a silent one: `aria-busy` is what a reader consults when asked
          // rather than something it speaks, so it costs the wait's sentence nothing. `undefined`
          // when idle — `aria-busy={false}` would ship a permanent `aria-busy="false"` on a control
          // that is not waiting, which is a state where the honest answer is no answer.
          aria-busy={saving || undefined}
          onClick={() => {
            if (saving || dirty === false) return
            readActions().save?.()
          }}
          className={`${shell} transition ${saving ? 'opacity-70' : ''}`}
        >
          {body}
        </button>
      ) : (
        <span data-testid="save-state" className={shell} aria-busy={saving || undefined}>
          {body}
        </span>
      )}
    </span>
  )
}
