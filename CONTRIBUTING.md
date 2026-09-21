# Contributing

Three trees, three toolchains:

| Tree | What it is | Toolchain |
|---|---|---|
| `backend/` | FastAPI control plane | Python 3.14, `uv` |
| `portal/` | React + Vite single-page app | Node >=20 (the image builds on 24), `npm` |
| `sandbox/` | The build supervisor and the template every generated app starts from | Python 3.14 (via `backend/`), Node 24 in the image |

`README.md` introduces the platform and maps `documentation/`. This document covers the
toolchains, the checks, and the conventions the code is written to.

## Getting set up

```sh
cd backend  && uv sync --all-groups
cd portal   && npm ci
```

`sandbox/` has no environment of its own — its Python runs against `backend/`'s:

```sh
cd sandbox && uv run --project ../backend pytest -q
```

Copy `backend/.env.example` to `backend/.env` and fill it in. Required settings have no
defaults: the app refuses to boot rather than start half-configured, so an empty value is a
startup error, not a silent fallback.

Python dependencies change through `uv add` and `uv remove`, never by hand-editing the manifest,
so the lockfile moves with them. There is one `backend/pyproject.toml` and the dependencies sit in
four tiers: the shared runtime set, then `api`, `worker` and `dev` groups. The distinction earns
its keep at the image — a container that syncs without naming the group it needs ships without the
code it needs and dies at start, which is the most expensive moment to find out.

## Running the gates, and what each one proves

**Backend — static.** All six must pass. They need no database, no Redis and no secrets.

```sh
cd backend
uv run ruff check .                     # lint
uv run ruff format --check .            # formatting, including inside docstrings
uv run ty check                         # type check
uv run mypy src tests                   # type check, strict on src
uv run pyright src tests                # type check, third opinion
uv run python scripts/openapi.py --check  # the published API reference still matches the code
```

Three type checkers is not belt-and-braces for its own sake — they disagree, and the
disagreements are where the real bugs sit. Fix rather than suppress.

The last one compares `documentation/reference/openapi.json` against the surface this tree
actually serves. It proves the published API reference has not gone stale — production disables
the schema endpoint, so that committed file is the only reference a reader has, and nothing else
would notice it drifting. On a mismatch it names the operations that moved and prints the
regenerate command; it never repairs anything itself. **Route and Pydantic model docstrings
publish as the descriptions in that document**, so this gate can fail on a change that touched
nothing but prose — see "Some prose is load-bearing" below.

**Backend — tests.** These need a PostgreSQL database built to
[`documentation/runbooks/test-database-setup.md`](documentation/runbooks/test-database-setup.md),
including a `REVOKE CONNECT` on the control-plane database that several per-app database tests
assert against. Build it out of band before your first run.

```sh
cd backend && uv run pytest -q      # ~8 minutes
```

**Portal.**

```sh
cd portal
npx tsc --noEmit
npm run lint
npm run test
```

**Sandbox.**

```sh
cd sandbox
uv run --project ../backend pytest -q
uv run --project ../backend ruff check .
```

## How the backend is written

**A service-layer function earns a separate existence, or the query is inlined into the endpoint
that owns it.** It earns it in one of two ways: present-tense reuse — two or more call sites that
exist *now*, not a second caller somebody expects later — or a realized testing benefit, meaning a
direct unit test that pins behaviour below the HTTP layer because the behaviour warrants it, and
that has actually been written. A one-to-one wrapper around a single scoped query concentrates
nothing and costs a file, an import and a jump for every reader.

**Dependencies resolve before the route body runs.** A `Depends(...)` is evaluated eagerly, so a
dependency that raises produces its error *before* request-body validation, and a route that means
to report a missing dependency in its own words never gets the chance. Where a dependency may
legitimately be absent, the seam is split: one provider that raises and one that yields `None`, and
the route decides. This has been needed for three unrelated dependencies now, which is what makes
it a rule rather than a quirk of one of them. Splitting a provider in two also forks the key that
test overrides are registered under — a fake bound to one seam does not apply to the other, so both
get bound.

