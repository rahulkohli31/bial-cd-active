/**
 * THE RAIL'S CONTENTS AT REST (Plan F, U1) — everything R6 asks the project screen to carry.
 *
 * ═══ WHAT THIS IS NOT ═══
 *
 * It is not a page and it is not a grid. The two-column frame belongs to `WorkspaceShell`, above
 * the Outlet, and this fills the Outlet's column. An implementer who builds rail-plus-pane in here
 * has produced a second two-column layout nested in the first, and every "the app did not remount"
 * assertion in this plan fails on the first navigation to a chat — because this component does not
 * match that address and the one holding the iframe must.
 *
 * ═══ R6's FOUR THINGS, AND THE ONE WITH A CONSTRAINT ATTACHED ═══
 *
 * A composer with the kind beside it; the app's status; when it was last saved; the description.
 * Three are unconditional. The save half is not, and the reason is cost rather than design:
 * `fetchSaveState` runs two `git` executions inside the container, so it may only be asked while
 * the workspace is alive — asking a stopped project whether it has unsaved work is a start the
 * screen caused, which R3 forbids. So a stopped project's rail shows the status sentence and NO
 * save state, NO commit and NO time, and the save half appears while the app is running. That is
 * a stated consequence, not an omission, and a saved-at TIME waits on a server field that does not
 * exist yet (no endpoint returns one).
 *
 * ═══ WHY THE STATUS SENTENCE APPEARS HERE AS WELL AS IN THE PANE ═══
 *
 * Because R6 asks for it, and because a citizen reading the rail should not have to look across at
 * the pane to learn whether their app is up. It is the SAME sentence — one computed value, so the
 * two cannot disagree — and this renderer deliberately carries no ACTION. R3 says exactly one
 * control starts the app, and that control is the pane's. A second Start button here would satisfy
 * "exactly one" with two.
 *
 * ═══ THE COLLAPSE CONTROL IS NOT IN THIS FILE, ON PURPOSE ═══
 *
 * A collapsed rail is `w-0` and `invisible`: out of the tab order and out of the accessibility
 * tree. A toggle living inside it would be a one-way door — press it once and nothing can undo it
 * without a reload. It is published into the PANE's leading toolbar slot instead, which is exactly
 * where the conversation surface already puts its own chat-panel toggle, and it points back here
 * through `aria-controls`.
 */
import { useEffect, useState } from 'react'
import { ArrowLeft, Check, MoreVertical, Pencil, X } from 'lucide-react'
import PublishStatusChip from '../PublishStatusChip'
import ProjectDescriptionEditor from '../projects/ProjectDescriptionEditor'
import RailComposer from './RailComposer'
import { chatKindFor } from '../../utils/chatKind'
import { relativeTime } from '../../utils/chatHistory'
import { shortSha } from '../../utils/shortSha'
import type { ChatSummary } from '../../utils/conversationApi'
import { patchProject } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'
import type { Project } from '../../utils/projectApi'
import type { SaveState } from '../../utils/buildSessionApi'
import type { WorkspaceState } from './workspaceState'

export interface WorkspaceRailProps {
  project: Project
  /** The one computed workspace value. Its sentence is rendered; its action is the pane's. */
  workspace: WorkspaceState
  /** Non-null only while the workspace is alive — see the cost note above. */
  save: SaveState | null
  chats: ChatSummary[]
  chatsError: string | null
  onProjectUpdate: (project: Project) => void
  onBack: () => void
  onOpenChat: (chatId: string) => void
  onDeleteChat: (chatId: string) => void
}

