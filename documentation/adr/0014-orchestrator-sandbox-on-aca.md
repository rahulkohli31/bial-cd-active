# ADR-0014: Orchestrator and Sandbox Execution on Azure Container Apps

## Context

The platform builds real, multi-file Next.js applications for citizen developers. An AI
orchestrator writes code, runs shell commands, reads build and runtime errors, and iterates —
inside a dev sandbox running a live Next.js dev server with hot-reload and a live preview. The
code the orchestrator writes and runs is **untrusted**: it is generated from natural-language
prompts, may install arbitrary packages, and is never reviewed before it executes. This needs an
execution environment that isolates that code, and a transport for the orchestrator to reach into
it.

Sandbox options considered:

1. Run the build loop directly on the control-plane host — no isolation between citizen-written
   code and the platform's own process. Rejected outright.
2. Build and operate custom per-project container infrastructure — heavy to run and patch
   ourselves, for a problem a managed platform already solves.
3. Azure Container Apps — a managed container platform already inside the organization's Azure
   tenant, with per-app scaling and network controls available if needed later.

Transport options for reaching into a running sandbox:

1. The container platform's own exec channel — a control-plane shell, not a programmable
   per-request API.
2. An HTTP server running inside the sandbox, with the sandbox pushing updates out.
3. A long-lived WebSocket held open from the sandbox — fragile across container restarts and
   redeploys.

## Decision

**Each user's sandbox is a dedicated Azure Container Apps application, scaled to exactly one
replica** (`minReplicas = maxReplicas = 1`). One replica keeps a citizen's workspace and dev-server
state coherent — there is never a second instance to route a request to inconsistently. The
workspace lives on the container's own local disk, not a network file share. Because a container
filesystem's own change notifications are not guaranteed to fire reliably across every host, the
dev server watches the filesystem in polling mode rather than relying on them.

Durability across restarts comes from **snapshotting, not from keeping the container alive**: the
workspace is periodically bundled with git and written to blob storage. When a sandbox is torn
down and later resumed, a fresh container is provisioned and the most recent snapshot is restored
onto it before the dev server starts. A container that stays running longer is a performance
convenience — a faster resume — never a correctness requirement, since nothing depends on any
single container surviving.

**A supervisor process runs inside every sandbox and is the only way in.** It exposes a small HTTP
API: run a shell command and get back structured output, read and write files under the workspace,
and start, restart, or check the status of the dev server, including its recent logs. Writing a
file through the supervisor lands it on the container's local disk, which is what makes the dev
server's hot-reload fire. An in-container reverse proxy routes one path prefix to the supervisor
and everything else to the dev server, so the supervisor and the citizen's running app are never
reachable through the same route.

**The supervisor is guarded by a per-session bearer token that only the control plane holds.**
Every child process the supervisor spawns — `npm`, the dev server itself — runs under a
scrubbed environment built from an explicit allowlist of names, not a denylist: the token, and
any other secret-shaped value, is excluded by default and must be named to pass through. This
means the generated application code running inside the same container can never read the token
or make a supervisor call in its own right — closing off code execution inside the sandbox as a
path to escalating past the sandbox's own boundary.

**Updates flow out of the sandbox by push, not by a held connection.** Progress, errors, and
console output are posted by the supervisor back to the control plane as they happen, which then
streams them on to the citizen's browser. Nothing requires a persistent connection to stay open
from the sandbox itself; a sandbox that has stopped responding is detected by a liveness timeout
rather than a dropped socket. Idle sandboxes — no activity for a bounded period — are torn down on
a scheduled sweep and rehydrated from their last snapshot the next time the citizen returns.

**Network posture:** the sandbox is reachable over a public ingress endpoint, and outbound network
access from inside the sandbox is not restricted. Restricting the network — a private ingress
endpoint, egress limited to an allowlist — is a deliberate, recorded choice to defer, not an
oversight: it is additive and does not change the supervisor's role as the sole entry point, so it
can be added later without reworking anything described here. The trigger to revisit it is an
actual requirement for network isolation, not a scheduled review.

There is no gateway mediating shared platform capabilities — model access, search, email — for
generated applications; nothing of that kind exists for an app to reach, so there is nothing to
allowlist on that front. The orchestrator itself calls the model service directly; the boundary
this ADR is about is the sandbox the generated code runs in, not a capability broker in front of
it.

## Consequences

- Isolation, resource limits, and container lifecycle are managed by the platform; the platform
  keeps control only over the image the containers run.
- The supervisor is a clean, testable seam: the orchestrator talks HTTP to a fixed API surface,
  never a shell attached directly to the container.
- Single replica plus local-disk workspace keeps hot-reload coherent for one citizen's session;
  the git-bundle-to-blob-storage snapshot supplies durability across restarts without a
  persistent network-mounted filesystem.
- A per-session token that the generated app's own process can never read narrows what a
  compromised or malicious dependency inside the sandbox can reach — it cannot impersonate the
  supervisor or ride its credentials.
- With public ingress and open egress, a dependency that reaches out to an external host during a
  build is not prevented from doing so. This is an accepted, revisitable trade rather than a
  closed question, and it does not touch how the supervisor itself is reached or secured.
- A sandbox is a workspace, not the thing that ships. Publishing an application builds it from the
  snapshot captured at submission rather than from a live sandbox, so the container this record
  describes can be destroyed at any time without affecting anything already published.

## Related

- ADR-0004 (per-user isolation; this sandbox is where that isolation is enforced for
  AI-generated, unreviewed code)
- ADR-0011 (background jobs and the scheduled worker; the orchestrator is a standalone service and
  does not run as a queued job — the queue's passengers are scheduled reconcilers, including the
  idle-sandbox sweep)
- ADR-0015 (how images are built and reach runtime, and the cross-platform constraint that shapes them)
- ADR-0018 (the Next.js/TypeScript stack this sandbox builds)
- ADR-0029 and ADR-0030 (the worker holds authority to remove sandboxes directly, driven off the
  coordination store this ADR describes; ADR-0030 withdraws the Azure-as-fleet-of-record design
  ADR-0029 also proposed)
