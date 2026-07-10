import { partsToText } from './attachmentStore.js'
import { createConversationStore, deriveTitle } from './conversationApi.js'

// Planning-chat history, server-backed (kind 'planning'). The async store logic
// lives in the shared factory, which builderHistory.js mounts by kind alone. The
// exported names are unchanged — loadHistory/getConversation/appendMessage/
// deleteConversation are async (return Promises); newConversation stays
// synchronous (mints a UUID).
const store = createConversationStore('planning')

export const { loadHistory, newConversation, getConversation, deleteConversation, appendMessage } = store

export { deriveTitle }

export function buildPromptFromHistory(messages) {
  const userMessages = messages.filter((m) => m.role === 'user')
  // partsToText so an attachment turn yields its prose, not "[object Object]".
  const goal = partsToText(userMessages[0]?.parts ?? '')

  const contextMessages = messages.slice(-10)
  const transcript = contextMessages
    .map((m) => `${m.role === 'user' ? 'User' : 'Assistant'}: ${partsToText(m.parts)}`)
    .join('\n\n')

  return `Based on the following planning conversation, build the described application:

User's goal: ${goal}

Planning session:
${transcript}

Build this app based on the planning session above. Incorporate all the features and requirements discussed.`
}

export function relativeTime(isoString) {
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
