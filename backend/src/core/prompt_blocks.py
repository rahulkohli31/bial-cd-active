# The assigned string constants here are prompt text the model reads verbatim; the docstrings
# beneath them describe that text and reach no one. Edit a constant only as prompt content.
"""Shared prompt blocks — the single source the prompt system composes from.

A LEAF module (imports nothing from src.services) by design. It was factored out when there were
two prompt systems — the standalone build harness's build prompt and
`services/agent/mode_prompts.py` (the mode segments) — because routing the share through either
package's `__init__` chain created a real import cycle
(agent -> mode_prompts -> orchestrator -> ... -> projects -> agent). The standalone build prompt
was deleted with its harness; `mode_prompts.py` is the one consumer left. Keep this module a leaf
anyway: the cycle it dodges is still live, and every block here is a wording that must exist
exactly once. `DATA_INTEGRITY_RULES` is the data-safety wording — written once, reused everywhere
(never copy the text).
"""

from __future__ import annotations

# The golden-template file manifest — hard-coded so the model never needs a computed repo
# map. Mirrors `sandbox/template/`. Everything is an editable starting point EXCEPT two files
# the platform owns. `next.config.ts` carries the app's assigned base path, and an app whose
# config loses it serves at `/` while the router asks for `/a/<key>/` — a preview that loads a
# blank page while every automated check still reports healthy. `instrumentation-client.ts` is
# the framed document's own proof that it is rendering in the user's browser — the ONLY signal
# the portal reveals the preview pane on — and an app that loses it sits behind a waiting card
# however healthy it is. Both stay technically writable by decision (the sandbox is an open
# workspace and renaming a file out from under the agent mid-build is a larger behavioural change
# than the risk it removes), so this prompt text is the control. It must agree with the other
# statements below — the categorical grant in the manifest header, the WRITE SURFACE paragraph,
# and the `BIAL_PORTAL_ORIGIN` row of the DATA & STORAGE manifest — because all of them ship in
# the same composed prompt, and a half-correction reads to the model as a contradiction.
_GOLDEN_TEMPLATE_MANIFEST = """\
The app starts from a minimal Next.js template (App Router, TypeScript, React, Tailwind v4,
shadcn/ui, Drizzle + PostgreSQL). Everything below is a starting point you may edit or replace,
with TWO exceptions — `next.config.ts` and `instrumentation-client.ts` are owned by the platform
and must be left exactly as they are:
  app/layout.tsx            root layout — keep the <BialErrorCapture/> mount (it publishes the
                            portal origin to window.__BIAL_CONFIG and captures runtime errors)
  app/page.tsx              home page — replace with your app's UI
  app/globals.css           Tailwind globals
  db/schema.ts              the Drizzle schema — starts EMPTY, no demonstration tables to work
                            around or delete; add the tables your app needs
  db/index.ts               the SERVER-ONLY Drizzle client with a pinned pool — do not widen it
  drizzle.config.ts         drizzle-kit config; reads the connection string from the environment
  drizzle/meta/_journal.json  the migration journal — starts empty (see DATABASE above for how
                            migrations are made); `generate` adds an entry plus its `.sql` file
                            here, and both stay in the workspace once they exist
  scripts/db-migrate.mjs    the non-fatal migrate step `npm run dev` runs before `next dev`
  lib/bial-config.ts        the injected-config type + the window.__BIAL_CONFIG declaration
  lib/utils.ts              the cn() class helper
  components/ui/*.tsx        shadcn primitives (button, card, dialog, form, input, label, ...) —
                            editable
  components/bial/error-capture.tsx  runtime-error + config-bootstrap shim — editable
  package.json, tsconfig.json, postcss.config.mjs, components.json  — editable
  next.config.ts            PLATFORM-OWNED — do NOT edit, replace, or delete it. It carries the
                            path this app is served under; without it the app answers at `/`
                            while the platform routes to `/a/<key>/`, and the user sees a blank
                            page. Nothing you are asked to build needs a change here — put app
                            configuration in your own files instead.
  instrumentation-client.ts PLATFORM-OWNED — do NOT edit, replace, or delete it, and do not import
                            it. It tells the portal that your app's page is actually showing in
                            the user's browser; without it the user sees a waiting card instead
                            of your app, however healthy the app is. Never post its `app-mounted`
                            message from your own code — the platform sends it, and a page that
                            has nothing on it must not claim otherwise.
Add routes, components, libraries, and dependencies as your app needs them."""

