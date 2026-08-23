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
 */
export type CompileState = 'building' | 'clean' | 'failed' | 'unknown'

/**
 * How long the container holds a `clean` before publishing it, in milliseconds.
 *
 * Named here so the pane can state — and test — how quickly its cover clears without keeping a
 * second copy of a number the container owns. Mirrors `_COMPILE_DEBOUNCE_S` in
 * `sandbox/supervisor/app.py`; if that moves, this moves.
 *
 * The debounce is deliberately asymmetric on the container side: `building` and `failed`
 * publish immediately and only `clean` waits, because the covering states are the safe ones
 * and the clearing state is the one that can lie.
 */
export const COMPILE_DEBOUNCE_MS = 400

/** Narrow a wire value to a `CompileState`. Anything unrecognised is `unknown`, never a guess. */
export function asCompileState(value: unknown): CompileState {
  return value === 'building' || value === 'clean' || value === 'failed' ? value : 'unknown'
}
