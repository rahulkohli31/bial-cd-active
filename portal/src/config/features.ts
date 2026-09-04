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
// always on: the citizen's publish chip (PublishStatusChip, which replaced the
// Publish card, the review-status card and the toolbar button) and the
// Admin → App Registry review queue.
//
// AND THERE IS NO FLAG FOR THE CHIP EITHER, deliberately. This file is the
// portal's only flag mechanism and it is compile-time constants — no runtime
// switch, no per-environment toggle — so there is no way to dark-ship the
// publish swap or roll it back without a redeploy. That is exactly why the
// three retired controls' 48 test cases were walked before they were deleted:
// the parity pass is the only safety net that exists here.
//
// AUDIT-2026-09-03 · verified-alive: intentionally retained pending verification — see the audit record.


