/**
 * Builder-session store, server-backed (kind 'build'), built on the shared async factory. The
 * plan-kind sibling that used to sit beside it is gone (plan 002, U3): the plan chat reaches
 * `conversationApi` directly, so there was a module left holding nothing but re-exports. U7: the server persists turns itself — the page
 * creates the row before the first turn (`createBuild`) and reloads via the
 * projection read (`getBuild` → derived display messages). The legacy `code`
 * header snapshot died with its column (migration 0024); code truth lives in the
 * app registry + build snapshots.
 *
 * A build header is `{ id, title, createdAt, updatedAt, context, mode }`:
 *   - context: generation settings (uploadedFiles), passed at create so
 *     refinements after a resume keep their configuration.
 */
import { createConversationStore, deriveTitle } from './conversationApi'

// U1 collapsed the old three-value ConversationKind + ask/plan/write ConversationMode into one
// two-valued ChatKind (plan | build); the server 422s on the retired 'builder' string.
const store = createConversationStore('build')

export const loadBuilds = store.loadHistory
export const getBuild = store.getConversation
export const createBuild = store.createConversation // (id, {projectId, title?, context?})

export { deriveTitle }
