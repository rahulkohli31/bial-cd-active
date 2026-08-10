"""The ACR Tasks image builder, over ARM REST.

Every call is asserted against `httpx.MockTransport` — the same shape
`tests/services/sandbox/test_client.py` uses for the supervisor. What matters here is the
WIRE: the API version (the GA versions do not carry these routes at all, so a bump turns
every build into a 404), the explicit Dockerfile path, the blob-type header the upload
needs, and the fact that no secret is ever put in a build argument.

The 403 case gets its own test because it is the single most likely first-run failure and
no retry can fix it — the message has to name the missing grant.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.services.deploy.config import DeployConfig
from src.services.deploy.images import (
    AcrImageBuilder,
    ImageBuildError,
    ImageBuildTransientError,
)

_APP_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
_DEPLOY_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_DIGEST = "sha256:" + "ab" * 32
_UPLOAD_URL = "https://acrbuildsource.blob.core.windows.net/src/x?sv=2024&sig=abc"


def _config(**overrides: Any) -> DeployConfig:
    base: dict[str, Any] = {
        "acr_server": "bialgenaicr.azurecr.io",
        "acr_name": "bialgenaicr",
        "acr_resource_group": "BIAL-GENAI-AIML-RG",
        "acr_subscription_id": "sub-acr",
        "acr_username": "bialgenaicr",
        "acr_password": SecretStr("acr-pass"),
        "subscription_id": "sub",
        "resource_group": "rg",
        "region": "centralindia",
        "managed_environment_name": "env",
        "build_poll_interval_s": 0.001,
    }
    values: dict[str, Any] = {**base, **overrides}
    return DeployConfig(**values)


def _builder(handler, **config_overrides: Any) -> AcrImageBuilder:
    return AcrImageBuilder(
        _config(**config_overrides),
        transport=httpx.MockTransport(handler),
        credential=SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token="tok")),
    )


def _happy(
    record: list[httpx.Request], *, status: str = "Succeeded", digest: str | None = _DIGEST
):
    """A registry that accepts an upload, schedules a run, and reports `status`."""

    def handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        path = request.url.path
        if path.endswith("/listBuildSourceUploadUrl"):
            return httpx.Response(200, json={"uploadUrl": _UPLOAD_URL, "relativePath": "src/x"})
        if request.url.host.endswith("blob.core.windows.net"):
            return httpx.Response(201)
        if path.endswith("/scheduleRun"):
            return httpx.Response(200, json={"name": "ca123"})
        if path.endswith("/listLogSasUrl"):
            return httpx.Response(200, json={"logLink": "https://logs.example/x"})
        if path.startswith("https://logs.example") or request.url.host == "logs.example":
            return httpx.Response(200, text="the build log")
        if "/runs/" in path:
            images = [{"digest": digest}] if digest else []
            return httpx.Response(
                200, json={"properties": {"status": status, "outputImages": images}}
            )
        return httpx.Response(404)

    return handler


async def test_a_successful_build_returns_the_digest() -> None:
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls))

    built = await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    assert built.digest == _DIGEST
    assert built.run_id == "ca123"
    assert built.tag == f"citizen-apps/{_APP_ID}:{_DEPLOY_ID.hex[:12]}"
    await builder.aclose()


async def test_the_tasks_api_version_is_pinned() -> None:
    """The GA API versions do not carry these routes AT ALL — a bump turns every build into
    a 404, and it would look like a permissions problem."""
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls))
    await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    arm = [c for c in calls if c.url.host == "management.azure.com"]
    assert arm
    assert all(c.url.params["api-version"] == "2019-06-01-preview" for c in arm)
    await builder.aclose()


async def test_the_dockerfile_path_is_named_explicitly() -> None:
    """So a Dockerfile smuggled at any other path in the citizen's workspace is never built."""
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls))
    await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    import json

    schedule = next(c for c in calls if c.url.path.endswith("/scheduleRun"))
    body = json.loads(schedule.content)
    assert body["dockerFilePath"] == "Dockerfile"
    assert body["type"] == "DockerBuildRequest"
    assert body["isPushEnabled"] is True
    assert body["platform"] == {"os": "Linux", "architecture": "amd64"}
    await builder.aclose()