**Prefer `.returning(...)` over refreshing an object after commit.** Sessions here are configured
not to expire objects on commit, and server-side defaults mean the values a row was written with
are not always the values it now holds. Asking the database to hand back what it wrote, in the same
statement, avoids a second round trip and avoids the class of failure where a refresh is attempted
outside the context that can perform it.

**A partial index needs its predicate as a literal, not a bound parameter.** Connections are pooled
and long-lived, so prepared statements persist and the planner switches to a generic plan after a
handful of executions. Once it does, a partial index whose predicate pins a column value is
invisible to it — the parameter could be anything, so the index cannot be assumed to apply. Render
that predicate as a literal and the planner can see it. This has been applied three times; a fourth
partial index needs the same treatment.

## Writing tests

**A test must not compute its expected value with the code that produces the actual value.** When
both sides of an assertion run through the same function, a change to that function moves both
together and the assertion cannot fail — the test is green by construction and proves nothing. Pin
the expectation against an independent literal instead.

**Global styles carry an accessibility consequence that is easy to miss.** Styling scrollbars
suppresses the platform's own scrollbar rendering, and with it the exemption the platform's
defaults carry from contrast requirements. Once the repository styles them, meeting contrast is the
repository's job, and the values are asserted by a test rather than left to inspection.

## Branching and commits

Trunk-based, on short-lived branches off `main` that live a day or two. Prefixes: `feat/`, `fix/`,
`chore/`, `docs/`, `refactor/`. Conventional commit messages. Small pull requests, squash-merged.

Release automation that would commit generated artefacts into the repository is deliberately absent.

## The comment convention

The rule underneath all four of the following: **a comment never cites an identifier, a ticket
number, or a document that is not in this repository.** A reader with nothing but this
checkout has to be able to act on every word. If a citation was carrying the explanation,
the fix is to write the explanation, not to delete the sentence.

Also: no `NOTE:`/`IMPORTANT:` markers, no banner comments — the boxed
`===== SECTION =====` kind — no calendar dates used as history, and no archaeology: which
review round changed a thing, or what it used to be called, helps nobody reading the code
today. A plain `# --- helpers ---` divider is structure, not archaeology, and stays.

### Module docstring — 3 to 8 lines, hard ceiling 12

What this module owns, and the one or two constraints a caller gets wrong unaided.

```diff
-"""THE ROLLBACK FOR THE R22 REGISTRY PREFIX CUTOVER — mirror the new keys back onto the old shape.
+"""Mirror the environment-scoped sandbox registry back onto the pre-cutover key shape.
```

Lines here means lines that carry text; a blank line between paragraphs is not one, or a
docstring would be penalised for being readable. A module may exceed 12 lines only when it is
the single home of a durable fact — an incident the code is shaped around, a measurement that
justifies a threshold — in which case it says so under a `WHY THIS EXISTS` heading and stops
at 25. That allowance is module-scope; it never applies to a function.

### Function and class docstring — hard ceiling 8

State the contract. Do not restate the signature.

Counted the same way, text-carrying lines only. On that basis thirteen function docstrings are
still over the ceiling, the worst at thirty-one lines. They were read and kept rather than cut
to reach the number: where a ceiling and a causal fact collide, the fact wins. The ceiling is
the instrument, not the gate.

```diff
-"""The per-project-database half of the orphan sweep (U7, R10). Counts ONLY — never a
-database name, which embeds the owning project's uuid and would turn this report into an
-inventory of who has what (the exact posture `PrefixReconcileCounts` takes on keys).
+"""The per-project-database half of the orphan sweep. Counts only, never a database name.
```

### Inline comment — only where a reader would otherwise misread the line

A sequencing constraint, a fixture that must not be bound, a deliberate absence, or why an
odd-looking line is correct. Not a narration of what the next line does.

```diff
-# Minutes-scale (deliberately far under the ABC's 7-day ceiling): long enough for an
-# out-of-band review download, short enough that a leaked URL dies fast (R15).
+# Minutes-scale: long enough for an out-of-band review download, short enough that a leaked
+# URL dies fast.
```

