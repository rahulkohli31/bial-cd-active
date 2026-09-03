/**
 * What a chat's KIND is called, and how a row draws it — ONE table, not a predicate.
 *
 * The project page used to answer this with `chat.kind === 'builder'`, a two-way test on a
 * THREE-valued field (`ConversationKind` is PLANNING | ASSISTANT | BUILDER), so an `assistant`
 * row fell into the else arm and drew the Plan icon. An icon can get away with that; a word that
 * says "Plan" cannot. Hence an exhaustive lookup with a NAMED fallback: every value the field can
 * hold today, plus one honest answer for every value it might hold tomorrow.
 *
 * THE FALLBACK'S WORD IS "Chat", deliberately. Not "Unknown" — citizen-hostile on a row someone
 * is about to click — and not "Assistant", which is a schema word, not a product word. "Chat" is
 * true of every value this field can carry, including ones that do not exist yet.
 *
 * U16/R73 RE-POINTED THE WORDING AT THE SERVER. `word` and `description` no longer live as
 * literals in this file — they come from `chat_kinds` on the once-cached `GET /auth/me` bootstrap
 * (`utils/auth.ts`'s `UserProfile.chat_kinds`, mirroring `backend/src/services/agent/toolsets.py`'s
 * `CHAT_KIND_CATALOGUE`), the same catalogue the toolset registry sits beside. Only `Icon` and
 * `pill` — and the screen-reader-only `completion` suffix, which is UI grammar rather than a
 * description of what a kind IS — stay LOCAL: the server has no notion of a Lucide icon or a
 * Tailwind class, and re-pointing those too would just move the "what does this look like"
 * decision somewhere it does not belong.
 *
 * THIS IS THE SINGLE FRONTEND SOURCE of what a kind is CALLED and what it DOES (R73). Its readers
 * are the toolbar row's kind pill and the rail composer's kind picker — the project page's chat
 * list was the original one and it is gone (plan 002, U3: nothing points back to a past chat).
 * Said plainly rather than as an aspiration, because one surface still spells the words itself:
 * the help page's prose, a named deferral rather than an oversight — the copy rides a later
 * release. When it does catch up, it should read the `description` this module already carries
 * rather than restating it.
 *
 * (The paragraph above used to name a SECOND surface too — the composer's mode chooser,
 * `components/chat/ModeSwitcher.tsx`, on a different axis, ask/plan/write, not the stored kind.
 * That axis is gone, not merely unmigrated: U1 collapsed conversation kind and the mode it
 * switched into one two-valued `ChatKind`, and U19 deleted `ModeSwitcher` with it. Recorded here
 * so this file does not go on describing a control that no longer exists.)
 *
 * `kind` arrives as a plain `string` (`conversationApi` types it that way, and `ProjectPage`'s
 * `narrowChat` legitimately coerces a malformed row's kind to `''`), so the lookup is keyed on a
 * string and never on a union — the fallback is the type-safety, not a cast.
 */
import { MessageSquare, Wrench, type LucideIcon } from 'lucide-react'
import { getStoredUser } from './auth'

export interface ChatKindPresentation {
  /** The word on the badge — what a citizen reads. Sourced from the bootstrap catalogue's
   * `name`, never a literal here, and never the storage value (`plan`/`build`). */
  word: string
  /**
   * The rest of the badge's text, shown to a screen reader but not to the eye, so the element
   * reads as a phrase ("Build chat") and not as a bare noun. LOCAL, not server-sourced: it is
   * UI grammar ("… chat"), not part of what a kind IS, so it has nothing to drift out of sync
   * with. Stored rather than sliced off a separate field: a derivation would carry an unchecked
   * invariant (that the phrase starts with the word), and editing one half without the other
   * would silently produce wrong screen-reader text with nothing to catch it.
   */
  completion: string
  /** The one line a citizen reads about what this kind of chat does for them — the bootstrap
   * catalogue's `description`, verbatim. Not rendered by today's one reader (the badge shows
   * only `word`), but carried here rather than dropped, so the composer and the help page read
   * it from here instead of writing their own when they arrive (R73). */
  description: string
  Icon: LucideIcon
  /**
   * The kind PILL's own colours — a text/ground pair, applied to the caps pill the canvas draws
   * beside a chat's title. LOCAL: the server has no opinion on Tailwind classes.
   *
   * The pair is the board's, not a choice made here: BUILD is #8C5D1E on #FFF4E0 and PLAN is
   * #0A5C5F on #E0F5F6, both of which this build already owns as tokens. The pill is a LABEL and
   * never an action, which is why gold is allowed to appear in it while nothing gold may fill a
   * button.
   */
  pill: string
}