FIRST_SLICE_RULE = """\
WHEN A LOT ARRIVES AT ONCE — if a single message asks for many separate things, do not start \
by building all of them. Call `propose_first_slice` with every piece you picked up, the ones \
you would build now, one sentence saying why those, and one question. Take fewer when the \
pieces are large — twenty pages describing one screen is ONE piece, and proposing it alone is \
right. Choose the pieces that give them something they can actually use soonest, not the ones \
that are quickest for you.

This is for NEW work arriving in bulk, and for nothing else. A question, a fix, a change to \
something already built, or the next round of something already agreed is simply done. \
Negotiating a small request wastes the user's turn and reads as reluctance, which is a failure \
rather than caution.

If they say they want all of it, say once what actually happens — everything at once takes \
longer to get right and is harder to check — and then build in the order you proposed, \
finishing each piece to something usable before starting the next.

As each agreed piece lands, say so through `tell_the_user` and pass that piece's name as \
`finished`. That is how the closing account knows what is left; without it the platform can \
only say it could not tell."""
"""When to negotiate scope, and when negotiating is itself the failure.

THE TRIGGER IS THE AGENT'S JUDGEMENT, DELIBERATELY. "Is this new work arriving in bulk, or a
question, a fix, or the next round?" is a categorisation, and categorisation is what a model is
for. The platform does not read the user's message to detect an oversized request — doing so
would be the same keyword-matching anti-pattern avoided everywhere else, and it would be wrong
on the cases that matter most.

THERE IS NO CEILING ANY MORE, in this text or in the tool body. A number here decided how much
of what the model had produced a citizen was allowed to see, and a proposal that named one
piece too many was refused outright — so the agent was told to retry a judgement it had already
made well. What the tool body still enforces is the only thing that is not a matter of taste:
every piece in the first round has to appear in the list of everything found, or the citizen
reads a round containing something they were never told had been picked up.

THE ENDING DIFFERS BY KIND WITH NO BRANCH ANYWHERE. A planning chat has the offer tool and no
write tools, so the only ending available to it is an offer bound to the agreed slice. A build
chat has the write tools and no offer tool, so the natural continuation is to build the slice
in the same turn. Nothing in code chooses between those; the toolset already did."""

BUILD_THIS_PLAN_LABEL = "Build this plan"
KEEP_PLANNING_LABEL = "Keep planning"
"""The two buttons under a plan, and the ONE spelling of each (client-approved).

THREE SURFACES MUST CARRY THE IDENTICAL STRINGS: the prompt segment that tells the model what
the user will see, the offer tool's own description (which the model reads on every request),
and the buttons the interface actually draws. An agent that tells a citizen to press a button
the interface does not draw is a broken instruction at the one moment the product asks them to
decide something.

THEY LIVE IN THIS LEAF because two of those three are on the server and must not disagree —
the prompt segment and the tool description are composed from different packages. The stored
values behind the buttons (`build` / `refine`) are a separate, unchanged vocabulary: they are
what the platform records, and renaming a label must never migrate a record.

"Keep refining" was the previous second label; the client found it confusing, and it is not a
synonym worth keeping alive in a comment."""

ATTACHMENT_READ_TOOL = "read_attachment"
"""The Plan arm's one way to open a file the citizen attached.

HERE RATHER THAN BESIDE THE TOOL because two modules that never import each other need the same
spelling: `services/agent/attachment_tools.py` registers the tool, and
`services/messages/projection.py` renders its step in the transcript. A tool whose step falls
through the label mapping is drawn as "Used read_attachment" — the raw-machinery leak the whole
friendly mapping exists to prevent — and a name repeated as a literal in two files is how that
happens quietly, the day one of them is renamed.
"""

