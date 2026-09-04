/**
 * Builder-session store, server-backed (kind 'build'), built on the shared async factory. The
 * plan-kind sibling that used to sit beside it is gone (plan 002, U3): the plan chat reaches
 * `conversationApi` directly, so there was a module left holding nothing but re-exports.
 *
 * READ-ONLY NOW. This store lists a project's build chats (`loadBuilds`) and reloads one from the
 * server-side projection (`getBuild` → derived display messages). It creates nothing: a build
 * row's parentage — its project, its kind and its title — rides the FIRST TURN's own request and
 * is written inside that turn's transaction (`startTurn`'s `create` block in `turnStreamApi.ts`),
 * so a workspace refusal rolls the row back instead of leaving a titled, empty build chat in the
 * project. The `createBuild` wrapper that made the separate round trip went with it (plan 001,
 * unit 6). The legacy `code` header snapshot died with its column (migration 0024); code truth
 * lives in the app registry + build snapshots.
 *
 * What comes back is a `ConversationHeader` plus the projected messages — see `conversationApi.ts`
 * for both shapes.
 */
import { createConversationStore, deriveTitle } from './conversationApi'

// U1 collapsed the old three-value ConversationKind + ask/plan/write ConversationMode into one
// two-valued ChatKind (plan | build); the server 422s on the retired 'builder' string.
const store = createConversationStore('build')

export const loadBuilds = store.loadHistory
export const getBuild = store.getConversation

export { deriveTitle }
