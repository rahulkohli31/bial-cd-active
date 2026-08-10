"""Platform-owned files copied verbatim into every generated app's Docker build context.

A package (not a bare directory) so `importlib.resources` can read them out of the
installed backend image without any path arithmetic — and so they travel wherever the
backend does.

NOTHING HERE IS PYTHON. These are the app `Dockerfile`, its `.dockerignore`, the strict
production migrator, the Next config wrapper, and a `public/` placeholder. They are read as
bytes and never imported or executed by the control plane.

They are pinned to LF in the root `.gitattributes`: the backend image is built on a Windows
VM, and a CRLF checkout would bake `\\r` into the app Dockerfile and the migrator, breaking
the build for every citizen at once. `tests/services/deploy/test_assets.py` asserts it.
"""