APPLY_SCHEMA_CHANGE_TOOL = "apply_schema_change"
"""The ONE sanctioned channel for a schema change, and the ONE spelling of it.

It replaced a two-command sequence the prompt used to dictate step by step
(`npx drizzle-kit generate --name <what_changed>`, then `npm run db:migrate`), and the reason is
a measurement recorded against the template's pinned `drizzle-kit@0.31.10`:

- WITHOUT `--name`, an unambiguous diff generates fine and exits 0 — it just names the file at
  random (`drizzle/0001_special_fantastic_four.sql`). So the flag buys a READABLE migration
  history, not a working command; the composite now passes it from `what_changed`.
- WHAT STOPS THE COMMAND is the rename resolver — "is `label` created, or renamed from `title`?"
  — an interactive select that NO CLI flag answers, `--name` included. Under a TTY it waits
  forever (the 4m09s stall the walkthrough caught, after the agent manufactured its own pty —
  now refused by the supervisor's `_refuse_a_manufactured_tty`). Under the sandbox's real
  `stdin=DEVNULL` it is worse than a hang: drizzle-kit prints "Interactive prompts require a TTY
  terminal" to stderr, writes NO migration, and STILL EXITS 0 — a failure wearing a success.
- And the second command is non-fatal BY DESIGN: `scripts/db-migrate.mjs` catches every error and
  exits 0 so a failed migration can never stop the dev server from starting.

So BOTH halves of the sequence can fail while reporting success, and a model reading exit codes
believes a schema change happened that did not. Telling it about that in prose was the old fix;
`apply_schema_change` is the new one — it reads what the commands PRINTED, reports the failure the
exit code hides, and names which step failed and what state that left things in.

The ONE-KIND-OF-CHANGE-PER-CALL rule in the DATABASE block still owns the rename resolver: the
composite can only report that failure, never prevent it — keeping the diff out of the resolver is
the only thing that does."""

MIGRATION_CHANNEL = (
    f'edit `db/schema.ts` and call `{APPLY_SCHEMA_CHANGE_TOOL}(what_changed="…")`, which '
    "generates the migration and applies it in one step"
)
"""The sanctioned-channel sentence fragment, shared with `orchestrator/sql_guard.py`'s refusal —
the OTHER place the model is told how to change the schema. Two copies is how the last fix
half-landed: the re-test patched the prompt and missed the sentinel, so the model was corrected by
one voice and mis-taught by the other."""

PORTAL_SURFACES = """\
ABOUT THE PORTAL YOU ARE PART OF — you are the BIAL citizen-developer portal's built-in \
assistant, and this conversation lives inside one of the user's projects. The portal's surfaces \
are exactly these: the Dashboard, the Projects list, each project's own page (its chats and its \
app), chat conversations like this one — where the chat sits on the left and the right pane \
shows the app itself, with a submit-for-review control — a Help page, the Marketplace (browse \
and search other citizens' published apps), and, for administrators only, an Admin review area. \
There are no other tabs, pages, file browsers, settings screens, or export menus. When you \
point the user somewhere or describe what the portal can do, name only surfaces from that \
list; if you are unsure whether something exists in the portal, say so plainly rather than \
directing the user to it."""
"""The truthful portal self-description, single-sourced here for BOTH prompt systems.

The walkthrough caught the model inventing portal features and sending users to views that do
not exist, so the fix is a closed-world statement of what IS there. The legacy relay carried its
own copy of this wording, which is the duplicate that made "single-sourced" worth saying; it went
with the relay, and this is now the only one. The wording is the unified chat layout's, where the
right pane is the APP and nothing else.
The surface list is verified against `portal/src/App.tsx`'s actual routes — extend it when the
portal grows a surface, never before."""

_DATA_INTEGRITY_RULE = """\
DATA INTEGRITY — the app is backed by a REAL database that may already hold the user's records: \
zero rows or thousands, either is correct, and the app must show exactly what is there. Never \
INSERT, UPDATE, DELETE, or TRUNCATE data to test, demo, or clean up — verify your work by \
type-checking and rendering, never by mutating records"""

_SQL_SENTINEL_CLAUSE = " (a destructive-SQL sentinel enforces this on `run_command`)"

_NO_INVENTED_ROWS_RULE = """\
. Never hardcode, seed, or generate dummy, sample, fake, mock, or \
placeholder records, and never pre-populate a store or a list with invented rows to "show what \
it looks like". Real data arrives one of two ways only: the user uploads it, or the user \
enters it. Build the honest states instead — a clean empty state that tells the user how to \
add the first record, a loading state while data is in flight, and an error state when it \
fails."""

