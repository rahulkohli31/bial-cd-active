/**
 * Plain-language names for audit actions.
 *
 * The stored action is a machine token (`publish_gate`, `classification_review`) chosen so it
 * can be queried and never reworded. That makes it exactly the wrong thing to put in front of
 * an administrator, who is reading this trail to answer "what happened to this app, and who
 * did it" — a question `classification_review` does not answer on its own.
 *
 * So the token stays canonical in the database and this module is the only place it is turned
 * into words. An action with no entry falls back to a readable form of the token rather than
 * rendering raw, so a new action added on the server is never worse than it is today.
 */

export interface AuditLabel {
  /** What happened, as a person would say it. */
  title: string
  /** One sentence of what it means. Absent where the title is already the whole story. */
  description?: string
}

const LABELS: Record<string, AuditLabel> = {
  // --- the pre-publish path, in the order it happens ---------------------------------
  classification_review: {
    title: 'Automatic data check',
    description:
      'The platform read the saved code and answered the six data questions itself.',
  },
  publish_gate: {
    title: 'Publish decision',
    description:
      'The developer asked to publish. The platform compared their answers with its own and decided whether the app could go live or needed a person.',
  },
  submit: {
    title: 'Sent for review',
    description: 'The app entered this queue, pinned to one exact version of the code.',
  },
  withdraw: {
    title: 'Withdrawn by the developer',
    description: 'The developer took the app back out of the queue before anyone decided.',
  },
  approve: {
    title: 'Approved',
    description:
      'An administrator accepted the data this app holds. The developer publishes it themselves.',
  },
  'approve:self': {
    title: 'Approved own app',
    description: 'An administrator approved an app they own. Recorded separately on purpose.',
  },
  reject: {
    title: 'Sent back for changes',
    description: 'An administrator declined it and wrote a note the developer receives.',
  },

  // --- lifecycle -------------------------------------------------------------------
  disable: { title: 'Disabled', description: 'Switched off, and stays off until re-enabled.' },
  enable: { title: 'Re-enabled', description: 'Switched back on after being disabled.' },
  unpublish: { title: 'Taken offline', description: 'The live app was removed from its address.' },
  'unpublish:unconfirmed': {
    title: 'Taken offline — not confirmed',
    description: 'Removal was requested, but the platform could not confirm it finished.',
  },
  'mark-deployed': {
    title: 'Marked as deployed',
    description: 'An administrator recorded that the go-live runbook was run.',
  },
  'config:loginRequired': {
    title: 'Sign-in requirement changed',
    description: 'Whether people must sign in to open this app.',
  },
  'app:delete': {
    title: 'App deleted',
    description: 'The app, its files and its database were permanently removed.',
  },
  'project:delete': { title: 'Project deleted' },

  // --- access to things that matter --------------------------------------------------
  'bundle:download': {
    title: 'Source code downloaded',
    description: "An administrator downloaded a copy of the app's code.",
  },
  'deploy-credential:mint': {
    title: 'Deploy credential issued',
    description: 'A short-lived credential was created so this app could be deployed.',
  },
  'db:reveal': {
    title: 'Database password viewed',
    description: "An administrator viewed this app's database password.",
  },
  'db:revoke': { title: 'Database access revoked' },
  'db:restore': { title: 'Database access restored' },
  'db:drop': { title: 'Database deleted' },

  // --- housekeeping the platform runs on itself ---------------------------------------
  'build_session.reap': {
    title: 'Idle workspace reclaimed',
    description: 'A build workspace was shut down after sitting idle.',
  },
  'db:reconcile': { title: 'Databases reconciled', description: 'Routine housekeeping.' },
  'deploy:reconcile': { title: 'Deployments reconciled', description: 'Routine housekeeping.' },
  'storage:reconcile': { title: 'Storage reconciled', description: 'Routine housekeeping.' },
  'sandbox:reconcile': { title: 'Sandboxes reconciled', description: 'Routine housekeeping.' },
  'sandbox:backfill-tags': { title: 'Sandbox tags backfilled' },
  'sandbox:reclamation_report': { title: 'Idle sandbox report run' },

  // --- people and quotas ---------------------------------------------------------------
  'limits:set': { title: 'Usage limit changed' },
  'limits:bulk_set': { title: 'Usage limits changed for several people' },
  'usage:reset': { title: 'Usage counter reset' },
  'user:deactivate': { title: 'User deactivated' },
  'user:reactivate': { title: 'User reactivated' },
}

/**
 * The words for one action. Unknown tokens are made readable rather than shown raw:
 * `some_new:action` becomes "Some new action", which is wrong in tone at worst — never
 * jargon, and never blank.
 */
export function auditLabel(action: string): AuditLabel {
  const known = LABELS[action]
  if (known) return known
  // `-` sits last in the class, where it is already literal — escaping it is what
  // `no-useless-escape` flags.
  const readable = action.replace(/[_:.-]+/g, ' ').trim()
  return { title: readable.charAt(0).toUpperCase() + readable.slice(1) }
}
