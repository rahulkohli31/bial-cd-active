# ADR-0034: BIAL Chat Reads Office Files by Running Code in a Managed Session per Chat

## Context

Staff ask BIAL Chat about the spreadsheets, documents and decks most of their work lives in. The
model accepts PDFs, pictures and text only; the code-execution and file features that would let it
open anything else are not offered on the deployment the platform uses. Reading a workbook
correctly takes code: the shipped reader first, then whatever calculation the question needs.

That code is written by the model, and the model reads the document while writing it. Whoever wrote
the document can therefore steer the code, so the code is untrusted in the same way a generated
application is (ADR-0014). It must not reach another person's files, the platform's credentials, or
the internet.

BIAL Chat has no project and no workspace. The containers that serve Plan and Build chats belong to
a person's one app-building workspace, so borrowing one for a question about a spreadsheet would
take that workspace from whatever it was doing.

## Decision

**Each BIAL Chat that holds an Office or delimited file gets its own Azure Container Apps dynamic
session**, from a code-interpreter session pool. Microsoft runs the session from its own Python
image, isolates it with Hyper-V, and the pool has internet access switched off. The session is named
by the chat alone, and only after the send route has confirmed the chat belongs to the person
asking.

**The session is reused across the chat's replies and is only ever a working copy.** The stored
attachment stays the truth. On a reply's first file access the backend lists the session's files
against its own in-process record of what it copied in: anything missing or changed refills the
session, a deleted attachment is removed, and the reader is uploaded again. Azure deletes a session
after an idle cool-down without warning, and the next call quietly opens an empty one; the listing
is how a reply notices, and the cost is a re-copy, never a file.

**The model gets two tools, and only in a chat that needs them.** One runs the shipped reader over
a file; the other runs Python against the chat's files. Both run one at a time. They are registered
when the chat holds such a file or has already used them, because the model API refuses a history
carrying tool calls when no tool is defined. A reply that never opens a file makes no call to the
session at all.

**Every reply that works on files is bounded**: a ceiling on model requests, a wall clock, a time
limit on each run of code, and one run at a time. Either ceiling ends the reply in one plain
sentence and keeps the steps taken so far.

**Running code is stopped by deleting the session, and only after its call has ended.** The service
offers no other way to stop it. A Stop, the wall clock, or a run past its time limit deletes the
chat's session, and the next reply refills it. The call that started the code is cancelled first:
deleting a session under a call still open makes the service run the same code again in a fresh
one. Deleting the chat deletes its session at once; the cool-down is the backstop.

**Only the backend's own identity can call the pool**, holding the session-executor role and
nothing else. The pool's API-key access is switched off, which also puts its built-in tool server
behind the same role.

**The runtime is optional.** With no pool configured, BIAL Chat refuses Office and CSV files at
upload and at send and answers everything else as before. A chat that already holds such files is
still answered; a tool call then returns a fixed "unavailable" result.

## Consequences

- One chat can never reach another chat's files, the same person's included. Isolation is a
  hardware boundary, not a folder.
- Code steered by a document can alter that same chat's working copies. A careless change is caught
  by the size and modified-time comparison and refilled; a deliberate one that forges both can last
  until the session ends. It never reaches another chat, and the stored original is untouched.
- The pool's data plane is a public Microsoft endpoint with no private path. The backend calls out
  to it; nothing becomes reachable from outside.
- A session is small: one processor and a few gigabytes of memory. The densest text workbook the
  upload limit admits can exhaust the reader's own time limit, which ends as a named failure rather
  than a wrong answer. Running out of memory is likewise a named result.
- Variables computed in one reply survive into the next only while the session lives. The model is
  told to recompute rather than assume.
- Analysis capacity costs nothing while nobody uses it, and the platform runs no worker and no pool
  of its own for it. Usage is billed as for every other reply: from the tokens the model reports,
  never from a file's size.
- Nothing changes for app workspaces: ADR-0014 stands, and so does the warm pool of ADR-0032.

## Rejected alternatives

- **One shared container for every chat.** The only wall between people's files would be a Linux
  user on a kernel the platform cannot patch, its internet access is open, one crash ends everyone's
  analysis, and it needs the platform to build process limits, sweeps and retirement rules of its
  own. It is no cheaper at this volume.
- **One container per chat from the existing sandbox machinery.** It isolates as well, but its
  internet access is open, a cold start takes tens of seconds, and it carries the platform's own
  lifecycle code. It is the fallback if the managed sessions cannot be used.
- **A session per reply.** It removes the within-chat tampering above, but copies every file on
  every reply. The per-chat session was chosen for speed.
- **A session pool running the platform's own image.** It bills dedicated capacity whether or not
  anyone asks a question.
- **Extracting a document's text on the server.** A summary written by a parser the model cannot
  see is wrong in ways nobody notices, which is why the shipped reader exists.

## Related

- ADR-0004 (the per-person scoping the send route applies before any session is named)
- ADR-0014 (the platform's own sandboxes, unchanged by this)
- ADR-0025 (the shared daily token limit analysis replies count against)
- ADR-0032 (the warm pool of ready sandboxes, unchanged by this)
