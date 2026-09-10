"""The citizen-facing connector wire shapes.

`ConnectorEntry` IS THE `ConnectorStates` BOARD'S "THE PERSON" HALF, one object per registry
entry: the connector's name and subtitle, which of the four person states the caller is in, and
the facts that state needs rendered beside it. Fields outside the caller's own state are `null` —
`approvedByName` on a declined row would be a second answer to "who decided", and the board draws
one.

WHY THE APPROVED AND DECLINED TIMESTAMPS ARE TWO FIELDS OVER ONE COLUMN. `decided_at` carries
both, but the board's two sentences are `Approved for you 2 Sep · Rahul Menon` and
`Declined 2 Sep · Rahul Menon`, and a client that had to read `decidedAt` and then consult
`state` to learn which sentence it belongs to is one `if` away from writing "Approved" over a
decline. The state selects the field; the field names the sentence.

EVERY NAME FIELD IS `str | None`, AND NOT BECAUSE THE NAME MIGHT BE BLANK. The server already
falls back to the decider's email when `users.display_name` is null (see
`services/connectors/access.PersonAccess`), so a present decider always has a non-empty handle.
`None` means there is no decider to name at all: nobody has decided, or the administrator who did
has since been deleted (`decided_by_id` is `ON DELETE SET NULL`, so the decision outlives them).

THE SECOND HALF OF THIS MODULE IS THE PROJECT'S, not the person's — the switch, the days, and the
one body that writes both. `ConnectorStates` draws the two as separate state machines on purpose;
see the section comment below them for why they nevertheless share this file.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from src.db.models.project_connector import ConnectorWindowKind
from src.schemas import CamelModel, clean_stated_reason
from src.services.connectors import ConnectorPersonState


def _clean_request_remarks(value: str) -> str:
    """The connector access request's binding of the shared 5-50 word stated-reason rule.

    THE SAME RULE THE DELETE REASON USES, by owner decision D1 — not a note type of its own.
    Both surfaces count words with `portal/src/utils/words.ts`, so a browser counter can never
    let through something this API refuses, and `Give a little more detail — at least 5 words.`
    is something a person can act on where a character floor is not.

    The empty-field sentence is the `AskAccess` board's own helper text, which is what makes it
    read as an instruction rather than as a complaint about a form."""
    return clean_stated_reason(value, say_why="Say what you need the data for.")


class AccessRequestBody(CamelModel):
    """The body `POST /v1/connectors/{connector_key}/request` requires.

    THE REMARK IS THE WHOLE OF WHAT THE ADMINISTRATOR DECIDES ON — the decide dialog leads with
    it — which is why it is required here and NOT NULL on the row. WHO is asking is stamped from
    the authenticated session and never carried in the body; an extra key is ignored, as Pydantic
    ignores any unknown key."""

    remarks: str

    _v_remarks = field_validator("remarks")(_clean_request_remarks)


class ConsentLine(CamelModel):
    """One ticked line of the `WHAT AN APPROVAL GIVES YOU` panel: the bold lead, then the body.

    TWO FIELDS RATHER THAN ONE JOINED STRING, mirroring `core.connectors.ConsentLine` exactly.
    The boards set the lead in `font-weight:700` and the body in the panel's ordinary grey, so a
    pre-joined sentence would force the browser to guess the split at the first full stop — and
    the approver's `Read access to the Flight Fact Report.` breaks that guess outright, its body
    starting lowercase and mid-sentence on purpose. Sending the pair costs one nesting level and
    removes the guess."""

    lead: str
    body: str


class ConnectorEntry(CamelModel):
    """One registry connector as the asking person sees it. See the module docblock.

    THE ASK PANEL'S COPY RIDES THIS OBJECT (R18). `askSubtitle` and `consentLinesRequester` are
    the two things `AskAccess` says about a connector that no client can derive from a name: what
    the system holds, and what an approval does and does not give you. They come off the registry
    entry, which is where the same sentences already live for the administrator's panel. Without
    them a browser would have to carry one connector's dataset facts in a component, and "add a
    second connector" would stop being a registry entry and become a component change.

    BOTH ARE REGISTRY FACTS, NOT PER-CALLER FACTS, which is why they are non-null in EVERY state
    while the fields below them go null outside their own. The dialog draws the ask panel from a
    row the person has not asked about yet — that is the only state it is reachable from — so a
    state-conditional copy field would arrive null exactly when it is needed."""

    #: The stored `connector_key`. Stable, lowercase, and never rendered — the display name is.
    key: str
    display_name: str
    subtitle: str
    #: The `AskAccess` board's own sentence under its title — a whole sentence, and NOT
    #: `subtitle` (the row's four-word label). Neither is derivable from the other.
    ask_subtitle: str
    #: `AskAccess`'s three ticked promises, in board order. Consent copy: R1 makes it binding in
    #: substance, and the approver's differently-voiced set never travels to the citizen.
    consent_lines_requester: list[ConsentLine]
    state: ConnectorPersonState
    #: `pending` only: when they asked. The board reads `Asked 5 Sep, 08:30 · waiting on an
    #: administrator`, so the time of day is part of the sentence and this is not a date.
    asked_at: datetime | None = None
    #: `approved` only.
    approved_at: datetime | None = None
    approved_by_name: str | None = None
    #: `approved` only: how many of the caller's own projects have this connector switched on,
    #: for `On in 2 projects ›`. `None` — not `0` — in every other state: "we did not count"
    #: and "none" are different answers, and only one of them belongs on a row with no access.
    on_project_count: int | None = None
    #: `declined` only. The administrator's words reach the citizen VERBATIM and are rendered as
    #: plain text on every surface, never through a markdown component: one user writes this and
    #: another reads it.
    decided_at: datetime | None = None
    decided_by_name: str | None = None
    decision_remarks: str | None = None


class ConnectorListResponse(CamelModel):
    """Every registry connector, in registry order.

    AN ENVELOPE RATHER THAN A BARE ARRAY, matching `MarketplaceListResponse`: a top-level JSON
    array cannot grow a field, and this list is the one the Integrations dialog renders whole."""

    connectors: list[ConnectorEntry]


# --- the project's switch and its days ------------------------------------------
#
# A SECOND FAMILY IN THIS MODULE, ON PURPOSE. `ConnectorEntry` above answers "where does this
# PERSON stand"; everything below answers "what does this PROJECT read". `ConnectorStates` draws
# them as two state machines side by side and says why (`Access is yours. The days are the
# project's.`), and the split is the whole reason one administrator's answer covers every project
# somebody owns. They share a module because they share a domain and a wire vocabulary, not
# because either is derivable from the other.


class StoredWindow(CamelModel):
    """What the row actually holds — the pair the citizen picked, before any clamp.

    Exactly one shape is populated, per `ck_project_connectors_window_shape`: `days` for a
    preset, `start` + `end` for a fixed range. The KIND is not repeated here; it is the
    enclosing `ConnectorWindow.kind`, which is the same column and which the resolver never
    changes.

    IT IS ON THE WIRE SO `clamped` CAN BE ACTED ON. `true` says the resolved pair differs from
    the stored one, and the only way to tell somebody *what* they picked — a June range read in
    September — is to send it. Nothing renders this as the project's current window; that is
    the resolved pair beside it."""

    days: int | None
    start: date | None
    end: date | None


class ConnectorWindow(CamelModel):
    """The days one project reads from one connector RIGHT NOW, resolved by
    `core.connectors.resolve_window`, plus the two bounds the resolution used.

    EVERY FIELD HERE IS AN ANSWER, NOT AN INPUT. `start`, `end` and `days` are post-clamp, so
    `Reading N days of flight data` and the `1 – 30 Sep` chip beside it are the same numbers
    from the same emitter; a preset is re-resolved on every read against the connector's
    freshness ceiling — NOT against today — so a project on `Last 7 days` still reads the seven
    most recent READABLE days a month later.

    `earliestDate` AND `latestDate` ARE RETURNED BECAUSE THE CALENDAR GREYS AGAINST THEM. A
    browser in Bangalore and a server in UTC are 5½ hours apart, so a grid that computed its own
    floor would draw a date as selectable that the next read refuses. Both ends travel, and the
    ceiling is the one that surprises: the connector holds nothing after `latestDate`, which is
    `today - freshnessLagDays`, because a day's flights are loaded the following morning.

    `clamped` IS NOT AN ERROR. It says the stored pair aged out of the connector's retention or
    pointed past today and the platform read the nearest honest window instead. It is false by
    definition for a preset — a day count has no stored pair to differ from."""

    kind: ConnectorWindowKind
    start: date
    end: date
    #: `(end - start) + 1` AFTER clamping — the number in `Reading N days of flight data`, and
    #: therefore the number of days the app can actually see.
    days: int
    clamped: bool
    #: The oldest and newest dates this connector will serve RIGHT NOW, inclusive, in
    #: `Asia/Kolkata`. `latestDate` is behind today by the connector's freshness lag — it is the
    #: newest day the lake has actually loaded, never the current date.
    earliest_date: date
    latest_date: date
    stored: StoredWindow


class ProjectConnectorEntry(CamelModel):
    """One registry connector as ONE PROJECT sees it — the rail's DATA row, whole.

    THE FOUR PROJECT STATES OF `ConnectorStates` ARE READ OFF `state` AND `enabled` TOGETHER:
    `approved` + on is state a (the chip and the sentence), `approved` + off is state b,
    `pending` is state d (`You asked for access on 5 Sep — waiting on an administrator`, which is
    what `askedAt` is here for), and `neverAsked` / `declined` are state c (`You do not have
    access to … yet`, with `Request →`). Two of the four are read-outs of where the PERSON
    stands, so a project never looks broken without saying why.

    `enabled` IS THE SWITCH POSITION AND `effectivelyOn` IS WHETHER IT READS. They are different
    facts and both ship: the switch renders `enabled`, and everything that means "this project
    can see flight data" — the DATA counter included — reads `effectivelyOn`. A client that
    computed `enabled && state == 'approved'` for itself would be a second home for the
    conjunction that `resolve_window` owns, and the two would eventually disagree.

    NO `subtitle`. The dialog's connector row draws one under the name; the rail's row draws the
    project state sentence in that place instead, so the field would ship with no reader."""

    #: The stored `connector_key`. Stable, lowercase, and never rendered — the display name is.
    key: str
    display_name: str
    #: What this connector's data is CALLED, lowercase, for the rail's two state sentences:
    #: `Reading N days of {dataNoun}` and `Switch it on when a chat needs {dataNoun}`. It rides
    #: the wire for the same reason `askSubtitle` does — the sentence is the board's, the noun
    #: inside it is the connector's, and a second connector must not cost a component edit (R18).
    data_noun: str
    state: ConnectorPersonState
    #: `pending` only: when this person asked. The rail's state-d sentence names the date.
    asked_at: datetime | None = None
    #: The project's own switch, as stored. `false` when the connector was never switched on
    #: here — no row is `off`, and the rail draws them identically.
    enabled: bool
    #: `enabled` AND the owner is approved, straight off the resolver. `false` with no row.
    effectively_on: bool
    #: `null` when this connector was never switched on for this project — there is no window to
    #: render, which is a different fact from a window that exists behind a lowered switch.
    window: ConnectorWindow | None = None


class ProjectConnectorListResponse(CamelModel):
    """Every registry connector for one project, in registry order — the rail's whole DATA
    section in one response.

    NO `onCount` AND NO `total`. The rail holds this array and counts it, which involves no clock
    and no second emitter; a server-side count of the same list is how `1 of 2 on` and the rows
    under it come to disagree with nothing on screen admitting it."""

    connectors: list[ProjectConnectorEntry]


class ConnectorProjectEntry(CamelModel):
    """One of the caller's projects, on the drill-down list behind an approved connector row.

    The board draws the project's name, a switch, and the window chip — `Last 7 days` for a
    preset, `1 – 30 Sep` for a fixed range — with an em dash where a project has no window
    because the connector was never switched on there."""

    project_id: uuid.UUID
    name: str
    enabled: bool
    #: `null` for a project this connector was never switched on in. See `ProjectConnectorEntry`.
    window: ConnectorWindow | None = None


class ConnectorProjectListResponse(CamelModel):
    """Every project the caller owns, for one connector, newest first — and whether that IS all
    of them.

    `truncated` EXISTS BECAUSE NOTHING BOUNDS A CITIZEN'S PROJECT COUNT. The projects listing
    pages at 25 and no per-user cap exists anywhere, so this read stops at a cap and SAYS so
    rather than silently returning a prefix — the panel renders it as a line pointing at the
    search. Same shape, and the same reasoning, as the admin registry listing."""

    projects: list[ConnectorProjectEntry]
    truncated: bool = False


class RelativeWindowChoice(CamelModel):
    """`Last N days` — one of the presets the connector offers, re-resolved on every read.

    `days` IS NOT BOUNDED HERE, and that is not an omission. The set a connector offers is
    derived from its own `max_window_days` at the route (a connector that keeps a week cannot
    offer `Last 30 days`), and this model has no connector — the key is a path parameter. One
    refusal in one place beats a Pydantic bound that is right for today's registry only."""

    kind: Literal[ConnectorWindowKind.RELATIVE]
    days: int


