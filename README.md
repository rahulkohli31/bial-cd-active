# BIAL Citizen-Developer Platform

An internal platform on which people who do not write code build working web applications by
describing what they want.

Someone writes a few sentences describing the application they need. An AI agent builds it, and
they see it running in the same page moments later. They refine it by carrying on the
conversation. When it does what they want, they submit it for approval, and an approved
application is deployed for their colleagues to use.

Everything the platform produces is generated code that no person has reviewed line by line, and
everyone using it is a colleague rather than a developer. Those two facts shape most of how the
system is built — `documentation/architecture.md` explains how.

## The repository

| Directory | What it holds |
|---|---|
| `backend/` | The control plane: the HTTP API, the build orchestration, and the approval workflow. |
| `portal/` | The single-page application people use, and the web edge that serves it. |
| `sandbox/` | The build supervisor and the project template every generated application starts from. |
| `documentation/` | This edition — architecture, deployment, decision records, runbooks, reference. |

## Documentation

| | |
|---|---|
| [`documentation/architecture.md`](documentation/architecture.md) | How the system is put together and why. Start here. |
| [`documentation/deployment.md`](documentation/deployment.md) | The services the platform needs, how an image reaches runtime, and how to prove a deployment worked. |
| [`documentation/adr/`](documentation/adr/) | The decisions in force, each with the reasoning that produced it. |
| [`documentation/runbooks/`](documentation/runbooks/) | Operating and recovering the platform, and building the test database. |
| [`documentation/reference/`](documentation/reference/) | The generated API reference and the permissions the platform requires of its host. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Toolchains, how to run the gates, and the conventions this code is written to. |

## Prerequisites

| Tree | Toolchain |
|---|---|
| `backend/` | Python 3.14, `uv` |
| `portal/` | Node 20 or newer — the shipped image builds on 24 — and `npm` |
| `sandbox/` | Python 3.14 via `backend/`, Node 24 in the image |

`CONTRIBUTING.md` covers installing these and running the checks. The static checks — linting, three
type checkers, and the API reference comparison — need nothing beyond the toolchain above, and run
on a fresh clone.

## What a clone cannot do on its own

**This repository does not provision the platform, and there is no single command that starts it
locally.** Running the control plane means supplying three things the repository cannot create:

- **A PostgreSQL instance.** The platform's own database, and one database per project for
  generated applications.
- **A Redis instance.** Build slots, live-session state and background-job scheduling.
- **An application registration in the organisation's directory**, for sign-in.

These come from the tenant. `documentation/deployment.md` describes what each is for and which of
them need somebody with authority the platform does not hold — those are worth starting early.

The backend test suite additionally needs a PostgreSQL database built to
[`documentation/runbooks/test-database-setup.md`](documentation/runbooks/test-database-setup.md).
