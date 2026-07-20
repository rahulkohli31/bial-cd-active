/**
 * Exhaustiveness guard for a discriminated union / enum switch.
 *
 * Ending a `switch` in `default: return assertNever(x)` makes adding a new union
 * member a COMPILE error right here — the `never` parameter stops accepting the
 * leftover variant, so the type checker points at the unhandled case
 * (`.claude/rules/fail-first-typescript.md`). At runtime it throws, so a value that
 * somehow slipped past the type system (a hand-cast, a widened `any`) fails loud
 * instead of falling through silently. Callers that parse untrusted wire input drop
 * unknown variants at the boundary BEFORE dispatch, so this line stays unreached in
 * practice — it is the seatbelt, not the road.
 */
export function assertNever(value: never): never {
  throw new Error(`Unhandled union member: ${JSON.stringify(value)}`)
}
