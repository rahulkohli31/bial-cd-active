"""The Plan chat's one way to read an attached file.

Registered on the Plan arm alone, by `toolsets_for_kind` — the one place permitted to read a
chat kind. Build does not get it and the asymmetry is deliberate: Build holds an unrestricted
`run_command` and can read, EDIT and re-run the reader as it would any other file, so a
fixed-shape tool beside that would be a second, weaker way to do what it already does better.

WHY THIS EXISTS

Plan already executes inside the container, but only through `check_the_guest_list`: eight
read-only binaries, exec-style argv, no shell, no runtime, no package manager. `python3` is
deliberately absent, so Plan cannot invoke the shipped reader the way Build does.

The obvious fix — add `python3` to the guest list, or allow a path outside the app root — is
the one that must not be taken. That surface is SHARED with the reviewer agent, which runs on
the control plane over untrusted project contents, and `check_the_guest_list(argv)` takes argv
and nothing else precisely so no body below it can ask which agent is calling. Widening it for
attachments widens it there too, and the signature is the proof it cannot be done selectively.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

import structlog
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

from src.core.prompt_blocks import ATTACHMENT_READ_TOOL
from src.core.redaction import scrub_untrusted
from src.services.agent.read_tools import (
    ATTACHMENTS_PREFIX,
    is_an_attachment_path,
    refuse_unsafe_path,
    to_container_path,
)
from src.services.orchestrator.deps import SandboxSession

logger = structlog.get_logger()

# Where the canonical reader is baked. Fixed and known, never discovered, because the point is
# that an agent is TOLD where this is, because an agent that has to find a reader writes one
# instead — the single failure this design exists to prevent.
READER_PATH = "/usr/local/lib/bial/read_attachment.py"

# Long enough for the reader's own 30-second ceiling to fire first, so a slow file comes back as
# the reader's named `timeout` failure rather than as a transport error with no advice in it.
_READ_TIMEOUT_SECONDS = 45

_STDERR_LOG_CHARS: Final = 500
"""How much container-authored text a failure log keeps. Enough for a traceback's last frame and
the exception line, which is where the cause is; short enough that a hostile file cannot make the
log the place its payload lives."""


# ── WHAT A FAILED READ LEAVES BEHIND ───────────────────────────────────────────────────────────
#
# THREE FAILURES USED TO BE ONE SILENCE. A damaged spreadsheet, a container that ran out of memory
# reading it, and an image predating the reader all reached the model as one `ModelRetry` sentence
# and reached the operator as nothing at all — so "attachments are broken" could not be narrowed
# without reproducing it. Each class now leaves exactly one event naming itself.
#
# WHAT THEY BIND, AND WHAT THEY MUST NOT. `app_id` and the file's SUFFIX. Never the display name
# (citizen-supplied text), never the path, and never `handle` — it carries the live supervisor
# bearer, which is the rule `SandboxSession` states for itself. Explicit fields rather than
# `exc_info=True`: the configured chain (`core/log_config.py`) reduces an exception to its class
# chain and raise site, which names the failure but carries none of these facts.


def _suffix_of(path: str) -> str:
    """The file's extension, which is all of a citizen's file name a log may carry."""
    return PurePosixPath(path).suffix


#: The largest reader answer worth returning. The manifest is bounded inside the image; this
#: is the backstop for a container that predates that bound, generous enough that no bounded
#: manifest can reach it.
_MAX_REPLY_CHARS: Final = 250_000


def _was_killed(exit_code: int) -> bool:
    """Whether a process was stopped by a signal rather than returning a status.

    Two spellings reach here: a negative code, which is how Python reports a signal, and
    128 + signal, which is how a shell reports the same thing. Either way nothing in the
    child got to say why, so the caller is the only place left that can.
    """
    return exit_code < 0 or exit_code >= 128


@dataclass
class AttachmentReader:
    """Runs the shipped reader over one attached file, and nothing else.

    THE SCOPE IS THE ARGV, which is why this is a class holding a session rather than a general
    exec helper: the command is assembled here from a fixed interpreter, a fixed script path and
    one operand that must name the attachments root. There is no shape of input that turns this
    into a way to run something else.
    """

    session: SandboxSession

    async def read(self, path: str) -> str:
        # ★ TRANSLATED, NOT PASSED THROUGH. The model is given `.attachments/<name>` — a token the
        # READ SURFACE understands and rewrites — but this tool does not go through that surface:
        # it hands an argv straight to `exec`, which runs in the app root. Passed verbatim, the
        # reader resolves `.attachments/roster.xlsx` against `/workspace/app` and reports the file
        # missing while it sits in `/workspace/attachments` the whole time. That is exactly what
        # the first end-to-end run produced, and no unit test caught it: every test here drives
        # `read` with a path and asserts on the argv, so the argv was self-consistently wrong.
        #
        # The same `to_container_path` `read_file` and `search_files` use, for the same reason and
        # with the same one-prefix scope — `read_attachment` refuses anything that is not an
        # attachment path, so this only ever rewrites the prefix it was built for.
        # ★ `-I` ISOLATES THE INTERPRETER, and that is a security property rather than a tidy-up.
        # A bare `python3` runs `site`, which imports `usercustomize` and executes every `.pth`
        # file under `$HOME/.local` — and the container's `$HOME` belongs to the account the app's
        # own build runs as. Any Build turn, any `npm` lifecycle script and the generated app
        # itself could therefore rewrite what the reader does, AFTER the turn note has told the
        # model this is the trusted shipped copy. `-I` also ignores `PYTHONPATH` and the current
        # directory, so the app tree cannot shadow a stdlib module the reader imports.
        argv = ["python3", "-I", READER_PATH, to_container_path(path)]
        run_command = self.session.sandbox_client.exec  # aliased off the JS-oriented exec guard
        try:
            result = await run_command(self.session.handle, argv, timeout_s=_READ_TIMEOUT_SECONDS)
        except Exception as exc:
            # THE TRANSPORT CLASS: the container never answered. Logged and re-raised unchanged —
            # the tool body above turns it into the retry sentence it always did, and this only
            # stops that sentence from being the sole record of what happened.
            logger.warning(
                "attachment_read_transport_failed",
                app_id=str(self.session.app_id),
                suffix=_suffix_of(path),
                error=type(exc).__name__,
                detail=scrub_untrusted(str(exc), limit=_STDERR_LOG_CHARS),
            )
            raise
        if result.exit != 0:
            # THE STALE-IMAGE CLASS, and the only one that can produce a non-zero exit: the
            # reader's own contract is to exit 0 and print a named failure for every bad file, so
            # a non-zero exit means the script itself is missing or unrunnable — an image that
            # predates it. The return is UNCHANGED (the caller's JSON parse fails and the model
            # gets its retry sentence); what is new is that an operator can tell this apart from
            # a file that would not read.
            logger.warning(
                "attachment_read_nonzero_exit",
                app_id=str(self.session.app_id),
                suffix=_suffix_of(path),
                exit_code=result.exit,
                stderr=scrub_untrusted(result.stderr, limit=_STDERR_LOG_CHARS),
            )
            if _was_killed(result.exit) and not result.stdout.strip():
                # ★ THE ONE FAILURE THE READER CANNOT NAME FOR ITSELF. Its contract is one
                # JSON object and exit 0 — but polars allocates through Rust, whose
                # allocator ABORTS on failure rather than raising, so a file that exhausts
                # the memory ceiling kills the process with a signal and prints nothing.
                # Python never runs again in that process, so the name has to be given
                # here. Left unnamed it reaches the tool as unparseable output and asks the
                # model to try again — against a failure that is perfectly deterministic.
                return json.dumps(
                    {
                        "ok": False,
                        "file": path.rsplit("/", 1)[-1],
                        "error": {
                            "code": "too_large",
                            "message": (
                                "This file needed more memory than the reader is allowed."
                            ),
                            "next": "Attach a smaller file, or split it into parts.",
                        },
                    }
                )
        return result.stdout


def attachment_toolset[DepsT](
    reader_of: Callable[[RunContext[DepsT]], AttachmentReader],
) -> FunctionToolset[DepsT]:
    """The Plan arm's attachment capability, over whatever deps the caller resolves a reader from.

    A factory for the same reason `read_only_toolset` is one: WHICH container is a fact about the
    run, not about the ability. What this toolset allows is written down once, here.

    The inner tool annotates `RunContext[Any]`, matching `read_only_toolset`'s own note and for
    the same reason: pydantic-ai resolves tool annotations with `get_type_hints` at registration,
    and a PEP-695 type param of the ENCLOSING function is not in scope there under deferred
    annotations. The factory signature carries the real typing.
    """
    toolset: FunctionToolset[DepsT] = FunctionToolset[DepsT](id="attachments")

    # THE NAME IS THE CONSTANT, not the spelling of this function, because the transcript's
    # label mapping matches on it from a module that cannot import this one.
    @toolset.tool(name=ATTACHMENT_READ_TOOL)
    async def read_attachment(ctx: RunContext[Any], file: str) -> str:
        """Read a file the user attached to this chat and report what is in it.

        Pass `file` as the path you were given for the attachment — it starts with
        `.attachments/`. Returns a description of the file's real contents: for a spreadsheet the
        sheets, their true row counts and what each column holds; for a document its headings,
        paragraphs and tables; for a deck its slides and speaker notes.

        The whole file is read, not a sample, and the answer says so — if anything could not be
        summarised it is named. Use this rather than guessing from the file's name, and never
        write your own parser: this is the tested one.
        """
        # ★ THE PREFIX IS NOT A CONTAINMENT CHECK, and treating it as one left the attachments
        # root escapable. `is_an_attachment_path` answers "does this name the reserved prefix" and
        # nothing more, so `.attachments/../../etc/roster.csv` satisfies it — and this tool hands
        # its argv to `exec`, which does NOT pass through the supervisor's `_resolve`. The reader
        # would open any `.csv`/`.xlsx`/`.docx`/`.pptx`/`.tsv` in the container.
        #
        # It matters because the path can be MODEL-CHOSEN and attachment content is untrusted by
        # this feature's own rule — a spreadsheet cell that talks an agent into a traversal
        # is exactly the shape that rule anticipates. `_vet_path_token` is the same lexical guard
        # `read_file` and `search_files` apply, and it is applied here for the same reason and
        # BEFORE the translation, exactly as `to_container_path` documents.
        refusal = refuse_unsafe_path(file)
        if refusal is not None:
            raise ModelRetry(refusal)
        if not is_an_attachment_path(file):
            # A TEACHING REFUSAL, not an error. The model gets one sentence naming the shape it
            # should have used and retries — the same contract every refusal on the read surface
            # follows.
            raise ModelRetry(
                f"`{file}` is not an attached file. Attachments are named with the "
                f"`{ATTACHMENTS_PREFIX}` prefix you were given in this turn — pass that path. "
                "This tool reads attachments only; use `read_file` for the app's own source."
            )
        reader = reader_of(ctx)
        try:
            raw = await reader.read(file)
        except Exception as exc:  # a transport failure, not a bad file
            raise ModelRetry(
                f"The attachment could not be read right now ({type(exc).__name__}). "
                "Try once more; if it fails again, say so rather than guessing at the contents."
            ) from None

        if len(raw) > _MAX_REPLY_CHARS:
            # THE LAST BOUND BEFORE THE MODEL'S WINDOW. The reader bounds its own manifest
            # now, so this fires only for a container running an image that predates that
            # bound — and one oversized answer costs the turn it was meant to serve, which is
            # worse than saying plainly that the file could not be described.
            logger.warning(
                "attachment_read_reply_too_large",
                app_id=str(reader.session.app_id),
                suffix=_suffix_of(file),
                chars=len(raw),
            )
            return json.dumps(
                {
                    "ok": False,
                    "file": file.rsplit("/", 1)[-1],
                    "error": {
                        "code": "too_large",
                        "message": "The description of this file was too large to return.",
                        "next": "Attach a smaller file, or split it into parts.",
                    },
                }
            )

        # THE READER ALWAYS PRINTS ONE JSON OBJECT AND EXITS 0, including for a corrupt or
        # encrypted file — that is its contract. Anything else means the script itself is wrong or
        # missing, which the model must not paper over by inventing a description of the file.
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            raise ModelRetry(
                "The reader did not return a result for that file. Tell the user the file "
                "could not be read rather than describing what it might contain."
            ) from None
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            # A NAMED FAILURE IS AN ANSWER, so it is returned rather than raised: the model should
            # tell the citizen the file is damaged and what to do, not retry a file that will fail
            # identically.
            #
            # ★ AND IT IS THE COMMONEST FAILURE THERE IS, which is why it is logged here rather
            # than left to the two sites in `read`. Those fire only when the container itself
            # misbehaves; the reader answers `ok: false` — with its own `code` — for a corrupt,
            # encrypted, oversized or timed-out file, exits 0, and would otherwise be an answer
            # only the model ever sees. `code` is the reader's own vocabulary, so the log names
            # the same class the citizen was told about.
            error = parsed.get("error")
            logger.warning(
                "attachment_read_named_failure",
                app_id=str(reader.session.app_id),
                suffix=_suffix_of(file),
                code=error.get("code") if isinstance(error, dict) else None,
            )
            return raw
        return raw

    return toolset
