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
 * THIS IS THE SINGLE FRONTEND SOURCE of what a kind is CALLED (R73) — and today it has exactly
 * ONE reader, the project page's chat list. Said plainly rather than as an aspiration, because
 * two surfaces still spell the words themselves: the composer's mode chooser
 * (`components/chat/ModeSwitcher.tsx`, a different axis — ask/plan/write, not the stored kind)
 * and the help page's prose. Both are named deferrals, not oversights: the picker is out of this
 * work's scope, and the help-page copy rides the release that narrows attachments. When they do
 * come, they read from here rather than restating. Narrowing or renaming the vocabulary is an
 * edit to this table.
 *
 * `kind` arrives as a plain `string` (`conversationApi` types it that way, and `ProjectPage`'s
 * `narrowChat` legitimately coerces a malformed row's kind to `''`), so the lookup is keyed on a
 * string and never on a union — the fallback is the type-safety, not a cast.
 */
import { MessageSquare, Wrench, type LucideIcon } from 'lucide-react'

export interface ChatKindPresentation {
  /** The word on the badge — what a citizen reads. Never the storage value (`builder`). */
  word: string
  /**
   * The rest of the badge's text, shown to a screen reader but not to the eye, so the element
   * reads as a phrase ("Build chat") and not as a bare noun. Stored rather than sliced off a
   * separate `phrase` field: a derivation would carry an unchecked invariant (that the phrase
   * starts with the word), and editing one half without the other would silently produce wrong
   * screen-reader text with nothing to catch it.
   */
  completion: string
  Icon: LucideIcon
  /** The icon plate's background tint. */
  tint: string
  /** The icon's own colour. */
  iconTint: string
}

/** One record per kind the API can send. Keyed on the wire value, not on a display name. */
export const CHAT_KINDS: Readonly<Record<string, ChatKindPresentation>> = {
  builder: {
    // "Build chat"
    word: 'Build',
    completion: ' chat',
    Icon: Wrench,
    tint: 'bg-secondary/10',
    iconTint: 'text-secondary',
  },
  planning: {
    // "Plan chat"
    word: 'Plan',
    completion: ' chat',
    Icon: MessageSquare,
    tint: 'bg-primary/10',
    iconTint: 'text-primary',
  },
}

/**
 * The named fallback: `assistant`, `''`, and any value this vocabulary does not have yet. Its
 * word is the whole phrase, so the badge has no hidden half to read out.
 */
export const UNKNOWN_CHAT_KIND: ChatKindPresentation = {
  word: 'Chat',
  completion: '',
  Icon: MessageSquare,
  tint: 'bg-neutral/10',
  iconTint: 'text-neutral',
}

/** How to present one chat row's kind. Never throws, never returns undefined. */
export function chatKindFor(kind: string): ChatKindPresentation {
  return CHAT_KINDS[kind] ?? UNKNOWN_CHAT_KIND
}