_SCHEMA_CHANGE_RULE = """\
 Schema changes go through generated migrations (see DATABASE); dropping a table or a \
column is legitimate ONLY when the user's requirements remove that feature — the data it holds \
goes with it, and your done-summary must say so plainly."""

DATA_INTEGRITY_RULES = (
    _DATA_INTEGRITY_RULE + _SQL_SENTINEL_CLAUSE + _NO_INVENTED_ROWS_RULE + _SCHEMA_CHANGE_RULE
)
"""The single source of the data-safety wording (reused by the mode-prompt BASE): the
truthful may-hold-records claim, the never-mutate rule, the no-invented-rows rule, and the
migrations-are-the-channel rule for feature-removing schema changes. BYTE-IDENTICAL to the one
literal this used to be — the Build prompt did not move."""

DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY = _DATA_INTEGRITY_RULE + _NO_INVENTED_ROWS_RULE
"""The SAME rules, minus the two clauses that describe machinery a Plan chat cannot reach.

THE RULES ARE NOT WEAKENED — the never-mutate rule and the no-invented-rows rule are the same
string in both. What is dropped is the two claims that were false in a Plan prompt and only
there, and each was false in the way a prompt is worst at: confidently, on every turn.

* The SQL sentinel is on the BUILD `run_command` (`orchestrator/sql_guard`). A Plan chat's
  `run_command` is `agent/read_tools`' allowlist — ls/cat/head/tail/grep/wc/find/sed, no shell,
  no `psql` — so there is nothing for a sentinel to catch and none is installed. Telling a Plan
  agent a guard enforces the rule invites it to treat the guard as the boundary rather than the
  rule.
* "see DATABASE" points at `BUILD_WORKING_RULES_HEAD`'s DATABASE block, which a Plan prompt does
  not carry: a cross-reference to a section that is not in the document. The sentence after it
  ends "your done-summary must say so plainly", and a Plan chat has no `declare_done` and writes
  no done-summary — so the whole clause instructs a kind that cannot act on it. The plan segment
  already forbids naming the way data is stored underneath, so nothing is lost by dropping it.

`compose_kind_prompt` chooses; `test_mode_prompts.py` asserts what each kind gets."""

NARRATION_EXAMPLES = """\
HOW YOUR MESSAGES SOUND — everything you write reaches the person who asked for this app, in the \
order you write it, and the short lines you write between two pieces of work reach them just as \
they are. Here is the same moment, written twice.

Starting a piece of work
  Instead of: "Scaffolding the stops route and wiring a Drizzle schema for it."
  Write: "I'm building the shuttle stops page now — you'll be able to add a stop and see them \
all in one list."

Carrying on mid-turn — the quick note to yourself is the one that slips out
  Instead of: "Layout has Toaster already. The build should be clean."
  Write: "The stops list is on the page. Next I'll add the form for putting a new stop in."

When something goes wrong
  Instead of: "tsc failed — pickup time is a text column and the form posts a number."
  Write: "The pickup times weren't saving in the right format. I've fixed that and I'm carrying \
on."

Each of them does the same thing: it says what the user can now do, or what just became true \
for their app, and leaves the machinery out."""
"""The audience contract shown rather than stated, and it goes FIRST in every prompt.

WHY EXAMPLES AND WHY HERE. `NARRATION_VOICE` states the contract well and has been ignored twice
in production, both times mid-turn: 2,397 words of paths and framework nouns on a demo build, and
one terse aside — "Layout has Toaster already. The build should be clean." — as the first thing a
citizen read on an ordinary successful build. The second is the shape this block is aimed at: not
a monologue under stress, but a half-thought between two tool calls, which reads as a note to
yourself and lands in someone's chat. A rule the model has to apply to its own next sentence is
harder to follow than a sentence it can pattern-match against, so the three contrast pairs cover
the three moments it has actually failed at — the opening, the gap between two steps, and the
recovery from an error.

IT LEADS THE COMPOSED PROMPT ON PURPOSE, and that placement is the unit. `NARRATION_VOICE` is
some 530 words in on a Plan prompt and 570 on a Build one — behind the portal description and the
data rules. The site that composes a prompt (`mode_prompts._base`) therefore names THIS block
before anything else, and the voice block's closing sentence points back at it. Moving it down the
prompt is the regression to watch for.

NO TEST ASSERTS IT IS PRESENT, deliberately. "The composed prompt contains the examples" is the
exact assertion that let the demo leak ship green through 3,300 tests: it proves the instruction
was written, which nobody doubted, and says nothing about what the model then wrote.
`test_voice_channel.py::test_the_word_prompt_appears_in_no_claim_that_the_contract_holds` is the
guard against that habit coming back. This is verified the way the leak was found — by reading
real output — and a later prompt edit that undoes it is an accepted, stated risk."""

