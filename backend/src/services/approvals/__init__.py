"""Approvals — the submit-into-queue service, the ONE route into the admin
submit/approve queue (R15a, ASM9).

Module map (consumers import the module they need,
`from src.services.approvals import submit`):

* `submit` — the extracted submit body: the app-scoped build-session guard, the
  fail-closed bundle read, the blob-first-row-second copy, and the guarded UPDATE
  that moves the app to pending carrying its lineage and declaration.

The citizen-callable `POST /apps/{app_id}/submit` route this was extracted from is
RETIRED (ASM18): it was the only backend writer of the pending status, and leaving it
reachable would let a queue item arrive with no declaration attached. Its callers now
are the publish gate (U9, with the merged declaration) and any future admin-initiated
entry — both go through this service, which is what makes R15a's "exactly one route"
checkable by grep: no code path outside this package writes `AppStatus.PENDING`.
"""
