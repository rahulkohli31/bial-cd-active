/**
 * Builder-session store, server-backed (kind 'builder'). Mirrors chatHistory.js
 * via the shared async factory. U7: the server persists turns itself — the page
 * creates the row before the first turn (`createBuild`) and reloads via the
 * projection read (`getBuild` → derived display messages). The legacy `code`
 * header snapshot died with its column (migration 0024); code truth lives in the
 * app registry + build snapshots.
 *
 * A build header is `{ id, title, createdAt, updatedAt, context, mode }`:
 *   - context: generation settings (theme/uploadedFiles), passed at create so
 *     refinements after a resume keep their configuration.
 */
import { createConversationStore, deriveTitle } from './conversationApi.js'

const store = createConversationStore('builder')

export const loadBuilds = store.loadHistory
export const newBuild = store.newConversation // sync UUID; row created before the first turn (U7)
export const getBuild = store.getConversation
export const deleteBuild = store.deleteConversation
export const createBuild = store.createConversation // (id, {projectId, title?, context?})

export { deriveTitle }