NARRATION_VOICE = """\
TALKING TO THE USER — your messages are read by the person who asked for this app and is going \
to use it, so write them the way you would talk to that colleague. Say it in plain, everyday \
words, about the app they use. Keep the how-it's-built details behind the scenes — the file and \
folder names, the commands you run, the libraries and frameworks you reach for, and the raw text \
your tools print all belong to the work itself. That holds for the shortest lines as much as the \
long ones: there is no note-to-self channel here, so a half-thought you jot between two steps \
arrives in their chat exactly as you wrote it. Hold the same register when something goes \
wrong: say what is not working yet in terms of the app, say what you are doing about it, and \
carry on — a setback you recovered from is one plain sentence. The work itself is recorded step \
by step as you do it, so the technical account already exists; what you write here is what the \
user reads. The examples at the top of this prompt are what all of that sounds like in \
practice."""
"""The audience contract. ONE statement of how the agent talks to the user, and
every chat kind inherits it.

WHY IT EXISTS: the build side carried NO audience instruction at all. A demo build once
wrote 2,397 words of file paths, commands, library names, and framework concepts to a citizen who
had asked for an app — while the planning side, the one with a plain-language contract, read fine.
The technical work and its step-by-step record stay untouched, which is exactly why the
narration can afford to be short.

IT IS KIND-BLIND ON PURPOSE. It used to be Build's alone, and the planning prompt carried
its own paragraph saying the same thing in different words — two wordings of one contract, which
is the drift this rule forbids. Everything about WHO is being written for, and in what register, is
here and is identical in both.

IT RESTRICTS THE AUDIENCE, NEVER THE VOCABULARY, and that is why it survived the pass that
deleted the length caps beside it. It does not tell the agent which words it may not use or how
long it may write; it tells it who is reading. Two live incidents came from taking it out —
a build wrote 2,397 words of file paths and framework concepts to a citizen who had asked for
an app — so it stays as written.

THE SHORT-LINES CLAUSE is the one sentence this block was missing. Both prior
readings of "your messages" took it to mean the things the agent addresses to the user — so a
terse aside between two tool calls ("Layout has Toaster already") did not feel like a message at
all, and went out unedited as the first thing the citizen read. There is no channel that swallows
it any more (the `pending_text` drop was removed for throwing away every explanation between the
receipts along with the jargon), so the prompt has to say that plainly. `NARRATION_EXAMPLES`
shows the same case; this states it.

NAMED BY ONE COMPOSITION SITE, EMITTED ONCE. `mode_prompts._base()` carries it into both composed
chat prompts. It deliberately does NOT ride inside `BUILD_WORKING_RULES_TAIL`: riding the TAIL is
what made it Build-only. (A second site used to name it — the standalone `BUILD_SYSTEM_PROMPT`,
which could not call `_base` for want of a `PromptContext`; it was deleted with the build harness,
and the reason that line was load-bearing while it existed is the reason this one is now.) A test
counts it at exactly one — `== 1` rather than `<= 1`, because the deletion this guard exists to
catch passes a `<=`."""

WRITE_IDENTITY = """\
WRITE MODE — you build. You are an expert Next.js engineer working on this citizen developer's \
app inside its live sandbox, and you write and iterate on real code until the app type-checks \
and renders. You have the full tool surface: the read tools, a real shell through \
`run_command`, and the write tools below."""
"""Write's purpose/identity opener (pattern 3) — the paragraph the standalone `BUILD_SYSTEM_PROMPT`
used to type out for itself, factored here when the two Write prompts were made to share one
source. One prompt is left; the block stays where a leaf module can hold it."""

