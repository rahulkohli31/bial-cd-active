# ADR-0018: Generated Apps — Next.js, TypeScript, and shadcn/ui, Open Package Registry

## Context

The AI orchestrator builds real, multi-file applications for citizen developers, and the platform
controls the technology stack centrally: what framework, language, and UI component library an
app is built on. Generated code is untrusted, so where that code may reach is a security
boundary, not a style preference (ADR-0014).

The platform does not centrally control which packages an app may install, or the shape an app
takes. Both are the building agent's call: the agent has a real shell inside its sandbox and may
install whatever the application needs from the public npm registry, and the starting template is
an editable starting point rather than a fixed scaffold every app must keep. The security boundary
that matters is not the package registry — it is the sandbox the code runs inside (ADR-0014).

## Decision

**Generated apps are full-stack Next.js applications, written in TypeScript only, using
shadcn/ui** for UI components. Server rendering and route handlers are used where they add real
value; client rendering is used otherwise. Server-side rendering is not mandated for every route —
the goal is a real application with a server where one is needed, not server rendering
everywhere.

**The package set is open.** The sandbox resolves dependencies against the public npm registry,
and the building agent may install anything an application needs. The base sandbox image ships
with a pre-installed set of dependencies as a speed base — so a session starts fast — not as a
frozen or enforced set; a restored session reconciles its dependency lockfile against whatever the
agent has since installed. What makes this acceptable is containment inside the sandbox, not a
gatekept registry: generated code runs as an unprivileged process in a single-tenant container,
behind a fail-closed environment allowlist, with no credential the application's own process can
read (ADR-0014). The honest residual risk is that a malicious or typosquatted package is not
physically unavailable to the agent; the mitigations that exist are sandbox-side, plus the human
approval gate an app passes through before it is deployed.

**Generated apps compile under TypeScript's strict mode.** The build verification step that the
orchestrator runs after every change is a type-check; a type error is build feedback the agent
iterates on, the same as a failing command.

**The template and base image pin the current stable release of each library at authoring time,
not a floor the agent is expected to track.** This keeps every new session starting from a known,
byte-identical dependency graph — which matters because the agent is working from that graph, not
re-deriving it — while staying current as of whenever the image was last rebuilt. The template is
a starting point the agent may edit or extend, not a ceiling: it may add dependencies beyond it,
and nothing in this decision freezes it in place. The authoritative pinned versions live in the
generated-app template's own package manifest and lockfile in the repository, not in this record —
restating a version number here would only go stale.

**Data access is through Drizzle, against a PostgreSQL database dedicated to the app's own
project.** Each generated app owns its schema, authored by the building agent directly in the
template's schema module, reached through a server-only, runtime-injected connection string that
is never baked into the built artifact and never reachable from client-side code. A schema change
is made through a dedicated build step that generates a migration from the agent's schema edit and
applies it in one call — never through an ad hoc shell command against the database — so every
schema change leaves a versioned migration artifact rather than an unrecorded, ad hoc change. The
per-project database's isolation model — its own database, its own scoped role, a capped
connection pool — is the subject of ADR-0028; this record only carries forward that data access
for these apps is Drizzle against that database, not an HTTP client to a shared platform data
service.

## Consequences

- Every generated app shares one stack and one base image that the platform can patch — but not
  one fixed file shape. The starting template is a starting point an agent may reshape, not an
  enforced scaffold every app must retain.
- The dependency supply chain is open: an unapproved or unvetted package can be installed by the
  agent. The mitigations against that are entirely sandbox-side — a single-tenant, unprivileged
  container with no readable credential for the app's own process to abuse — plus the human
  approval gate before an app is deployed. There is no registry-level control layer.
- Dependency footprint is per-app and effectively unbounded: each deployed app carries whatever
  dependencies its own building agent chose. Responding to a vulnerability in a widely-used
  package means checking it against each app's own dependency set individually, not patching one
  curated list once for the whole fleet. This is the accepted cost of letting the agent install
  freely rather than working from a fixed, pre-approved dependency set.

## Related

- ADR-0014 (the sandbox these apps are built and run inside during development)
- ADR-0028 (the per-project database and app-owned-schema isolation model that Drizzle data access
  in these apps relies on)
