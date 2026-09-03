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
 * `visibility:hidden` RATHER THAN `aria-hidden` OR ZERO WIDTH ALONE, for the reason `hiddenSubtree.ts`
 * records beside the constant: zero width and `overflow:hidden` clip a subtree visually but leave its
 * descendants in the tab order, so `aria-hidden` alone left controls keyboard-reachable while
 * collapsed — a WCAG 4.1.2 violation. The stake is highest here of the three appliers: what this one
 * hides is a cross-origin frame holding a whole application.
 *
 * ═══ WHAT THIS COMPONENT WILL NOT DO ═══
 *
 * NOTHING HERE REQUESTS AN ADDRESS. The host frames what already exists; it never starts a
 * sandbox. That is what keeps R3 true before Plan F owns the start control — a mounted-but-hidden
 * pane on the project screen costs nothing, because there is nothing for it to frame unless a
 * conversation already put something there.
 */
import { useRef } from 'react'
import LivePreview from '../LivePreview'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import type { DeviceName } from './WorkspaceToolbar'
import { useWorkspaceAddress, useWorkspacePane, useWorkspacePaneVisible } from './workspaceChannel'

export interface AppPaneHostProps {
  /** Shell-owned, passed straight through — see `AppPane`. */
  device: DeviceName
  reloadNonce: number
}

export default function AppPaneHost({ device, reloadNonce }: AppPaneHostProps) {
  const address = useWorkspaceAddress()
  const pane = useWorkspacePane()
  const visible = useWorkspacePaneVisible()

  // TWO PANE FIELDS ARE FRAME IDENTITY RATHER THAN CHROME, AND THAT IS WHY THEY ARE HELD HERE.
  //
  // The pane view is cleared when its publisher unmounts, which is right for everything else on it:
  // a departed conversation's toolbar and handlers are not this pane's business. These two are not
  // chrome — each one, left to fall back to `LivePreview`'s prop default, tears down the very frame
  // this host exists to keep alive, on a different leave:
  //
  //   iterating      `LivePreview` turns a true→false edge into a reload nonce — its "a turn just
  //                  ended over a live preview, re-request the document" signal. Defaulting it
  //                  FABRICATES that edge, so leaving a build chat WHILE A BUILD IS RUNNING reloads
  //                  the app: silently, and semantically wrongly, because the turn had not ended.
  //   completedLive  the #13/R2 pardon — "this container is alive under an idle lease". It is what
  //                  makes `keepFramed` outrank a terminal status (`LivePreview.tsx:500`, `:521`).
  //                  The address KEEPS its status, and for a finished build that status is `ended`,
  //                  so defaulting this one to `false` collapses `frameContext` and UNMOUNTS the
  //                  iframe — leaving a build chat right after the build SUCCEEDS, which is the
  //                  most common moment to leave one, destroys an app the server is still serving.
  //
  // These are the only two. Every other pane field that reaches the frame chain — `relaunching`,
  // `reconnecting`, `previewState` — defaults to the permissive value, so losing it cannot unmount
  // anything. Adding a restrictive-by-default field to `PaneView` means adding it here too.
  //
  // Holding the last published value keeps the leave side inert. The RETURN side still re-frames
  // where it should, and that is correct and unchanged: a remounted surface publishes its own pane
  // view on its first commit, which replaces both held values before they can be read again.
  //
  // (The tidier end state is to move `completedLive` onto the ADDRESS, where it belongs — it is a
  // fact about what is framed, not about the conversation's chrome. That is a `PreviewAddress`
  // change with its own resolver arms and tests, so it is named here rather than smuggled in.)
  const lastIterating = useRef(false)
  const lastCompletedLive = useRef(false)
  if (pane) {
    lastIterating.current = pane.iterating
    lastCompletedLive.current = pane.completedLive
  }

  // NOTHING TO HOST AT ALL. Not the same as "hidden": there is no address and no surface asking
  // for a pane, so there is no element to keep alive and none to hide. This is the project screen
  // before anybody has opened a conversation in it.
  if (!pane && !address.url) return null

  return (
    <div
      data-testid="app-pane"
      aria-hidden={!visible}
      // THE MOVEMENT THE BOARD DRAWS (plan 002, U6). `T2Sliding` is an artboard of this one
      // transition, caught halfway, with an annotation that says exactly what it is: the app card
      // sliding out to the right and fading as it goes, and "nothing about the app is stopped or
      // reloaded — it is only taken off the screen".
      //
      // THE ANIMATION IS ON THE HIDE TREATMENT, NEVER ON THE MOUNT, and that distinction is the
      // whole reason this is safe. The element is not conditionally rendered — it is the same node
      // throughout, with a class change — so the frame inside it is untouched by the movement. An
      // enter/exit animation that keyed on mounting would remount the iframe, which is the one
      // thing this host exists to forbid.
      className={
        visible
          ? 'flex-1 min-w-0 overflow-hidden animate-pane-return'
          : // Zero size AND out of reach. The width alone would only clip it; HIDDEN_BUT_MOUNTED is
            // what takes the framed app out of the tab order and out of the accessibility tree.
            `w-0 flex-shrink-0 overflow-hidden ${HIDDEN_BUT_MOUNTED}`
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
        // shapes honest for the fields they SHARE: a `PaneView` field whose type stops matching
        // `LivePreview`'s prop is an error right here. It does NOT catch a field `LivePreview` has no
        // prop for at all — JSX spread attributes are exempt from excess-property checking, so such a
        // field would go nowhere silently. `UnacceptedPaneProps` beside `PaneView` is what pins that
        // half, because a comment cannot.
        {...(pane ?? {})}
        previewUrl={address.url}
        status={address.status}
        // AFTER the spread, deliberately: these two must not be allowed to fall back to the
        // component defaults when the publisher is gone. See the hold above for what each breaks.
        iterating={pane ? pane.iterating : lastIterating.current}
        completedLive={pane ? pane.completedLive : lastCompletedLive.current}
        device={device}
        reloadNonce={reloadNonce}
      />
    </div>
  )
}
