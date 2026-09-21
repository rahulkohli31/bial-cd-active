# Reference

Generated and machine-readable material. Everything here is either produced by a command in this
repository or copied from what the platform requires of its host, so each file can be checked
rather than trusted.

## `openapi.json` — the API reference

This is the control plane's HTTP surface: every route, request body, response shape and error
code the API serves.

It is committed rather than linked because **production serves no schema endpoint**. The
application disables the schema route, and the interactive documentation with it, whenever it runs
in production — that surface would otherwise be readable by any unauthenticated caller who could
reach the host. A reader holding this repository therefore has nothing to query, and the file
travels with the code instead.

The committed copy is the whole document, unredacted. What the production gate withholds from an
anonymous network caller is not withheld from someone holding the source: every route, permission
and error code it hides is already visible in `backend/src/api/`, so publishing the assembled
version discloses nothing a reader could not reconstruct by reading the tree.

### Reading it

It is a single large JSON document, which most code-hosting web views will not render. To read it
as documentation rather than as text, open it in any OpenAPI viewer — the format is OpenAPI 3.1,
and a viewer needs no access to a running system, only this file.

### Regenerating it

From `backend/`:

```
uv run python scripts/openapi.py --write
```

The same script checks it:

```
uv run python scripts/openapi.py --check
```

`--check` exits non-zero when the committed document no longer matches the code, and names the
operations that moved along with the command that repairs them. It is part of the static gate
sequence in `CONTRIBUTING.md`, and it needs no database, no cache and no configuration of any
kind — it builds the application from the sample environment, which is also the only environment a
fresh clone has.

Generation is pinned to that sample environment deliberately. A generator that honoured whatever
each developer had configured locally would let two people produce two different documents, and
the comparison would then report one person's settings as the other's drift.

**Route and model docstrings publish as the descriptions in this file.** A change to prose in
`backend/src/api/` moves this document, which is why the check is part of the ordinary gate run
rather than something to remember at release time.

### Running a browsable copy locally

The interactive documentation is served outside production, but reaching it means running the
control plane, which is a larger ask than reading the file: it needs a PostgreSQL instance, a
Redis instance, and an application registration in the tenant's directory. This repository
provisions none of those — see `CONTRIBUTING.md` for what has to be supplied. Reading the
committed document needs none of it.

## The Azure role definitions

Two custom role definitions describing the permissions the platform needs from its host
subscription. Each is the least privilege that lets the control plane do its job, and the reasoning
matters more than the syntax:

- **`citizen-dev-image-builder-role.json`** — lets the control plane ask the container registry to
  *build* an application's image. Read and schedule only. The control plane never pushes an image;
  the registry's own build agent does, so no push or delete permission is included, and none should
  be added.

- **`citizen-dev-aca-role.json`** — lets the control plane create, read and delete the container
  apps that run generated applications, and join them to a managed environment. It is confined to
  container apps: it cannot reach a database, a cache or a storage account sharing the same
  resource group.

`AssignableScopes` in both files carries the documented placeholder form rather than a real scope.
**The scope is chosen at assignment time**, and choosing it is a real decision — it is what decides
how far each role reaches. The image builder is written to be scoped to a single registry resource,
and the container apps role to the one resource group holding the generated applications.

A deployment may satisfy either requirement with an equivalent built-in role instead; these
definitions state what the platform needs, not the only way to grant it.
