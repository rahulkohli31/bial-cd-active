/**
 * Builder-session store, server-backed (kind 'build'), built on the shared async factory. No
 * plan-kind sibling: the plan chat reaches `conversationApi` directly.
 *
 * READ-ONLY: lists a project's build chats (`loadBuilds`) and reloads one from the
 * server-side projection (`getBuild`). Creates nothing — the row is created by the SEND path,
 * which posts `createConversation` a round trip before the first turn because an upload has to
 * name a conversation the server has already written. That ordering replaced the `create` block
 * this comment used to describe, and it costs the guarantee that block bought: a refused first
 * message now leaves an empty chat rather than rolling the row back. Recorded in
 * `conversationApi.createConversation`, which is also where the returned shapes live.
 */
import { createConversationStore, deriveTitle } from './conversationApi'

// The old three-value ConversationKind + ask/plan/write ConversationMode collapsed into one
// two-valued ChatKind (plan | build); the server 422s on the retired 'builder' string.
const store = createConversationStore('build')

export const loadBuilds = store.loadHistory
export const getBuild = store.getConversation

export { deriveTitle }
