/**
 * THE APP PANE HOST — one iframe for the whole workspace.
 *
 * WHY THIS EXISTS: a SIBLING of the shell's `<Outlet/>`, so a route change (which replaces the
 * outlet's content) can never reach it. The pane is rendered BY THE ADDRESS, not by whichever
 * page matches — no address, no element; same address across every transition, same element.
 * Mounting `LivePreview` anywhere else builds a second host, the remount this file forbids. The
 * address deliberately outlives the surface that published it (leaving a build chat for the
 * project screen must not kill a running app), so it is bounded by the PROJECT, not a
 * publisher's lifetime — see `useWorkspaceAddress`.
 *
 * Frame identity is the URL plus `LivePreview`'s own reload nonce — route, rail mode and open
 * chat are NOT part of it, and this host adds no `key`. THE FAILURE this guards against: buying
 * continuity by weakening that identity, most obviously by never unmounting at all, which leaves
 * a frame pointing at a gone container, undetectably. Continuity comes from WHERE THE ELEMENT
 * LIVES, not from what identifies it — so the legitimate re-frames stay exactly as they are: the
 * app starting to answer at its URL, and the citizen's Reload. A different project is a
 * different app, so a different address, so a legitimate remount; an UNRESOLVED project is not a
 * different project.
 *
 * HIDDEN IS NOT UNMOUNTED: an invisible pane stays in the document, zero-size and
 * `visibility:hidden` — re-issuing this cross-origin frame's `src` means a full reload plus a
 * fresh handshake. Not `aria-hidden`/zero-width alone (see `hiddenSubtree.ts`): those leave
 * descendants tab-reachable while visually clipped, a WCAG 4.1.2 violation — the highest-stakes
 * case of the three appliers, since this hides a whole application. NOTHING HERE REQUESTS AN
 * ADDRESS: the host frames what already exists and never starts a sandbox, so a mounted-but-
 * hidden pane on the project screen costs nothing.
 */
import LivePreview from '../LivePreview'
import { HIDDEN_BUT_MOUNTED } from './hiddenSubtree'
import type { DeviceName } from './devices'
import { useWorkspaceAddress, useWorkspacePane, useWorkspacePaneVisible } from './workspaceChannel'

export interface AppPaneHostProps {
  /** Shell-owned, passed straight through — see `AppPane`. */
  device: DeviceName
  reloadNonce: number
  /**
   * The pane is unwanted but has not finished going. Decided by `AppPane`, which owns the column
   * this sits inside, so the two cannot disagree about whether they are still on their way out
   * — see `paneExit.ts`.
   */
  leaving: boolean
}

export default function AppPaneHost({ device, reloadNonce, leaving }: AppPaneHostProps) {
  const address = useWorkspaceAddress()
  const pane = useWorkspacePane()
  const visible = useWorkspacePaneVisible()

  // EVERY PANE FIELD THAT REACHES THE FRAME CHAIN DEFAULTS PERMISSIVELY. The pane view is cleared
  // when its publisher unmounts, and the spread below then falls back to `LivePreview`'s own
  // defaults. A field whose default would unmount or reload the frame has to be held across that
  // unmount here — or, better, belongs on the ADDRESS, which the channel keeps.

  // NOTHING TO HOST AT ALL. Not the same as "hidden": there is no address and no surface asking
  // for a pane, so there is no element to keep alive and none to hide. This is the project screen
  // before anybody has opened a conversation in it.
  if (!pane && !address.url) return null

  return (
    <div
      data-testid="app-pane"
      aria-hidden={!visible}
      // THE MOVEMENT THE BOARD DRAWS. `T2Sliding` is an artboard of this one transition, caught
      // halfway, with an annotation that says exactly what it is: the app card sliding out to the
      // right and fading as it goes, and "nothing about the app is stopped or reloaded — it is
      // only taken off the screen".
      //
      // THE ANIMATION IS ON THE HIDE TREATMENT, NEVER ON THE MOUNT, and that distinction is the
      // whole reason this is safe. The element is not conditionally rendered — it is the same node
      // throughout, with a class change — so the frame inside it is untouched by the movement. An
      // enter/exit animation that keyed on mounting would remount the iframe, which is the one
      // thing this host exists to forbid.
      //
      // THE LEAVE IS DRAWN BY THE COLUMN ABOVE, not here, and that is the whole of the split: one
      // keyframe on one element, so the card fades once rather than twice. What this element owes
      // the movement is its SIZE — collapsing to `w-0` while the column is still animating would
      // leave the column playing a keyframe over nothing.
      className={
        visible
          ? 'flex-1 min-w-0 overflow-hidden animate-pane-return'
          : leaving
            ? // ON ITS WAY OUT, at full size, because the column above is playing a keyframe over
              // this element and `w-0` would leave it playing over nothing.
              //
              // THE FOCUS CONTAINMENT IS NOT MISSING FROM THIS ARM, it is one level up: the
              // section carries `inert` for as long as the pane is unwanted, and `inert` covers a
              // whole subtree, so the fading frame is out of the tab order for the entire hold.
              // Do not reach for `HIDDEN_BUT_MOUNTED` here to close a gap that is already closed
              // — it would delete the movement it is meant to protect.
              'flex-1 min-w-0 overflow-hidden'
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
        // THE THREE THAT COME OFF THE ADDRESS, and liveness is one of them now: what to frame, what
        // to say about it, and whether anything is still answering there. All three outlive the
        // surface that published them, together, which is what "the pane is rendered by the address"
        // has to mean if it is to survive a leave.
        serving={address.serving}
        device={device}
        reloadNonce={reloadNonce}
      />
    </div>
  )
}