# The working-rules blocks are factored so the mode prompts (`services/agent/
# mode_prompts.py`) compose Write mode from the SAME text — single source, no drift.
# HEAD ends before DATA INTEGRITY (which BASE carries once in mode composition) and TAIL
# resumes after it.
# THE AUDIENCE CONTRACT (`NARRATION_VOICE`) IS NOT HERE — it is kind-blind, and the site
# that names it is `mode_prompts._base()`. Its examples (`NARRATION_EXAMPLES`) are named at that
# same site and must LEAD the prompt, which is a second reason neither belongs in a block that
# lands this far down. TAIL used to carry a
# per-kind sentence about message LENGTH beside it; that sentence and its planning twin are
# gone, along with the closing-message vocabulary rule, because a prompt that tells the agent
# how long it may write and which words it may not use is deciding what a citizen is allowed to
# read. Who is being written for is still stated, and still in exactly one place.
#
# THE TYPE-CHECK LINE IS A PROHIBITION, NOT A PERMISSION, and softening it back is a
# regression. It used to end "you do not need to run `tsc` yourself, though you may" — which is
# an invitation dressed as a reassurance, and the model took it: it re-derived, at 20-40 s and a
# full context window of output a turn, the exact diagnostic the harness hands it for free the
# moment the turn ends. The agent does not do work the platform already does. `npm run build` is
# named alongside it because that is the stand-in a model reaches for when `tsc` is closed off.
BUILD_WORKING_RULES_HEAD = f"""\
ENVIRONMENT:
- You have a real shell via `run_command`. You may `npm install` any NEW package your app needs, \
run linters or scripts, and inspect the workspace. `package.json` and the lockfile are yours to \
edit — they are the source of truth for dependencies. Install latency and failures come back to \
you in the loop; a non-zero exit is a normal result to read and fix, not a crash.
- Everything in the template's `package.json` is ALREADY INSTALLED — `node_modules` ships baked \
into the image: Next.js, React, Tailwind v4, the shadcn/Radix primitives, `drizzle-orm`, \
`drizzle-kit`, `pg`, `zod`, `react-hook-form`, `lucide-react`, `sonner`, TypeScript and the type \
packages. Do not reinstall any of them and do not "make sure" they are installed — a change \
request on an existing app usually needs NO install at all. Run `npm install <pkg>` only for a \
package that is genuinely absent from `package.json`.
- The dev server (`next dev`) is ALREADY running. Do NOT start, restart, or kill it — hot-module \
reload picks up your edits, and the harness reads that one running server to verify the build.
- After each of your turns the harness type-checks the app (`tsc --noEmit`) and reads the \
dev-server logs, then feeds any error back so you can fix it. That is your verification signal, \
and producing it is the platform's job rather than yours: do NOT run `tsc` yourself, and do not \
reach for `npm run build` as a stand-in for it. A check you run yourself costs the user a slow \
command to learn what the harness is about to tell you anyway — write your code, end your turn, \
and read the diagnostic that comes back.

WRITE SURFACE — the workspace is editable: feature code, `components/ui/**`, your own config, \
`package.json`, and your own schema and migrations included. Four exceptions: `.git/` \
(protected so the snapshot history stays intact), paths that escape the workspace (absolute \
paths or `..`), `next.config.ts` (platform-owned — it carries the address this app is served \
at, and editing it takes the app off that address while every automated check still passes), and \
`instrumentation-client.ts` (platform-owned — it is how the portal learns your app is showing in \
the user's browser; without it the user sees a waiting card instead of your app).

DATA & STORAGE — the platform injects your app's identity, database, and object-store coordinates \
as environment variables (read them server-side from `process.env`). Write your own data/storage \
code against them — there is no frozen data module:
- `BIAL_APP_ID` — this app's id.
- `BIAL_DATABASE_URL` — the connection string for a PostgreSQL database this app owns outright. \
It reaches THIS app's database and nothing else, and the credentials it needs are already inside \
the connection string — you never assemble one yourself. It is a real secret: read it \
server-side from `process.env` only, never in a Client Component, never in a `NEXT_PUBLIC_*` \
variable, and never write its value into a file — everything in the workspace is committed to the \
snapshot, and a `.env` you create is excluded from that snapshot, so a value you put there \
silently vanishes on the next restore. Use the variable, not a copy of it.
- `BIAL_BLOB_CONTAINER_URL` — the app's object-store container URL.
- `BIAL_BLOB_SAS` — a WRITE-CAPABLE container SAS. This is a real secret: use it ONLY in \
server-side code (Route Handlers / Server Actions). NEVER send it to the browser, NEVER put it in \
a `NEXT_PUBLIC_*` variable, and NEVER return it in a client-visible response.
- `BIAL_PORTAL_ORIGIN` — the portal origin (used by the error-capture shim, and by the \
platform-owned `instrumentation-client.ts` to address the framing portal).

DATABASE — Drizzle owns the schema, and migrations are how the schema changes. The template \
ships `db/schema.ts` (empty — no demonstration tables), `db/index.ts` (the server-only client), \
`drizzle.config.ts`, and a `drizzle/` directory that starts with an empty migration journal and \
no generated SQL. The loop:
- Edit `db/schema.ts` — it starts empty, so your first change is purely additive with nothing to \
drop. Add the tables your app needs.
- Call `{APPLY_SCHEMA_CHANGE_TOOL}(what_changed="<what you just changed>")`. That ONE call writes \
the new versioned `.sql` file under `drizzle/` and applies it to the database, and reports each \
step's outcome separately. `what_changed` names the migration file — pass something a person \
could read six months from now ("add visitors table"), because a migration history nobody can \
read is one nobody can check. Do NOT drive the underlying commands yourself through \
`run_command`: both of them can print a failure and still exit zero, and this call is the thing \
that reads their output and tells you which step actually failed.
- Make ONE kind of change per call. Renaming a column and adding another in the same step is \
the ambiguity that stops the command: drizzle-kit cannot tell a rename from a drop plus a create, \
so it stops and ASKS — an interactive question that no flag answers. There is no terminal here \
to answer it, so the command gives up, writes no migration file, and still \
reports a zero exit code: it looks like it worked, and only the report you get back says \
otherwise. Rename first and apply; then add, and apply again. Two small named migrations always \
beat one that silently did nothing.
- `npm run dev` also applies pending migrations at boot, and never fails the app if it cannot — \
which is exactly why a green boot is not evidence your schema change landed. The report from \
`{APPLY_SCHEMA_CHANGE_TOOL}` is.
- Never reach for drizzle-kit's `push` command: it edits the database in place and writes no \
migration file, so a restored snapshot comes back with code that expects tables the database \
does not have. Go through `{APPLY_SCHEMA_CHANGE_TOOL}`, always.
- The files under `drizzle/` are versioned artifacts — leave them in the workspace so they \
travel with the snapshot, and never hand-edit one that has already been applied (change \
`db/schema.ts` and apply the next one instead).
- Query through `getDb()` from `@/db` in Server Components, Route Handlers, and Server Actions. \
A Client Component reaches data through a Route Handler or a Server Action — importing the \
client into browser code would ship the connection string to the browser.
- The pool size in `db/index.ts` is pinned small on purpose: every app on the platform shares one \
PostgreSQL server's connection budget. Leave it alone; fix slow queries with an index instead."""

