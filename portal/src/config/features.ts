/**
 * UI feature flags (interim, hardcoded). Single source of truth for whether a
 * feature is surfaced in the running app.
 *
 * The flag that hid the general-assistant chat is gone along with the feature itself.
 * Conversation-create now requires `header.projectId` for every kind, so a
 * project-less assistant chat is unbuildable — and `/chat/:chatId` is the URL that
 * project chats own.
 */

// The old deploy feature flag is DELETED, not flipped (team doctrine: remove
// non-functional affordances). The approval surfaces it hid are now real and
// always on: the citizen submit control (SubmitControl) and the Admin → App
// Registry review queue.

/**
 * DECK_ATTACHMENTS_ENABLED surfaces PowerPoint (.pptx) chat attachments in the
 * composer (file picker + drag/drop allowlist). A deck is converted to a PDF by
 * an in-tenant Gotenberg sidecar and read by Claude with vision — unlike Word/
 * Excel, which are read as extracted text. This client flag only controls whether
 * .pptx is OFFERED in the UI.
 *
 * THE SERVER HAS NO FLAG OF ITS OWN. An earlier version of this comment described a
 * server-side `DECK_ATTACHMENTS_ENABLED` env as the other half of the pair; there is
 * no such setting anywhere in `backend/` or the Express server. The real gate is
 * `deck_attachments_enabled()` (backend/src/services/extract/deck.py), which is just
 * `bool(GOTENBERG_URL)` — configuring the sidecar IS enabling the feature, and the
 * server rejects .pptx cleanly (501) whenever that URL is unset. So "turn it on" is:
 * point GOTENBERG_URL at a reachable sidecar, and flip this flag. Restating the wrong
 * model was worth correcting here (#157 review) because it is the sentence whoever
 * ships the feature will act on.
 *
 * OFF, because ON was only ever half of that pair (#157 B2). No sidecar is configured,
 * so .pptx appeared in the picker and staged a slide chip in the composer, and then Send
 * failed every time with "PowerPoint attachments aren't enabled" — a capability
 * offered and refused at the last step. Turning this off cannot cost anyone a working
 * capability: the client-side wire strip in `attachmentStore` makes decks a no-op even
 * where GOTENBERG_URL is set.
 *
 * The flag is load-bearing for COPY as well as offering: `attachmentInput`'s legacy-.ppt
 * message and the Help FAQ's attachment answer both branch on it, because advice to
 * "save as .pptx" is only followable while .pptx is accepted. Its shipped value is
 * asserted against the real, unmocked import in attachmentInput.test.js.
 */
export const DECK_ATTACHMENTS_ENABLED = false
