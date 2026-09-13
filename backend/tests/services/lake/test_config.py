"""The lake's coordinates — three values, none of them optional, parsed once at construction.

WHY THIS FILE IS WRITTEN FIRST. A settings group whose required fields carry defaults is the
failure this repository has a rule about (`.claude/rules/fail-first-python.md`): it boots, it
looks configured, and it fails at the first read against an account nobody chose. The test that
proves the opposite is three lines, and it is worth more than the three lines suggest because the
group is `| None` on `ApiSettings` — so "unconfigured" is a supported posture and a HALF
-configured group is the one state that must not exist.

THE URL CARRIES THREE THINGS AND IS SPLIT ONCE, HERE. Account, container and folder are not three
fields: one URL carries all three, the generated app is handed exactly that URL, and both sides
split it the same way. Parsing at construction rather than at first use is what makes a
mis-typed value a startup failure instead of a listing that silently returns nothing — which is
this pass's central risk, because an empty listing reads exactly like a permissions fault.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.services.lake.config import LakeConfig

_URL = "https://anaccount.blob.core.windows.net/acontainer/AOS/tb_flight_fact_report/"
_CLIENT_ID = "52b74947-0621-46e2-a523-a6b466f47c33"
_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/a-group"
    "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/an-identity"
)


def _config(**overrides: str) -> LakeConfig:
    return LakeConfig(
        **{
            "url": _URL,
            "identity_client_id": _CLIENT_ID,
            "identity_resource_id": _RESOURCE_ID,
            **overrides,
        }
    )


# --- the three fields are required ------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["url", "identity_client_id", "identity_resource_id"], ids=lambda name: name
)
def test_every_field_is_required_and_fails_at_construction(missing: str) -> None:
    """No defaults, on any of the three. A group missing one of them must not construct.

    THE TWO IDENTIFIERS ARE BOTH LOAD-BEARING AND THEY ARE NOT INTERCHANGEABLE — this is the
    parametrised case that says so. The CLIENT id is what `ManagedIdentityCredential` needs
    inside the container; the ARM RESOURCE id is the key ARM's `user_assigned_identities`
    dictionary is keyed by, and the client-id field on that model is read-only, so it cannot
    attach anything. Only client ids were ever recorded when this feature was scoped, which is
    exactly how a group with a default for the resource id would have shipped."""
    values = {
        "url": _URL,
        "identity_client_id": _CLIENT_ID,
        "identity_resource_id": _RESOURCE_ID,
    }
    del values[missing]

    with pytest.raises(ValidationError) as raised:
        LakeConfig(**values)

    assert missing in str(raised.value)


def test_an_unknown_nested_key_fails_rather_than_being_ignored() -> None:
    """`extra="forbid"`, like every other settings group here. A mistyped `CONNECTOR_LAKE__*`
    name that were merely ignored would leave the real field on its default — and the fields
    above have none, so the process would refuse to start with a message naming the wrong
    field."""
    with pytest.raises(ValidationError):
        _config(prefix="AOS/")


# --- the URL is split once, at construction ---------------------------------------------------


def test_the_url_yields_the_endpoint_the_container_and_the_prefix() -> None:
    config = _config()

    assert config.account_url == "https://anaccount.blob.core.windows.net"
    assert config.container == "acontainer"
    assert config.prefix == "AOS/tb_flight_fact_report/"


def test_a_prefix_without_a_trailing_slash_gains_one() -> None:
    """A prefix is a `name_starts_with` filter, not a path. `AOS/tb_flight_fact_report` also
    matches `AOS/tb_flight_fact_report_archive/…`, so the folder the operator meant is
    normalised to end in a separator rather than trusted to have been typed with one."""
    config = _config(url="https://anaccount.blob.core.windows.net/acontainer/AOS/reports")

    assert config.prefix == "AOS/reports/"


def test_a_url_naming_only_a_container_has_an_empty_prefix() -> None:
    """The whole container is a legitimate coordinate — an empty prefix lists all of it, and
    must NOT be normalised to a bare `/`, which matches nothing."""
    config = _config(url="https://anaccount.blob.core.windows.net/acontainer")

    assert config.container == "acontainer"
    assert config.prefix == ""


@pytest.mark.parametrize(
    "bad",
    [
        "https://anaccount.blob.core.windows.net",  # no container at all
        "https://anaccount.blob.core.windows.net/",  # a trailing slash is not a container
        "anaccount.blob.core.windows.net/acontainer",  # no scheme, so no host either
        "ftp://anaccount.blob.core.windows.net/acontainer",  # not an http(s) endpoint
        "https:///acontainer",  # no host
    ],
    ids=["no-container", "empty-container", "no-scheme", "wrong-scheme", "no-host"],
)
def test_a_url_that_cannot_name_a_container_fails_at_construction(bad: str) -> None:
    """Each of these would otherwise become an empty listing at the first read — and an empty
    listing is indistinguishable from a permissions failure, which is the failure mode this
    whole pass is written around."""
    with pytest.raises(ValidationError):
        _config(url=bad)


def test_the_failure_message_never_carries_the_url() -> None:
    """pydantic echoes validator messages into `ValidationError` and thus into logs. The URL is
    not a bearer credential — it is a label, and the client's account name is allowed to appear
    in a raised error at the CLIENT boundary — but a validator message is a different surface:
    it is emitted at startup, before anything has decided what may be logged, and every other
    settings group in this tree keeps its messages static for the same reason."""
    with pytest.raises(ValidationError) as raised:
        _config(url="https://anaccount.blob.core.windows.net")

    assert "anaccount" not in str(raised.value)
