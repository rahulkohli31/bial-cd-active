import { createConversationStore, deriveTitle } from './conversationApi'

// Planning-chat history, server-backed (kind 'plan'). The async store logic
// lives in the shared factory, which builderHistory.js mounts by kind alone.
// U7: `appendMessage` is gone (the server persists turns itself); the send path
// calls `createConversation` before the first turn instead. `newConversation`
// stays synchronous (mints a UUID).
// U1 collapsed the old three-value ConversationKind + ask/plan/write ConversationMode into one
// two-valued ChatKind (plan | build); the server 422s on the retired 'planning' string.
const store = createConversationStore('plan')

export const { loadHistory, newConversation, getConversation, createConversation } = store

export { deriveTitle }

/* `relativeTime` AND `deleteConversation` ARE GONE (plan 002, U3), and both died with the same
   caller: the project rail's list of past conversations. `relativeTime` rendered each row's "1h
   ago"; `deleteConversation` was reached only through that list's ⋮ menu. The ruling of
   2026-09-02 is that nothing points back to a chat, running or finished, so there is no row to
   date and no row to delete. Chats stay in the database — cleanup, if it is ever wanted, is a
   scheduled job rather than a control. Named here rather than deleted in silence, because an
   export that simply stops existing tells the next reader nothing about why. */
