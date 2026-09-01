import { createConversationStore, deriveTitle } from './conversationApi'

// Planning-chat history, server-backed (kind 'plan'). The async store logic
// lives in the shared factory, which builderHistory.js mounts by kind alone.
// U7: `appendMessage` is gone (the server persists turns itself); the send path
// calls `createConversation` before the first turn instead. `newConversation`
// stays synchronous (mints a UUID).
// U1 collapsed the old three-value ConversationKind + ask/plan/write ConversationMode into one
// two-valued ChatKind (plan | build); the server 422s on the retired 'planning' string.
const store = createConversationStore('plan')

export const { loadHistory, newConversation, getConversation, deleteConversation, createConversation } = store

export { deriveTitle }

export function relativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
