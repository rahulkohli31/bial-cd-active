/**
 * A refusal whose message was WRITTEN FOR THE CITIZEN and is therefore safe to show.
 *
 * The type is the permission. `onSubmit` can reject for reasons that are nobody's business on
 * screen — a `TypeError` from a bug, an aborted send the surface has already explained in its own
 * banner with the server's wording — and showing `err.message` for those would put developer text,
 * or a second differently-worded copy of the banner, in front of someone asking for an app. Only
 * this class means "say this out loud".
 *
 * `silent` covers the press the surface swallowed because an identical one is already in flight:
 * the citizen did not knowingly make it, so there is nothing to report — but it must still reject,
 * because resolving would empty the composer for a press that sent nothing.
 *
 * IT LIVES IN ITS OWN MODULE so that `ComposerBox` can test for it with `instanceof`. `Composer`
 * imports `ComposerBox`, so a class exported from `Composer` is not reachable from inside the box
 * without a cycle — which is why that check used to be a duck-typed `err.name === 'SendRefusal'`
 * plus an unchecked cast for `silent`. A leaf module both can import costs nothing and makes the
 * type the permission everywhere, exactly as the paragraph above claims.
 */
export class SendRefusal extends Error {
  readonly silent: boolean
  constructor(message: string, opts: { silent?: boolean } = {}) {
    super(message)
    this.name = 'SendRefusal'
    this.silent = opts.silent ?? false
  }
}
