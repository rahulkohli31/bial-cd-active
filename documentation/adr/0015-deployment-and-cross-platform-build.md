# ADR-0015: Deployment and Cross-Platform Build Discipline

## Context

The platform's own images — the portal and the backend control-plane — are built by a
cloud build service invoked from a Windows host, then pushed to a container registry.
Development happens on Linux and macOS. That means the operating system that produces
the shipped artifact is not the operating system engineers build and test on day to
day, and this gap has already broken production once: a shell script was checked out
with Windows-style line endings, which turned its shebang line into something the
container's shell could not execute. The image built without complaint and ran
correctly on macOS; the same image failed immediately at container start once deployed,
because the extra carriage return made the interpreter path unresolvable.

The Dockerfile templates and build assets that ship inside every citizen-generated
app's own build context are themselves part of the backend image, so they are produced
on the same Windows host and are subject to the same risk, even though a generated
app's own image is built later by the container registry's own build service, on a
different, Linux-based build agent, once a citizen deploys it.

## Decision

**Cross-platform build discipline is a hard rule** for any code that runs on, or ships
to, the build host:

- Shell scripts and Dockerfiles are held to LF line endings by a repository-wide rule
  that normalizes them on checkout, so a Windows checkout cannot silently introduce
  carriage returns. Any Dockerfile that also carries a checked-in entrypoint script
  strips a trailing carriage return from it, as a second, independent guard, before
  making it executable — belt and braces against the same failure mode recurring
  through a path the checkout-time rule doesn't reach.
- File paths are OS-agnostic: no hard-coded `/`-only assumptions, and no reliance on a
  Unix-only shell for anything that has to run on the build host.
- Any script or tool invoked from the build path has to behave identically on Windows.
- A check that only runs the local macOS or Linux build is not verification that the
  Windows build succeeds. Such a check has to say so, rather than being read as proof
  the shipped artifact works.

**Deployment topology.** The portal and the backend API each run on Azure App Service
for Containers, selected by the container's own start command. The backend's second run
target — the Taskiq worker (ADR-0011) — runs instead on Azure Container Apps, which does
not require the container to answer inbound HTTP the way App Service does. Runtime
secrets are injected at container start in every case and are never baked into an
image.

## Consequences

- Environment-difference bugs — line-ending corruption, OS-specific APIs, path
  assumptions that hold on one platform and not the other — are treated as defects to
  prevent by rule, not surprises to debug after a deploy.
- Every change to a shell script, a Dockerfile, or a container entrypoint carries a
  cross-platform review, because code that runs cleanly wherever it was written can
  still fail on the one host that actually produces the shipped image.
- Rolling back the portal or the backend API means redeploying the previous image tag,
  since App Service for Containers has no revision mechanism of its own. Rolling back
  the worker means deactivating its current revision instead, since Container Apps
  keeps one — the same platform difference that puts the worker there in the first
  place also changes how it recovers from a bad deploy.

## Related

- ADR-0011 (the worker as the backend image's second run target)