export default function WorkspaceRail({
  project,
  workspace,
  save,
  chats,
  chatsError,
  onProjectUpdate,
  onBack,
  onOpenChat,
  onDeleteChat,
}: WorkspaceRailProps) {
  // OWNED HERE, BECAUSE THIS IS THE ONLY COMPONENT THAT RENDERS THEM. Both were lifted to
  // `ProjectPage` and threaded down through `ProjectWorkspace`, which cost nine props across two
  // interfaces to reach one renderer — and neither is state anybody above can act on: a half-typed
  // name and an open ⋮ menu mean nothing to a route.
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null)

  // Close the row action menu on Escape or an outside click while it is open.
  useEffect(() => {
    if (!menuOpenId) return undefined
    const onDown = () => setMenuOpenId(null)
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpenId(null)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [menuOpenId])

  const onStartRename = () => {
    setNameDraft(project.name)
    setNameError(null)
    setEditingName(true)
  }
  const onCancelRename = () => setEditingName(false)

  const onSubmitRename = () => {
    const trimmed = nameDraft.trim()
    // Blocked client-side BEFORE any request: the server 400s on name:null and 422s on "". A
    // whitespace-only name never reaches the wire.
    if (trimmed === '') {
      setNameError('Name cannot be empty.')
      return
    }
    if (trimmed === project.name) {
      setEditingName(false)
      return
    }
    void (async () => {
      try {
        const updated = await patchProject(project.id, { name: trimmed })
        onProjectUpdate(updated)
        setEditingName(false)
        setNameError(null)
      } catch (err) {
        setNameError(err instanceof ApiError ? err.message : 'Could not rename. Try again.')
      }
    })()
  }

  return (
    // ITS OWN SCROLLER. The shell is a full-height frame that does not scroll, and this is a flex
    // child of it — without `overflow-y-auto` a project with twenty conversations clips its list
    // with no way to reach the bottom. `min-h-0` is what actually lets a flex child scroll: without
    // it the child's min-content height wins and the overflow never has anywhere to happen.
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="w-full px-5 py-6 space-y-5">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1 text-sm text-neutral hover:text-primary transition"
        >
          <ArrowLeft size={15} /> Back to projects
        </button>

        {/* THE HEADER. Both branches are the same `flex items-center gap-2` row with the chip as
            the second child, so starting a rename does not move it and does not remount it. */}
        <div>
          {editingName ? (
            <div className="flex items-center gap-2">
              <input
                aria-label="Project name"
                value={nameDraft}
                autoFocus
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onSubmitRename()
                  if (e.key === 'Escape') onCancelRename()
                }}
                className="flex-1 min-w-0 text-xl font-extrabold text-tertiary bg-white border border-bial-border rounded-lg px-2 py-1 focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
              <PublishStatusChip projectId={project.id} />
              <button
                type="button"
                aria-label="Save name"
                onClick={onSubmitRename}
                className="p-2 rounded-lg text-primary hover:bg-primary/5 transition"
              >
                <Check size={18} />
              </button>
              <button
                type="button"
                aria-label="Cancel rename"
                onClick={onCancelRename}
                className="p-2 rounded-lg text-neutral hover:bg-surface-muted transition"
              >
                <X size={18} />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <h1 className="min-w-0 truncate text-xl font-extrabold text-tertiary">{project.name}</h1>
              {/* R37's "beside the project name", literally, and NOT gated on `project.appId`: a
                  project with nothing built learns that from the same place it learns everything
                  else about its app, rather than from an absence. A live component carried
                  forward from the page this rail replaces — not a slot left for future work. */}
              <PublishStatusChip projectId={project.id} />
              <button
                type="button"
                aria-label="Rename project"
                onClick={onStartRename}
                className="p-1.5 rounded-lg text-neutral hover:text-primary hover:bg-surface-muted transition"
              >
                <Pencil size={15} />
              </button>
            </div>
          )}
          {nameError && (
            <p className="text-xs font-medium text-danger mt-1" role="alert">
              {nameError}
            </p>
          )}
        </div>

        <RailComposer projectId={project.id} />

        {/* R6's app status. THE SENTENCE ONLY — the action belongs to the pane (see the docblock).
            Same computed value, so the two surfaces cannot say different things. */}
        <section data-testid="rail-app-status" className="bg-white border border-bial-border rounded-2xl p-4">
          <h2 className="text-[11px] font-bold uppercase tracking-wide text-neutral mb-2">Your app</h2>
          <p className="text-sm font-semibold text-tertiary">{workspace.headline}</p>
          {workspace.detail && <p className="text-xs text-neutral mt-1 leading-snug">{workspace.detail}</p>}
          {/* THE SAVE HALF, which exists only while the app is running. `dirty` is TRI-STATE and
              its `null` is "could not tell", never "clean" — so an unknown says so rather than
              reporting that everything is saved. */}
          {save && (
            <div data-testid="rail-save-state" className="mt-3 pt-3 border-t border-bial-border">
              <p className="text-xs text-neutral">
                {save.dirty === true
                  ? 'You have changes that are not saved yet.'
                  : save.dirty === false
                    ? 'Everything is saved.'
                    : 'We could not check for unsaved changes.'}
              </p>
              {save.savedHead !== null && (
                <p className="text-[11px] text-neutral mt-1 tabular-nums">
                  Last saved version <span className="font-mono">{shortSha(save.savedHead)}</span>
                </p>
              )}
            </div>
          )}
        </section>

        {/* THE TESTID IS KEPT DELIBERATELY. This block is no longer a right-hand `aside` — the
            rail IS the left column now — but it is the same description block with the same
            read-view-plus-Edit-pop-up behaviour, and five assertions in `ProjectPage.test.tsx`
            still say something true about it. Renaming the handle would have retired them as
            collateral of a layout change, which is exactly the silent removal L8 forbids. */}
        <section data-testid="description-rail" className="bg-white border border-bial-border rounded-2xl p-4">
          <ProjectDescriptionEditor
            projectId={project.id}
            description={project.description}
            onProjectUpdate={onProjectUpdate}
          />
        </section>

        {/* CONVERSATIONS — a plain recents list, KEPT EXACTLY AS IT WAS.
            It was going to move into a history rail, and history was withheld by a client call
            before it was built. Deleting the section now would retire two capabilities silently:
            the only project-scoped way to delete a conversation, and the only route back to an
            EXISTING chat (everything else in the portal navigates to a newly minted one). It
            stays, unstyled and unmoved, until history is decided. */}
        <section data-testid="conversations" className="bg-white border border-bial-border rounded-2xl p-5">
          <h2 className="text-sm font-bold text-tertiary mb-4">Conversations · this project</h2>

          {chatsError ? (
            <p className="text-xs text-danger" role="alert">
              {chatsError}
            </p>
          ) : chats.length === 0 ? (
            <p className="text-sm text-neutral">No conversations yet — start a build or plan above.</p>
          ) : (
            <div className="space-y-2">
              {chats.map((chat) => {
                // One lookup, not a two-way test on a many-valued field: a retired `assistant`
                // row used to draw the Plan icon, which a word cannot get away with.
                const kind = chatKindFor(chat.kind)
                const menuOpen = menuOpenId === chat.id
                return (
                  // F-10: the row is a plain container. The title is a real <button> whose
                  // stretched ::after covers the row, so the whole row opens the chat — but the
                  // ⋮ menu is a SIBLING button layered above it, never an interactive descendant.
                  <div
                    key={chat.id}
                    className="group relative flex items-center gap-3 bg-white border border-bial-border rounded-xl px-4 py-3 hover:border-primary/40 hover:shadow-sm transition"
                  >
                    <h3 className="min-w-0 flex-1">
                      <button
                        type="button"
                        onClick={() => onOpenChat(chat.id)}
                        className="block w-full text-left text-sm font-semibold text-tertiary cursor-pointer rounded-sm after:absolute after:inset-0 after:rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      >
                        <span className="block truncate">{chat.title || 'Untitled'}</span>
                      </button>
                    </h3>
                    <span
                      className={`inline-flex flex-shrink-0 items-center rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide ${kind.pill}`}
                    >
                      {/* The word is shown; the completion is read but not seen, so the screen
                          says "Build" and the element's text says "Build chat". An `aria-label`
                          on a role-less span is not reliably exposed — it would satisfy a test
                          and help nobody — so the name is built from text. */}
                      {kind.word}
                      {kind.completion && <span className="sr-only">{kind.completion}</span>}
                    </span>
                    <span className="text-[11px] text-neutral flex-shrink-0 tabular-nums">
                      {relativeTime(chat.updatedAt)}
                    </span>
                    <div className="relative z-10 flex-shrink-0">
                      <button
                        type="button"
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={() => setMenuOpenId(menuOpen ? null : chat.id)}
                        aria-label={`Actions for ${chat.title || 'conversation'}`}
                        className="p-1 rounded-lg text-neutral hover:text-primary hover:bg-surface-muted transition"
                      >
                        <MoreVertical size={16} />
                      </button>
                      {menuOpen && (
                        <div
                          onMouseDown={(e) => e.stopPropagation()}
                          className="absolute right-0 top-8 z-20 w-32 bg-white rounded-lg border border-bial-border shadow-xl py-1"
                        >
                          <button
                            type="button"
                            onClick={() => {
                              setMenuOpenId(null)
                              onOpenChat(chat.id)
                            }}
                            className="w-full text-left px-3 py-2 text-sm text-tertiary hover:bg-bial-bg transition"
                          >
                            Open
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setMenuOpenId(null)
                              onDeleteChat(chat.id)
                            }}
                            className="w-full text-left px-3 py-2 text-sm text-danger hover:bg-red-50 transition"
                          >
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
