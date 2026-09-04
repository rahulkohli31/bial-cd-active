"""Build a container image with the LOCAL docker daemon, for subscriptions where ACR Tasks
are refused.

WHY THIS EXISTS. `images.py` is the shipping path: the registry's own build agent does the
build and the push, so the control plane needs no docker daemon and never holds a pushable
credential. That path is unavailable on a free-trial subscription, which answers every
`scheduleRun` with `TasksOperationsNotAllowed` — the whole publish half of the product is
then untestable. This builder trades the property that makes the ACR path good (no local
daemon, no push credential) for the only thing that matters in that situation: being able
to run the pipeline at all.

SO IT IS NOT THE SHIPPING PATH, and the default keeps pointing at ACR Tasks. It is selected
explicitly by `DEPLOY__IMAGE_BUILDER=local_docker`, and it refuses to run in production —
a control plane that silently started shelling out to docker on a BIAL host would be a
worse outcome than a failed publish.

WHAT IT MUST MATCH. Same tag (`names.image_tag`), same three build arguments, same
`linux/amd64` platform, same `Dockerfile` path named explicitly so a Dockerfile smuggled
elsewhere in the citizen's workspace is never the one that gets built, and the same
`BuiltImage(digest, tag, run_id)` — the container spec pins the digest, so producing one is
not optional. The digest comes from buildx's own `--metadata-file` rather than a second
`imagetools inspect` call: it is what the push actually wrote, and it costs no round trip.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Final

import structlog

from src.services.deploy.config import DeployConfig
from src.services.deploy.images import BuiltImage, ImageBuildError, ImageBuildTransientError
from src.services.deploy.names import image_tag, published_app_name
from src.services.sandbox.base import base_path_for

_log = structlog.get_logger()

# Same cap as the ACR path: build output is attacker-influenced text from a workspace the
# citizen's AI drove, and the caller redacts and re-caps it anyway.
_LOG_TAIL_CHARS: Final = 32_000

# `docker login` is a small control-plane call against the registry, not the build.
_LOGIN_TIMEOUT_S: Final = 60.0


def _tail(text: str) -> str:
    return text[-_LOG_TAIL_CHARS:]


class LocalDockerImageBuilder:
    """`docker buildx build --platform linux/amd64 --push`, driven as a subprocess."""

    def __init__(self, config: DeployConfig) -> None:
        self._config = config
        # One login per process, not per build: the credential does not rotate mid-run and
        # a login on every publish is a needless round trip against the registry.
        self._logged_in = False
        self._login_lock = asyncio.Lock()

    async def _run(
        self, *args: str, timeout: float, stdin: bytes | None = None, cwd: Path | None = None
    ) -> tuple[int, str]:
        """Run a subprocess, capturing stdout+stderr together. Returns (exit code, output).

        Merged streams on purpose: buildx writes its progress to stderr and its errors to
        both, and the citizen-facing failure needs the whole thing in order."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd is not None else None,
            )
        except OSError as exc:
            # No docker on PATH is a configuration error, not a build failure — say so in
            # the words an operator can act on.
            raise ImageBuildError(
                "the local docker builder is selected but docker could not be executed: "
                "install Docker, or set DEPLOY__IMAGE_BUILDER=acr_tasks"
            ) from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ImageBuildError(f"the build exceeded {timeout:.0f}s and was killed") from None
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    async def _login(self) -> None:
        async with self._login_lock:
            if self._logged_in:
                return
            c = self._config
            code, out = await self._run(
                "docker",
                "login",
                c.acr_server,
                "--username",
                c.acr_username,
                "--password-stdin",
                stdin=c.acr_password.get_secret_value().encode(),
                timeout=_LOGIN_TIMEOUT_S,
            )
            if code != 0:
                # Transient: a registry that is briefly unreachable is retryable, and the
                # caller's retry policy is the right place to decide that.
                raise ImageBuildTransientError(
                    f"docker login to {c.acr_server} failed", log_tail=_tail(out)
                )
            self._logged_in = True

    async def build(
        self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes
    ) -> BuiltImage:
        from src.config import settings  # lazy: a module-level import is a cycle

        c = self._config
        tag = image_tag(
            repository_prefix=c.image_repository_prefix,
            app_id=app_id,
            deployment_id=deployment_id,
        )
        reference = f"{c.acr_server}/{tag}"

        await self._login()

        with tempfile.TemporaryDirectory(prefix="bial-build-") as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            # `filter="data"` refuses absolute paths, `..` traversal, links and device
            # nodes. The tarball is written by `context.build_context` a few frames up, but
            # this is the one place the bytes become files on the control plane's own disk
            # and it is not the place to assume provenance.
            try:
                with tarfile.open(fileobj=io.BytesIO(context), mode="r:gz") as archive:
                    archive.extractall(source, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise ImageBuildError("the build context could not be unpacked") from exc

            metadata = root / "metadata.json"
            code, out = await self._run(
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--push",
                "--metadata-file",
                str(metadata),
                # Named EXPLICITLY, exactly as the ACR path does it.
                "--file",
                str(source / "Dockerfile"),
                "--tag",
                reference,
                "--build-arg",
                f"NODE_IMAGE={c.node_base_image}",
                "--build-arg",
                f"BIAL_BASE_PATH={base_path_for(published_app_name(app_id))}",
                "--build-arg",
                f"BIAL_APPS_HOSTNAME={settings.apps_hostname}",
                str(source),
                timeout=float(c.build_timeout_s),
            )
            if code != 0:
                raise ImageBuildError("the image build failed", log_tail=_tail(out))

            digest = _digest_of(metadata)
            if digest is None:
                raise ImageBuildError(
                    "the build reported success but wrote no image digest", log_tail=_tail(out)
                )

        _log.info("local_image_built", app_id=str(app_id), tag=tag, digest=digest)
        # There is no ARM run to point an operator at, so the run id carries the deployment
        # instead — the same thing the tag encodes, and the only identifier that exists.
        return BuiltImage(digest=digest, tag=tag, run_id=f"local-{deployment_id.hex[:12]}")

    async def aclose(self) -> None:
        """Nothing to close: every build is a subprocess that has already exited."""
        return None


def _digest_of(metadata: Path) -> str | None:
    """The digest buildx recorded for the manifest it pushed. Loosely typed JSON written by
    another process, so every hop is guarded."""
    try:
        payload = json.loads(metadata.read_text())
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    digest = payload.get("containerimage.digest")
    return digest if isinstance(digest, str) and digest else None
