/**
 * HIDDEN, NOT UNMOUNTED — one treatment, one definition, with its reason attached (Plan A, U5).
 *
 * `visibility:hidden` rather than `aria-hidden` or zero width alone, and the difference is not
 * cosmetic. Zero width plus `overflow:hidden` clips a subtree visually but does NOT remove its
 * descendants from the tab order, so `aria-hidden` alone left a collapsed panel's composer, Send
 * and attach controls keyboard-reachable — a WCAG 4.1.2 violation. `visibility:hidden` drops the
 * whole subtree from BOTH the tab order and the accessibility tree while leaving it MOUNTED, which
 * is what keeps the draft, the scroll position and any in-flight stream intact.
 *
 * That last clause is the requirement, not an optimisation: hiding a conversation must never
 * become unmounting one, or its stream is aborted and its scroll position is gone the moment
 * something else takes the screen.
 *
 * ITS OWN MODULE, holding a string and a paragraph, for one reason: every alternative home imports
 * a page. The conversation slot is the component that applies this treatment and would have been
 * the natural place for it, but the builder surface's chat-panel collapse is its other caller —
 * and a page importing the slot that mounts pages is a cycle that drags the whole other chat kind
 * into every one of that page's fifteen test suites. A leaf module is what keeps one definition
 * possible at all.
 *
 * Callers compose it with their own layout classes; the class itself is the load-bearing part, and
 * `aria-hidden` beside it is belt-and-braces rather than the mechanism.
 */
export const HIDDEN_BUT_MOUNTED = 'invisible'