/**
 * The LOOK of each kind: everything about presenting a kind that is not part of what the kind
 * IS, and therefore has no business crossing the wire. Keyed on the wire value (`ChatKind`'s own
 * `.value` — "plan" / "build"), the same key the bootstrap catalogue itself uses, so
 * `chatKindFor` does one lookup here and one into the catalogue rather than two different keys
 * that could quietly drift apart.
 */
const CHAT_KIND_LOOKS: Readonly<
  Partial<Record<string, Pick<ChatKindPresentation, 'completion' | 'Icon' | 'pill'>>>
> = {
  build: {
    completion: ' chat',
    Icon: Wrench,
    pill: 'bg-accent-light text-secondary-800',
  },
  plan: {
    completion: ' chat',
    Icon: MessageSquare,
    pill: 'bg-primary-50 text-primary-dark',
  },
}

/**
 * The named fallback: `assistant`, `''`, and any value this vocabulary does not have yet — or a
 * recognised value whose wording has not arrived yet (the bootstrap has not resolved). Its word
 * is the whole phrase, so the badge has no hidden half to read out.
 */
export const UNKNOWN_CHAT_KIND: ChatKindPresentation = {
  word: 'Chat',
  completion: '',
  description: '',
  Icon: MessageSquare,
  pill: 'bg-status-grey-bg text-status-grey-fg',
}

/**
 * How to present one chat row's kind. Never throws, never returns undefined.
 *
 * TWO INDEPENDENT LOOKUPS, EACH WITH ITS OWN NAMED MISS. `Object.hasOwn` on `CHAT_KIND_LOOKS`,
 * NOT a bare index — `kind` is unvalidated wire data (`narrowChat` passes through whatever
 * string the API sent), so a row whose kind is `"constructor"` or `"toString"` would find a
 * truthy value on `Object.prototype` and sail past a bare `??`. And a plain `.find()` against
 * the catalogue array, not an index either — an array has no `Object.prototype` collision to
 * guard against, but it CAN legitimately come back empty (the profile has not loaded, or the
 * server sent a kind this build's `CHAT_KIND_LOOKS` does not recognise yet), and that miss must
 * fall back exactly like an unknown wire value rather than rendering a half-built badge.
 */
export function chatKindFor(kind: string): ChatKindPresentation {
  const look = Object.hasOwn(CHAT_KIND_LOOKS, kind) ? CHAT_KIND_LOOKS[kind] : undefined
  if (!look) return UNKNOWN_CHAT_KIND
  // `?.` ON THE ARRAY TOO, not just on the profile. `UserProfile` is an unchecked cast over
  // whatever `/auth/me` returned (`utils/auth.ts` asserts the shape, nothing validates it), so
  // an absent `chat_kinds` is a wire fact rather than a type-system impossibility — a stale
  // service worker, a test double, or a server that predates the catalogue. It has to degrade
  // to the unknown badge exactly like an unrecognised value, not throw mid-render and take the
  // whole chat list down with it. Same promise the `Object.hasOwn` guard above keeps.
  const entry = getStoredUser()?.chat_kinds?.find((candidate) => candidate.value === kind)
  if (!entry) return UNKNOWN_CHAT_KIND
  return {
    word: entry.name,
    completion: look.completion,
    description: entry.description,
    Icon: look.Icon,
    pill: look.pill,
  }
}