**Where the comment carries a security invariant, keep the invariant and drop only the
citation.** The failure here reads as a perfectly good comment — softening "a dropped
`user_id` predicate is a cross-user leak" into a style preference. It is not a style
preference.

```diff
-endpoint can use it. Denies with a plain, non-leaking 403 otherwise. Fail-closed:
-anyone not on the allowlist is a citizen and is denied (AE1)."""
+endpoint can use it. Fail-closed: anyone not on the allowlist is a citizen and is
+denied with a plain, non-leaking 403."""
```

### Test names — present tense, no identifiers, no coverage markers

The name states the behaviour, so the docstring does not have to. A test docstring earns its
place only by saying *why this behaviour is worth pinning*, in one to three lines.

```diff
-def test_ae24_a_live_app_with_four_saves_and_no_new_submission_reads_live_newer_work() -> None:
+def test_a_live_app_with_four_saves_and_no_new_submission_reads_live_newer_work() -> None:
```

If a test's body has a note saying which source change turns it red, **keep it.** That note is
the only record of what the test actually bites on, and it is worth more than the docstring
above it.

## Some prose is load-bearing

Comment-only changes in this repository can break the build. Three kinds of reader consume
prose at runtime:

- **Tests that read source text off disk.** Several assert that a phrase is *absent* from a
  file — you break those by *adding* a word, not by removing one — and others assert a phrase
  is present. `backend/tests/api/v1/claude_retired/test_retired_names_are_past_tense.py` and
  `portal/src/__tests__/retired-names-are-past-tense.test.ts` are the two to know about; they
  require any mention of a deleted thing to read as history. Deleting the mention satisfies
  them; rewriting it into the present tense turns them red.
- **Docstrings compiled into shipped output.** Tool docstrings in
  `backend/src/services/agent/toolsets.py` are sent to the model as its tool descriptions.
  Route and Pydantic-model docstrings publish as OpenAPI descriptions. A script's module
  `__doc__` is its `--help` text. Editing any of these changes behaviour, not documentation.
- **A test deselected by name.** `backend/Dockerfile.gates` deselects a test by node id. An
  unresolvable node id is *silently ignored* by pytest — so renaming that test does not error,
  it re-enables the test inside the gate image, where it then fails for an unrelated-looking
  reason.

None of this is caught by lint or by the type checkers. **Run the suites before landing a
comment-only change.**

## How this repository is documented

`documentation/` is the published edition, and each document holds one kind of fact:

| Document | Holds |
|---|---|
| `documentation/architecture.md` | Why the system has the shape it has. Explanation only. |
| `documentation/deployment.md` | What the platform needs from its host, and how to prove a deployment worked. |
| `documentation/adr/` | Decisions in force, each with the reasoning that produced it. |
| `documentation/runbooks/` | Procedures — operating, recovering, and building the test database. |
| `documentation/reference/` | Generated and machine-readable material. |

**The architecture document carries no countable detail.** No resource names, no configuration
values, no version numbers, no route tables. Renaming a resource or adding a setting should require
no edit to it. Where a perishable fact matters, the prose names the file that owns it instead of
restating the value — that file is then the single place it can go stale.

**Diagrams are Mermaid fences.** No images, no external diagram formats. A diagram that cannot be
read as text in a review cannot be reviewed.

**The API reference is generated, never edited.** `documentation/reference/openapi.json` is produced
by `backend/scripts/openapi.py`, and the static gate sequence compares it against the code. A hand
edit is reverted by the next regeneration.

**A documentation change ships in the same commit as the code change that invalidated it.** Not in
a follow-up. The published edition is only true between commits if it is never left behind by one.

**Records keep their number for the life of the repository.** A decision that is no longer in force
keeps its file and its number and gains a first line naming what replaced it — no status table, no
date, no directory changelog. Gaps in the numbering are just gaps; they are not placeholders, and
nothing is backfilled. There is no index under `documentation/adr/`: a directory listing is the
index, and an index that has to be maintained is one more thing that goes quietly stale.
