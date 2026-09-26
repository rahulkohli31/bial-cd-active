"""The connector wire shapes: what one project reads, and the one body that sets it.

Every shape here is about a PROJECT — its switch and the days it reads. The switch alone decides
whether a project reads a connector, so nothing here says anything about the person who owns it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from src.db.models.project_connector import ConnectorWindowKind
from src.schemas import CamelModel


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
    """One registry connector as ONE PROJECT sees it — the settings row, whole.

    `enabled` IS THE SWITCH POSITION AND `effectivelyOn` IS WHETHER IT READS. The switch renders
    `enabled`, and everything that means "this project can see the data" reads `effectivelyOn`,
    straight off `resolve_window` — a client never works that answer out for itself.

    NO `subtitle`. The row draws the project's state sentence under the name instead, so the field
    would ship with no reader."""

    #: The stored `connector_key`. Stable, lowercase, and never rendered — the display name is.
    key: str
    display_name: str
    #: What this connector's data is CALLED, lowercase, for the row's two state sentences:
    #: `Reading N days of {dataNoun}` and `Switch it on when a chat needs {dataNoun}`. The
    #: sentence is the board's, the noun inside it is the connector's, and a second connector must
    #: not cost a component edit.
    data_noun: str
    #: The project's own switch, as stored. `false` when the connector was never switched on
    #: here — no row is `off`, and the rail draws them identically.
    enabled: bool
    #: Whether this project reads the connector, straight off the resolver. `false` with no row.
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
