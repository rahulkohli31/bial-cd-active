/**
 * THE APP PANE HOST (Plan A, U4) — one iframe for the whole workspace.
 *
 * ═══ THE ONE IDEA ═══
 *
 * The pane used to be rendered BY THE ROUTE: it existed because `BuilderPage` was the page that
 * matched, and it was destroyed because a different page matched next. Here it is rendered BY THE
 * ADDRESS. No address, no element; the same address across every transition, the same element. R8
 * stops being a rule somebody has to remember and becomes a consequence of where the element lives.
 *
 * This component is a SIBLING of the shell's `<Outlet/>`, which is the whole mechanism. A route
 * change replaces the outlet's content and cannot reach a sibling.
 *
 * ═══ WHAT IDENTIFIES THE FRAME, AND THE FAILURE THIS IS WRITTEN AGAINST ═══
 *
 * The frame's identity is the framed URL plus the reload nonce that URL already carries inside
 * `LivePreview` (`url#nonce`). The route, the rail mode and the open chat are not part of it, and
 * this host adds no `key` of its own.
 *
 * THE FAILURE: buying continuity by weakening what identifies the frame — most obviously by making
 * it never unmount at all. That satisfies "nothing reloaded" and leaves a frame pointing at a
 * container that is gone, with nothing able to detect it. Continuity has to come from WHERE THE
 * ELEMENT LIVES, not from what identifies it — so the two legitimate re-frames stay exactly as they
 * are: a turn ending over a live preview, and the manual Reload control.
 *
 * A DIFFERENT PROJECT IS A DIFFERENT APP, so a different address, so a legitimate remount. That is
 * said here rather than left to be discovered, and the channel enforces it: a held address carries
 * the project it belongs to, and stops being this workspace's the moment a surface declares a
 * different one. An UNRESOLVED project is not a different project — see `useWorkspaceAddress`.
 *
 * ═══ HIDDEN IS NOT UNMOUNTED ═══
 *
 * When no mounted surface declares the pane visible, the frame stays in the document inside a
 * zero-size wrapper with `visibility:hidden`. The distinction is the requirement: the pane is a
 * cross-origin frame whose `src` is re-issued on remount, and re-issuing it means a full reload
 * plus a fresh framing handshake.
 *
 * `visibility:hidden` RATHER THAN `aria-hidden` OR ZERO WIDTH ALONE, for the reason the chat
 * panel's own comment already records: zero width and `overflow:hidden` clip a subtree visually but
 * leave its descendants in the tab order, so `aria-hidden` alone left controls keyboard-reachable
 * while collapsed — a WCAG 4.1.2 violation. `visibility:hidden` drops the whole subtree from both
 * the tab order and the accessibility tree while staying mounted.
 *
 * ═══ WHAT THIS COMPONENT WILL NOT DO ═══
 *
 * NOTHING HERE REQUESTS AN ADDRESS. The host frames what already exists; it never starts a
 * sandbox. That is what keeps R3 true before Plan F owns the start control — a mounted-but-hidden
 * pane on the project screen costs nothing, because there is nothing for it to frame unless a
 * conversation already put something there.
 */
import LivePreview from '../LivePreview'
import { useWorkspaceAddress, useWorkspacePane, useWorkspacePaneVisible } from './workspaceChannel'

export default function AppPaneHost() {
  const address = useWorkspaceAddress()
  const pane = useWorkspacePane()
  const visible = useWorkspacePaneVisible()

  // NOTHING TO HOST AT ALL. Not the same as "hidden": there is no address and no surface asking
  // for a pane, so there is no element to keep alive and none to hide. This is the project screen
  // before anybody has opened a conversation in it.
  if (!pane && !address.url) return null

  return (
    <div
      data-testid="app-pane"
      aria-hidden={!visible}
      className={
        visible
          ? 'flex-1 min-w-0 overflow-hidden'
          : // Zero size AND invisible. The width alone would only clip it; `invisible` is what
            // takes the framed app out of the tab order and out of the accessibility tree.
            'w-0 flex-shrink-0 overflow-hidden invisible'
      }
    >
      <LivePreview
        // NO `key`. The frame's identity is the URL plus `LivePreview`'s own reload nonce, and
        // adding one here — on the route, on the chat, on anything else — is exactly how a pane
        // that is meant to outlive a navigation gets remounted by one.
        //
        // SPREAD, NOT TRANSCRIBED. `PaneView` is a subset of this component's props, so listing
        // them again here would be a second copy of every default — twenty-odd `?? false` and
        // `?? null` clauses, each free to drift from the one `LivePreview` already declares in its
        // own signature. Spreading nothing when no surface has published is exactly right: the
        // frame needs only an address to keep running, and the pane's own defaults are the correct
        // resting state for a departed conversation's chrome. TypeScript is what keeps the two
        // shapes honest — a field on `PaneView` that this component does not accept is an error
        // here rather than a prop silently going nowhere.
        {...(pane ?? {})}
        previewUrl={address.url}
        status={address.status}
      />
    </div>
  )
}