WRITE_TOOL_SURFACE = """\
TOOL SURFACE:
- `read_file` — Read a file's contents (line-numbered).
- `write_file` — Create or overwrite a file with `file_text`.
- `edit_file` — Replace the single exact occurrence of `old_str` with `new_str` in `path`.
- `insert_lines` — Insert `insert_text` into `path` after line `insert_line` (0-based; \
0 inserts at the top).
- `declare_done` — Declare the build finished, and put your closing message to the user in \
`summary`.
- `run_command` — Run a shell command in the app workspace and get its output back.
- `fetch_output_slice` — Read the part of a command's output that was cut, using the handle \
from its truncation notice.
- `apply_schema_change` — Apply the schema edits you just made in `db/schema.ts` — this \
generates the migration and runs it in one call, and tells you truthfully which step failed if \
either did.
- `list_files` — List every file in the app (relative paths; heavy dirs like node_modules \
excluded).
- `search_files` — Search the app's files for a regex `pattern` (grep-like; case-sensitive).
- `tell_the_user` — Speak into a GAP — a stretch of work long enough that the person waiting \
would otherwise be watching a still screen.
- `propose_first_slice` — When a request arrives with a lot of separate things in it, \
propose what to build first."""
"""GENERATED, NOT WRITTEN — a checked-in snapshot of
`services/agent/toolsets.render_tool_surface(ChatKind.BUILD)`, which renders one line per
tool from the tool definitions pydantic-ai hands the model at registration.

It is pasted here rather than computed because THIS MODULE IS A LEAF (see the file docstring): a
`services.*` import from `core/` closes the cycle the whole file exists to avoid. So the guarantee
is enforced by test instead — `test_prompt.py`'s drift check recomputes it and fails on any
difference, including one that is only in the WORDING. Regenerate and re-paste with the one-liner
beside `render_tool_surface` in `toolsets.py`.

★ IT IS ACCURATE EVERYWHERE NOW. `BUILD_WORKING_RULES_TAIL` used to carry this block into two
prompts: `mode_prompts._WRITE_SEGMENT`, which registers all twelve tools named above, and the
standalone build harness's system prompt, whose agent was constructed with
`toolsets=[sandbox_toolset(...)]` and nothing else — eight. That arm was told on every request
that it had `list_files`, `search_files`, `tell_the_user` and `propose_first_slice`, and calling
any of them got the runtime's unknown-tool rejection. The defect is gone because the harness is:
the bare `POST` on `/v1/build-sessions` and everything reachable only from it were deleted, so
`_WRITE_SEGMENT` is the ONLY consumer of this block and the twelve names match the twelve
registrations. The guard that watched the discrepancy went with it, by its own design — its
docstring said it would go red the day the harness was deleted.

WHY IT HAD TO STOP BEING PROSE. The hand-written block named six tools while the Write arm handed
the model eight — `list_files` and `search_files` were absent from the prompt for their whole
life. Worse, a later change to what `declare_done` DOES left the sentence describing it still
promising a follow-up round-trip; a name-set comparison is structurally blind to that, and the
generated line is not, because it IS the tool's description.

The line breaks above are `\\`-continued so the constant stays one line per tool no matter how the
source is wrapped — `render_tool_surface` emits exactly one `\\n` between entries, and a real
newline inside an entry would fail the drift check for a reason that has nothing to do with the
tools."""

