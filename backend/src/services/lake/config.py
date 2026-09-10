"""The lake's coordinates: where one connector's object store is, and which identity reads it.

`Settings.connector_lake` is typed `LakeConfig | None`; pydantic-settings validates one
`CONNECTOR_LAKE__*` env block against it — the same single-config-funnel shape
`object_store`, `redis` and `sandbox` already take, and `| None` for the same reason: a
developer machine has no lake, and that is a supported posture rather than a broken one.

NO LITERAL FROM THIS FILE'S SUBJECT LIVES IN CODE. The account name and the identity's two
identifiers are the client's, not ours, so they arrive as configuration and never as constants in
`src/core/connectors.py` — which is also what keeps R11's checkable form (a word-boundary grep
that must hit exactly one module) true.

NOTHING HERE IS A BEARER CREDENTIAL, AND THAT IS DELIBERATE. A URL and two identity identifiers
are LABELS: holding them grants nothing. The credential is the managed identity itself, which is
minted by Azure inside the container and cannot be copied out of this file. So no field is a
`SecretStr`, both of the values injected into a container ride the spec as plain environment
variables, and the client is free to name the coordinates it attempted in an error — which is the
one diagnostic that tells a wrong-identity failure apart from a missing role assignment.
"""

from __future__ import annotations

from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

# Every message below is STATIC — no field value is interpolated. pydantic echoes validator
# messages into `ValidationError`, which is raised during settings construction at startup, before
# any of this process's own logging rules are in force.
_URL_SHAPE = (
    "CONNECTOR_LAKE__URL must be an https:// URL naming the account, the container and the "
    "folder in one string, e.g. https://<account>.blob.core.windows.net/<container>/<folder>/"
)


class LakeConfig(BaseModel):
    """One connector's object store, and the user-assigned managed identity that may read it.

    THREE VALUES, AND THE LAST TWO NAME THE SAME IDENTITY IN TWO DIFFERENT VOCABULARIES. This is
    the distinction that blocked the plan and is easy to lose again:

    * `identity_client_id` is what `ManagedIdentityCredential(client_id=...)` needs — INSIDE the
      container, at the moment a token is requested. It is one of the two values injected into a
      sandbox and a published app.
    * `identity_resource_id` is the full ARM path
      (`/subscriptions/…/userAssignedIdentities/<name>`). ARM's
      `ManagedServiceIdentity.user_assigned_identities` is a dictionary KEYED BY IT, and the
      client-id field on that model is read-only — so the resource id is the only thing that can
      ATTACH the identity to a container app at all. It never enters a container.

    Only client ids were recorded when this feature was scoped. A group that defaulted the
    resource id would therefore have shipped, attached nothing, and failed as though a role
    assignment were missing.
    """

    # `extra="forbid"` makes a mistyped CONNECTOR_LAKE__* nested key fail at startup instead of
    # being silently ignored and falling back to a default — and these fields HAVE no defaults,
    # so the ignored key would surface as a missing required field naming the wrong thing.
    model_config = ConfigDict(extra="forbid")

    # The whole coordinate in one string: `https://{account}.blob.core.windows.net/{container}/
    # {folder}/`. NOT three fields. The generated app is handed exactly this value and splits it
    # in two lines; a control plane that stored the parts separately would be a second spelling
    # of the same fact, and the two would eventually disagree about a trailing slash.
    url: str
    identity_client_id: str
    identity_resource_id: str

    @model_validator(mode="after")
    def _the_url_must_name_a_container(self) -> Self:
        """Split at CONSTRUCTION, not at first use.

        A URL that names no container produces an empty listing, and an empty listing is
        indistinguishable from a permissions failure — the exact silent shape this pass is
        written around. Failing here makes it a startup error with a message naming the field."""
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(_URL_SHAPE)
        segments = [segment for segment in parts.path.split("/") if segment]
        if not segments:
            raise ValueError(_URL_SHAPE)
        return self

    @property
    def account_url(self) -> str:
        """The blob service endpoint — `https://{account}.blob.core.windows.net`, no path.

        What `BlobServiceClient` takes. Derived rather than stored so it cannot drift from
        `container` and `prefix`, which are derived from the same string."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def container(self) -> str:
        """The ONE container this identity may read. The first path segment of `url`."""
        return [segment for segment in urlsplit(self.url).path.split("/") if segment][0]

    @property
    def prefix(self) -> str:
        """The folder inside that container, normalised to end in `/` when it is not empty.

        A prefix is a `name_starts_with` FILTER, not a path: `AOS/report` also matches
        `AOS/report_archive/…`. Normalising here means the operator's trailing slash is not
        load-bearing. An empty prefix stays empty — the whole container is a legitimate
        coordinate, and a bare `/` would match nothing at all."""
        segments = [segment for segment in urlsplit(self.url).path.split("/") if segment]
        folder = "/".join(segments[1:])
        return f"{folder}/" if folder else ""
