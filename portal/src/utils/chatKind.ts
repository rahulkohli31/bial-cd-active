/**
 * What a chat's KIND is called, and how a row draws it — ONE table, not a predicate: an
 * exhaustive lookup with a NAMED fallback ("Chat", never "Unknown"/"Assistant"), covering
 * every value the field can hold today plus one honest answer for tomorrow's.
 *
 * `word`/`description` are RE-POINTED AT THE SERVER, from `chat_kinds` on the bootstrap
 * (`utils/auth.ts`'s `UserProfile.chat_kinds`, mirroring backend's `CHAT_KIND_CATALOGUE`) —
 * never literals here. Only `Icon`/`pillIcon`/`pill`/`completion` stay LOCAL: the server has
 * no notion of a Lucide icon or a Tailwind class. `kind` arrives as a plain `string`, never a
 * union, so the lookup is keyed on a string and the fallback IS the type-safety, not a cast.
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
   * it from here instead of writing their own when they arrive. */
  description: string
  Icon: LucideIcon
  /**
   * THE GLYPH THE KIND PILL DRAWS — a different question from `Icon`, answered differently per
   * board: PLAN draws an 11px message-square in its pill; BUILD draws the word alone (the two
   * transition boards draw a wand glyph on a different palette, but the primary boards outrank
   * them). `Icon` still answers "what mark stands for this kind" for the rail's picker, where
   * both kinds carry one — `null` here is a real answer: this kind's pill is a word, not a mark.
   */
  pillIcon: LucideIcon | null
  /**
   * The kind PILL's own colours — a text/ground pair, applied to the caps pill beside a chat's
   * title. LOCAL: the server has no opinion on Tailwind classes. The pair is the board's, not a
   * choice made here (BUILD #8C5D1E on #FFF4E0, PLAN #0A5C5F on #E0F5F6, both owned as tokens);
   * the pill is a LABEL, never an action, which is why gold may appear here but not in a button.
   */
  pill: string
  /**
   * What the empty message box invites, for a chat of this kind that does not exist yet. LOCAL,
   * like `completion`: UI grammar, not part of what a kind IS. Lives HERE, not as a `kind ===
   * 'plan' ? … : …` at the render site — per-kind branching under `pages/` and
   * `components/workspace/` is forbidden MECHANICALLY (that scatter is how the two-page era
   * grew). Fixes a real bug: the rail once asked for "the change you need" in BOTH kinds,
   * contradicting the sentence above it promising a Plan chat changes nothing.
   */
  composerPlaceholder: string
  /** What the line under the composer says while a turn runs in a chat of this kind, or `null`
   *  for the composer's own sentence. LOCAL, like the placeholder. */
  composerRunningNote: string | null
}

/**
 * The LOOK of each kind: everything about presenting a kind that is not part of what the kind
 * IS, and therefore has no business crossing the wire. Keyed on the wire value (`ChatKind`'s own
 * `.value` — "plan" / "build"), the same key the bootstrap catalogue itself uses, so
 * `chatKindFor` does one lookup here and one into the catalogue rather than two different keys
 * that could quietly drift apart.
 */
const CHAT_KIND_LOOKS: Readonly<
  Partial<
    Record<
      string,
      Pick<ChatKindPresentation, 'completion' | 'Icon' | 'pillIcon' | 'pill' | 'composerPlaceholder' | 'composerRunningNote'>
    >
  >
> = {
  build: {
    completion: ' chat',
    Icon: Wrench,
    // The word alone — see `pillIcon`. The wrench belongs to the picker, not to the pill.
    pillIcon: null,
    pill: 'bg-accent-light text-secondary-800',
    composerPlaceholder: 'Describe the change you need…',
    composerRunningNote: 'Builds usually take several minutes and keep going if you leave. Keep typing if you like.',
  },
  plan: {
    completion: ' chat',
    Icon: MessageSquare,
    pillIcon: MessageSquare,
    pill: 'bg-primary-50 text-primary-dark',
    // Names no change, because a Plan chat makes none — the line above this box says so.
    composerPlaceholder: 'Describe what you have in mind…',
    composerRunningNote: null,
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
  // A kind we cannot name gets no mark either: the pill's whole content is the honest word "Chat".
  pillIcon: null,
  pill: 'bg-status-grey-bg text-status-grey-fg',
  // The kind is unknown, so the hint claims nothing about what sending will do.
  composerPlaceholder: 'Describe what you need…',
  composerRunningNote: null,
}

/**
 * How to present one chat row's kind. Never throws, never returns undefined.
 * TWO INDEPENDENT LOOKUPS, EACH WITH ITS OWN NAMED MISS. `Object.hasOwn` on
 * `CHAT_KIND_LOOKS`, not a bare index — `kind` is unvalidated wire data, so a kind of
 * `"constructor"` or `"toString"` would find a truthy `Object.prototype` value past a bare
 * `??`. And `.find()` against the catalogue array, not an index: no prototype collision
 * there, but it CAN legitimately come back empty, and that miss falls back the same way.
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
    pillIcon: look.pillIcon,
    pill: look.pill,
    composerPlaceholder: look.composerPlaceholder,
    composerRunningNote: look.composerRunningNote,
  }
}