class AbsoluteWindowChoice(CamelModel):
    """A fixed pair of calendar days, as picked in the month grid.

    ORDER IS THIS BOUNDARY'S GUARANTEE AND THE RESOLVER TAKES IT AS GIVEN — validated once,
    never re-checked inward. NOTHING ELSE is checked: a range older than the connector keeps, or
    one pointing into next month, is stored exactly as picked and clamped on every READ, so a
    window ages out on its own instead of a stored value slowly becoming a lie."""

    kind: Literal[ConnectorWindowKind.ABSOLUTE]
    start: date
    end: date

    @model_validator(mode="after")
    def _first_date_is_not_after_the_last(self) -> Self:
        if self.start > self.end:
            raise ValueError("The first date must be on or before the last.")
        return self


#: A DISCRIMINATED union, not a bare one: the `kind` picks the model, so a body naming
#: `absolute` and carrying `days` is refused as the absolute window it claims to be (`start` and
#: `end` missing) rather than silently matching the other arm. An unknown `kind` is
#: `union_tag_invalid`, which names the field that is wrong.
WindowChoice = Annotated[RelativeWindowChoice | AbsoluteWindowChoice, Field(discriminator="kind")]


class ProjectConnectorUpdate(CamelModel):
    """The body `PUT /v1/projects/{project_id}/connectors/{connector_key}` takes.

    ONE WRITE SETS BOTH THE SWITCH AND THE DAYS. Two routes would double the surface, double the
    refusal, and give a client two ways to reach an inconsistent pair.

    `window` OMITTED MEANS "KEEP WHATEVER IS STORED", and on a project that has never had this
    connector switched on it means the widest range the connector offers. That is what makes a
    switch press a one-field call: the client never has to know the default, and switching off
    and back on returns the range the citizen chose rather than silently re-picking it. Sending
    a window REPLACES the stored one outright — there is no per-field merge, and a `relative`
    choice does not leave the old dates behind it."""

    enabled: bool
    window: WindowChoice | None = None
