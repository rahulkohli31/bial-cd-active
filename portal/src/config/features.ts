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
 * .pptx is OFFERED in the UI; the server independently enforces its own gate
 * (DECK_ATTACHMENTS_ENABLED env + a reachable GOTENBERG_URL) and rejects .pptx
 * cleanly when off. Enabling the feature means flipping BOTH this flag and the
 * server env — that pair is the whole "turn it on".
 */
export const DECK_ATTACHMENTS_ENABLED = true
