# Contributing

Three trees, three toolchains:

| Tree | What it is | Toolchain |
|---|---|---|
| `backend/` | FastAPI control plane | Python 3.14, `uv` |
| `portal/` | React + Vite single-page app | Node >=20 (the image builds on 24), `npm` |
| `sandbox/` | The build supervisor and the template every generated app starts from | Python 3.14 (via `backend/`), Node 24 in the image |

`README.md` is a stub, so this is the document to read first.

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

## Running the gates, and what each one proves

**Backend — static.** All five must pass. They need no database, no Redis and no secrets.

```sh
cd backend
uv run ruff check .            # lint
uv run ruff format --check .   # formatting, including inside docstrings
uv run ty check                # type check
uv run mypy src tests          # type check, strict on src
uv run pyright src tests       # type check, third opinion
```

Three type checkers is not belt-and-braces for its own sake — they disagree, and the
disagreements are where the real bugs sit. Fix rather than suppress.

**Backend — tests.** These need a `citizen_one_test` database built to the test-database
runbook, including a `REVOKE CONNECT` on the control-plane database that several per-app
database tests assert against. Build it out of band before your first local run — CI builds
its own from scratch on every run (`.github/workflows/ci.yml`'s `backend-tests` job).

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