async def test_no_secret_is_passed_as_a_build_argument() -> None:
    """ARM logs request bodies. A build arg is not a place for a credential."""
    import json

    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls))
    await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    body = json.loads(next(c for c in calls if c.url.path.endswith("/scheduleRun")).content)
    names = {a["name"] for a in body["arguments"]}
    assert names == {"NODE_IMAGE"}
    assert all(a["isSecret"] is False for a in body["arguments"])
    assert "acr-pass" not in json.dumps(body)
    await builder.aclose()


async def test_the_upload_declares_a_block_blob() -> None:
    """Without this header the PUT is rejected, and the failure reads as a network problem."""
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls))
    await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tarbytes")

    upload = next(c for c in calls if c.url.host.endswith("blob.core.windows.net"))
    assert upload.method == "PUT"
    assert upload.headers["x-ms-blob-type"] == "BlockBlob"
    assert upload.content == b"tarbytes"
    # The upload uses the registry-supplied SAS, never our ARM bearer.
    assert "Authorization" not in upload.headers
    await builder.aclose()


# --- failures ---------------------------------------------------------------------


async def test_a_missing_grant_names_the_missing_grant() -> None:
    """The most likely first-run failure, and no retry can fix it. An opaque 403 hours into
    a rollout is the worst possible message here."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})

    builder = _builder(handler)
    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    assert "not authorized" in str(caught.value)
    assert "scheduleRun" in str(caught.value)
    assert not isinstance(caught.value, ImageBuildTransientError)
    await builder.aclose()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_throttling_and_5xx_are_retryable(status: int) -> None:
    builder = _builder(lambda request: httpx.Response(status))
    with pytest.raises(ImageBuildTransientError):
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")
    await builder.aclose()


async def test_a_failed_build_carries_the_log_back() -> None:
    """This text is what reaches the citizen's chat — without it the message is "the build
    failed" and nothing else."""
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls, status="Failed"))

    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    assert "failed" in str(caught.value)
    assert caught.value.log_tail == "the build log"
    await builder.aclose()


async def test_a_build_that_pushes_nothing_is_not_reported_as_the_users_fault() -> None:
    """Success with no digest is a PLATFORM problem. Reporting it as a code error would send
    a citizen editing code that is fine."""
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls, digest=None))

    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")
    assert "no image digest" in str(caught.value)
    await builder.aclose()


async def test_an_unfetchable_log_does_not_replace_the_real_error() -> None:
    """A deploy that failed for a real reason must not ALSO fail because the log could not be
    read — that would swap an actionable message for a meaningless one."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/listBuildSourceUploadUrl"):
            return httpx.Response(200, json={"uploadUrl": _UPLOAD_URL, "relativePath": "src/x"})
        if request.url.host.endswith("blob.core.windows.net"):
            return httpx.Response(201)
        if path.endswith("/scheduleRun"):
            return httpx.Response(200, json={"name": "ca123"})
        if path.endswith("/listLogSasUrl"):
            return httpx.Response(500)
        return httpx.Response(200, json={"properties": {"status": "Failed", "outputImages": []}})

    builder = _builder(handler)
    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")

    assert "failed" in str(caught.value)
    assert caught.value.log_tail is None
    await builder.aclose()


async def test_a_run_that_never_finishes_is_bounded() -> None:
    calls: list[httpx.Request] = []
    builder = _builder(_happy(calls, status="Running"), build_timeout_s=1)

    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")
    assert "did not finish" in str(caught.value)
    await builder.aclose()


async def test_a_registry_that_returns_no_upload_location_fails_clearly() -> None:
    builder = _builder(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ImageBuildError) as caught:
        await builder.build(app_id=_APP_ID, deployment_id=_DEPLOY_ID, context=b"tar")
    assert "no source upload location" in str(caught.value)
    await builder.aclose()
