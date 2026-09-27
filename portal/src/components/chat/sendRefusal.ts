/**
 * A refusal whose message was WRITTEN FOR THE CITIZEN and is therefore safe to show.
 *
 * WHY THIS EXISTS
 *
 * THE TYPE IS THE PERMISSION. Other `onSubmit` rejections (a `TypeError`, an abort the surface
 * already explained in its own banner) must never surface via `err.message`, or developer text —
 * or a second, differently-worded banner — lands in front of someone asking for an app.
 * `silent` means "reject, but say nothing" for two cases that must still reject so the composer
 * doesn't empty for a press that sent nothing: an in-flight duplicate nobody knowingly made, and
 * a send someone else already answered (`RailComposer`'s held-workspace dialog; `handleSubmit`'s
 * abort arm for the banner `fireRelayTurn` already wrote). A plain
 * `Error` there is not silence, it's the GENERIC catch-all overwriting the specific sentence
 * already on screen — an over-long PDF once got "try again" instead of the real page limit, and
 * trying again could never work: the PDF was still over the limit either way.
 * Lives in its OWN module so `ComposerBox` can `instanceof`-check it without a cycle through
 * `Composer` — replaces a duck-typed `err.name` check plus an unchecked cast for `silent`.
 */
export class SendRefusal extends Error {
  readonly silent: boolean
  constructor(message: string, opts: { silent?: boolean } = {}) {
    super(message)
    this.name = 'SendRefusal'
    this.silent = opts.silent ?? false
  }
}
