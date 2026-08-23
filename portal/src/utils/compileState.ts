/**
 * What the app's dev server is compiling right now — the signal the preview pane covers its
 * frame with (R17/R18).
 *
 * FOUR values, and the fourth is the whole point. `unknown` means the platform has no idea:
 * the container has not connected to its dev server's HMR socket yet, the socket is down
 * between reconnects, the container runs an image built before the signal existed, or the
 * transport failed. Every one of those must read as "no idea", NEVER as `clean` — a pane that
 * treats an absent signal as good news uncovers itself over the exact error screen the cover
 * exists to hide.
 *
 * Mirrors `CompileState` in `backend/src/services/sandbox/base.py`; the wire values are the
 * StrEnum's own, so the two lists are the same four strings.
 *
 * NOTE ON TIMING: the debounce that settles a `clean` before it is published lives entirely in
 * the container (`_COMPILE_DEBOUNCE_S` in `sandbox/supervisor/app.py`). This side keeps no copy
 * of it and runs no timer of its own — the pane is purely reactive to the state it is handed,
 * which is what makes "the cover clears one debounce after the app compiles" a property of one
 * number in one place.
 */
export type CompileState = 'building' | 'clean' | 'failed' | 'unknown'

/** Narrow a wire value to a `CompileState`. Anything unrecognised is `unknown`, never a guess. */
export function asCompileState(value: unknown): CompileState {
  return value === 'building' || value === 'clean' || value === 'failed' ? value : 'unknown'
}
