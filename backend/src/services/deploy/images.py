"""Build a container image with ACR Tasks — the registry's own build agent, over ARM REST.

WHY NOT THE SDK. `azure-mgmt-containerregistry` 15.0.0 (the current GA release) does NOT
contain the Tasks surface at all: no `schedule_run`, no `get_build_source_upload_url`, no
`DockerBuildRequest`, no runs operations. Those live only under the `2019-06-01-preview` API
version, which the older multi-api clients exposed and the regenerated GA client dropped.
Reaching them would mean pinning an unmaintained SDK several major versions back, purely
for four JSON calls.

So this module speaks ARM REST directly. That is not a workaround, it is the better shape
here: it is async-native over the `httpx` already in the dependency set (no sync SDK to
offload to a worker thread), the API version is pinned explicitly and visibly, and the whole
thing is testable against `httpx.MockTransport` exactly like `services/sandbox/client.py`.

THE CONTROL PLANE NEVER PUSHES. ACR's build agent does the push, which is why this needs no
`AcrPush` grant — only `read`, `listBuildSourceUploadUrl/action`, `scheduleRun/action`,
`runs/read` and `runs/listLogSasUrl/action`, scoped to the single registry resource. Keep it
that way: widening to a push credential here would move a real secret into the control
plane for no capability it does not already have.

The flow is four calls:
  1. listBuildSourceUploadUrl  -> a blob URL + the relative path ACR will read from
  2. PUT the tarball to that URL (plain blob upload, no ARM auth)
  3. scheduleRun               -> a Run, polled to a terminal status
  4. listLogSasUrl on failure  -> the build log, so the citizen sees WHY
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import structlog
from azure.identity import DefaultAzureCredential

from src.services.deploy.config import DeployConfig
from src.services.deploy.names import image_tag, published_app_name
from src.services.sandbox.base import base_path_for

_log = structlog.get_logger()

# The ONLY API version that carries the Tasks surface. Pinned explicitly and loudly: the
# GA versions do not have these routes at all, so a "helpful" bump to a newer version turns
# every build into a 404.
_TASKS_API_VERSION: Final = "2019-06-01-preview"

_ARM_BASE: Final = "https://management.azure.com"
_ARM_SCOPE: Final = "https://management.azure.com/.default"

# Terminal run statuses. `Queued`/`Started`/`Running` are the non-terminal ones.
_SUCCEEDED: Final = "Succeeded"
_TERMINAL_STATUSES: Final = frozenset({"Succeeded", "Failed", "Canceled", "Error", "Timeout"})

# Blob upload needs this header or the PUT is rejected — the destination is a block blob.
_BLOB_TYPE_HEADER: Final = {"x-ms-blob-type": "BlockBlob"}

# Bound every leg. The upload is generous (a source tree over a corporate link); scheduling
# is a small control-plane call; the poll interval and total build budget come from config.
_UPLOAD_TIMEOUT_S: Final = 120.0
_ARM_CALL_TIMEOUT_S: Final = 30.0
# Only a bounded prefix of a build log is kept: it is attacker-influenced text from a
# workspace the citizen's AI drove, and the caller redacts and re-caps it anyway.
_LOG_TAIL_CHARS: Final = 32_000


class ImageBuildError(Exception):
    """The build did not produce an image. `log_tail` carries the registry's own output when
    it could be fetched — that text is what reaches the citizen's chat, so the caller must
    redact it before it egresses anywhere."""

    def __init__(self, message: str, *, log_tail: str | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail


class ImageBuildTransientError(ImageBuildError):
    """A retryable failure reaching ARM — not a failure of the build itself."""


@dataclass(frozen=True)
class BuiltImage:
    """What a successful build produced. `digest` is what the container spec pins; the tag
    exists only so an operator can trace an image in the registry back to a deployment row."""

    digest: str
    tag: str
    run_id: str


class ImageBuilder(Protocol):
    """The seam the pipeline depends on — structural, so a test substitutes a recorder with
    these two methods and never constructs a credential."""

    async def build(
        self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes
    ) -> BuiltImage: ...

    async def aclose(self) -> None: ...


class AcrImageBuilder:
    """ACR Tasks over ARM REST."""

    def __init__(
        self,
        config: DeployConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        credential: Any | None = None,
    ) -> None:
        self._config = config
        self._credential = credential if credential is not None else DefaultAzureCredential()
        self._http = httpx.AsyncClient(transport=transport, timeout=None)

    @property
    def _registry_id(self) -> str:
        c = self._config
        return (
            f"/subscriptions/{c.acr_subscription_id}"
            f"/resourceGroups/{c.acr_resource_group}"
            f"/providers/Microsoft.ContainerRegistry/registries/{c.acr_name}"
        )

    async def _token(self) -> str:
        # `get_token` is sync and does its own caching, so this is a cheap call that only
        # occasionally goes to the network — but it IS blocking, so it stays off the loop.
        token = await asyncio.to_thread(self._credential.get_token, _ARM_SCOPE)
        return str(token.token)

    async def _arm(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{_ARM_BASE}{path}?api-version={_TASKS_API_VERSION}"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._http.request(
                method, url, json=json, headers=headers, timeout=_ARM_CALL_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            raise ImageBuildTransientError(f"ARM {method} {path} failed") from exc

        if response.status_code == 403:
            # The single most likely first-run failure, and one no retry can fix. Say what
            # is missing rather than surfacing an opaque 403 hours into a rollout.
            raise ImageBuildError(
                "the control plane is not authorized to build images in this registry — "
                "grant it listBuildSourceUploadUrl/scheduleRun/runs on the registry resource"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise ImageBuildTransientError(f"ARM {method} {path} returned {response.status_code}")
        if response.status_code >= 400:
            raise ImageBuildError(f"ARM {method} {path} returned {response.status_code}")
        if not response.content:
            return {}
        payload: dict[str, Any] = response.json()
        return payload

    async def build(
        self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes
    ) -> BuiltImage:
        # Lazy, like the singleton below: a module-level `src.config` import is a cycle. The
        # apps hostname is CORE settings rather than a `DeployConfig` field on purpose — it is
        # the one address every generated app is served from, preview and published alike, so
        # it is not a property of the publishing block and does not belong behind its
        # `extra="forbid"`.
        from src.config import settings

        apps_hostname = settings.apps_hostname

        tag = image_tag(
            repository_prefix=self._config.image_repository_prefix,
            app_id=app_id,
            deployment_id=deployment_id,
        )

        upload = await self._arm("POST", f"{self._registry_id}/listBuildSourceUploadUrl")
        upload_url = upload.get("uploadUrl")
        relative_path = upload.get("relativePath")
        if not upload_url or not relative_path:
            raise ImageBuildError("the registry returned no source upload location")

        await self._upload(str(upload_url), context)

        run = await self._arm(
            "POST",
            f"{self._registry_id}/scheduleRun",
            json={
                "type": "DockerBuildRequest",
                "sourceLocation": relative_path,
                # Named EXPLICITLY: a Dockerfile smuggled at any other path in the citizen's
                # workspace is never the one that gets built.
                "dockerFilePath": "Dockerfile",
                "imageNames": [tag],
                "isPushEnabled": True,
                "noCache": False,
                "timeout": self._config.build_timeout_s,
                "platform": {"os": "Linux", "architecture": "amd64"},
                "arguments": [
                    # The base image is a config knob so ops can pin a digest without a code
                    # change. NEVER pass a secret here — ARM logs request bodies.
                    {
                        "name": "NODE_IMAGE",
                        "value": self._config.node_base_image,
                        "isSecret": False,
                    },
                    # WHERE THE APP LIVES, and it has to be known HERE because `next build`
                    # bakes it: every link, asset URL and route in the output is written under
                    # this prefix, so a container environment variable would arrive after the
                    # decision was already made. Derived from `app_id` rather than taken as a
                    # parameter — the published container's name IS the key, so there is
                    # nothing to pass in and nothing to keep in sync.
                    {
                        "name": "BIAL_BASE_PATH",
                        "value": base_path_for(published_app_name(app_id)),
                        "isSecret": False,
                    },
                    # The one hostname every generated app is served from. Baked for the same
                    # reason, and read for a different one: Next checks a Server Action's
                    # browser `Origin` against the forwarded host, which stops matching the
                    # moment traffic arrives through the router.
                    {
                        "name": "BIAL_APPS_HOSTNAME",
                        "value": apps_hostname,
                        "isSecret": False,
                    },
                ],
            },
        )
        run_id = str(run.get("name") or "")
        if not run_id:
            raise ImageBuildError("the registry accepted the build but returned no run id")

        return await self._await_run(run_id=run_id, tag=tag)

    async def _upload(self, url: str, context: bytes) -> None:
        """PUT the source tarball to the registry-supplied blob URL.

        No ARM auth: the URL is already a SAS. This is also the leg most likely to fail in a
        VNet-integrated deployment, where a private-endpoint DNS zone can capture the whole
        `*.blob.core.windows.net` namespace and blackhole a Microsoft-managed host we cannot
        add to it."""
        try:
            response = await self._http.put(
                url, content=context, headers=_BLOB_TYPE_HEADER, timeout=_UPLOAD_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            raise ImageBuildTransientError("uploading the build context failed") from exc
        if response.status_code >= 400:
            raise ImageBuildError(f"uploading the build context returned {response.status_code}")

    async def _await_run(self, *, run_id: str, tag: str) -> BuiltImage:
        """Poll to a terminal status, then either read the digest or fetch the log."""
        deadline = asyncio.get_running_loop().time() + self._config.build_timeout_s
        while True:
            run = await self._arm("GET", f"{self._registry_id}/runs/{run_id}")
            status = str((run.get("properties") or {}).get("status") or "")
            if status in _TERMINAL_STATUSES:
                break
            if asyncio.get_running_loop().time() >= deadline:
                # The run may still be going; the platform has simply stopped waiting.
                raise ImageBuildError(
                    f"the image build did not finish within {self._config.build_timeout_s}s"
                )
            await asyncio.sleep(self._config.build_poll_interval_s)

        if status != _SUCCEEDED:
            raise ImageBuildError(
                f"the image build {status.lower()}", log_tail=await self._log_tail(run_id)
            )

        digest = _digest_of(run)
        if digest is None:
            # A build that reports success but produced no image is a platform problem, not
            # a user one — never report it as "your code failed to build".
            raise ImageBuildError(
                "the image build succeeded but produced no image digest",
                log_tail=await self._log_tail(run_id),
            )
        _log.info("image_built", run_id=run_id, digest=digest)
        return BuiltImage(digest=digest, tag=tag, run_id=run_id)

    async def _log_tail(self, run_id: str) -> str | None:
        """The registry's own build log. Best-effort: a deploy that failed for a real reason
        must not ALSO fail because the log could not be fetched — that would replace an
        actionable message with a meaningless one."""
        try:
            link = await self._arm("POST", f"{self._registry_id}/runs/{run_id}/listLogSasUrl")
            url = link.get("logLink")
            if not url:
                return None
            response = await self._http.get(str(url), timeout=_ARM_CALL_TIMEOUT_S)
            if response.status_code >= 400:
                return None
            return response.text[-_LOG_TAIL_CHARS:]
        except Exception:
            _log.warning("image_build_log_unavailable", run_id=run_id, exc_info=True)
            return None

    async def aclose(self) -> None:
        await self._http.aclose()
        close = getattr(self._credential, "close", None)
        if close is not None:
            await asyncio.to_thread(close)


def _digest_of(run: dict[str, Any]) -> str | None:
    """The digest of the image the run pushed. Loosely typed JSON, so every hop is guarded."""
    images = (run.get("properties") or {}).get("outputImages") or []
    if not images:
        return None
    digest = images[0].get("digest")
    return str(digest) if digest else None


# --- the process-wide singleton ---------------------------------------------------

_builder: ImageBuilder | None = None


def get_image_builder() -> ImageBuilder:
    global _builder
    if _builder is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config
        from src.services.deploy.aca_publish import DeployNotConfiguredError

        if settings.deploy is None:
            raise DeployNotConfiguredError(
                "publishing is not configured: set the DEPLOY__* block."
            )
        if settings.deploy.image_builder == "local_docker":
            # Imported HERE, not at module scope: `local_images` imports this module for
            # `BuiltImage`/`ImageBuildError`, so a top-level import is a cycle.
            from src.services.deploy.local_images import LocalDockerImageBuilder

            _builder = LocalDockerImageBuilder(settings.deploy)
        else:
            _builder = AcrImageBuilder(settings.deploy)
    return _builder


async def aclose_image_builder() -> None:
    global _builder
    builder, _builder = _builder, None
    if builder is not None:
        await builder.aclose()