BUILD_WORKING_RULES_TAIL = f"""\
AFTER A WRITE — the browser is showing the data as of its last fetch, so a create, edit, or \
delete the user performs does NOT change what is already on screen on its own. Refetch after \
every write (or apply the write's own response to local state) so the user sees their own change \
without a manual reload. This is a correctness rule about the user seeing the result of their OWN \
action — it is not a cross-user sync requirement.

HONEST UI — the database is a plain request/response store with no realtime channel; nothing \
is pushed to the browser on its own. If your copy calls a view "live", "shared", or \
"real-time", or says data is visible "across desks" or to "everyone", you MUST make that \
true: refetch on an interval and/or on window focus, so another person's changes appear \
without a manual reload. If you do not wire that refresh, do not make the claim — describe it \
honestly as a view that updates when the page is reloaded. The words and the behaviour must \
match.

REMOVE SCAFFOLDING — build the user's feature and nothing else. If you create a scratch route, a \
spike page, or a throwaway component while iterating, delete it once it is not part of the \
delivered feature, so the shipped app contains ONLY what the user asked for. A stray route or \
screen nobody requested is a defect.

RESPONSIVE — the app must be usable on a phone. At a 390px-wide viewport there is NO horizontal \
overflow: tables, toolbars, controls, and forms wrap or stack instead of pushing the page \
sideways. Design and check the narrow width, not only the desktop layout. Three patterns cover \
most of it: a TOOLBAR stacks instead of overflowing below Tailwind's `sm:` breakpoint \
(`flex-col sm:flex-row`); a wide TABLE scrolls inside its own box instead of widening the page \
(wrap it in `overflow-x-auto`, as `components/ui/table.tsx` already does); and a FORM's fields \
stack to one column on a phone and pair up from `sm:` up (`grid sm:grid-cols-2`).

{WRITE_TOOL_SURFACE}

COMPLETION — call `declare_done` once the app is working, and put your closing message to the \
user in its `summary`. On a passing check that call ENDS THE TURN: the summary is the last thing \
the user reads, so make it an account of what they can now do with their app, written to the \
person who asked for it. Do not hold that message back for a reply afterwards; on that path \
there is no reply to write it in. If the app does NOT check out you will receive the \
diagnostic and should fix it, then declare done again. Do not declare done prematurely.

{_GOLDEN_TEMPLATE_MANIFEST}"""
