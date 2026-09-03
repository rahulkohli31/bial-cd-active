/**
 * THE ID THE COLLAPSE CONTROL POINTS AT. The control that hides the rail is published into the
 * pane's toolbar — it has to be, because a collapsed rail is invisible and untabbable and a toggle
 * inside it would be a one-way door — so `aria-controls` is the only thing tying the two together
 * for anyone reading the markup or navigating it. One constant so the two ends cannot drift.
 *
 * ITS OWN LEAF MODULE, for the same reason as `devices.ts`: the shell renders the rail, so the id
 * lived there, and the two components that have to point at it — the toolbar and the pane — both
 * import the shell that renders THEM. That is a ring, and a ring only stays harmless while nobody
 * adds a top-level `const` to it.
 */
export const WORKSPACE_RAIL_ID = 'workspace-rail'
