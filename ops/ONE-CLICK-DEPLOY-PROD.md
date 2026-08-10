# One-click deploy — what production needs

Everything in this PR runs today against the personal dev subscription. Three things are
required before it runs against BIAL, and only the first has a lead time.

---

## 1. The registry role grant — BLOCKING, file it first

The control plane asks ACR to build each generated app's image. That is an ARM action, so it
needs a role on the registry. **It does not need `AcrPush`** — the control plane never
pushes; ACR's own build agent does. Five read/schedule actions, scoped to the single
registry resource rather than the resource group.

```sh
# 1. create the role (paste-ready definition lives beside this file)
az role definition create --role-definition @ops/citizen-dev-image-builder-role.json

# 2. assign it to the backend App Service's managed identity
az role assignment create \
  --assignee deb3e39f-69d7-4a7e-ae91-a858b48e0848 \
  --role "Citizen Dev Image Builder" \
  --scope "/subscriptions/5aa185fd-1f15-4769-94c9-b7dae72cadd5/resourceGroups/BIAL-GENAI-AIML-RG/providers/Microsoft.ContainerRegistry/registries/bialgenaicr"
```

Same identity and the same shape as the existing `Citizen Dev Container Apps Operator` grant
(tracker Step 4), so this is that conversation again at a tighter scope. Note it is
**cross-resource-group**: the registry lives in `BIAL-GENAI-AIML-RG` while the runtime is in
`BIAL-GENAI-DEV-RG`.

**Verify it landed** — this is the exact first call the pipeline makes:

```sh
az rest --method post --url "https://management.azure.com/subscriptions/5aa185fd-1f15-4769-94c9-b7dae72cadd5/resourceGroups/BIAL-GENAI-AIML-RG/providers/Microsoft.ContainerRegistry/registries/bialgenaicr/listBuildSourceUploadUrl?api-version=2019-06-01-preview"
```

A JSON body with `uploadUrl` means the grant is good. A 403 means it is not — and the
pipeline reports that case with a message naming the missing actions rather than an opaque
failure.

> **Known gap on the dev subscription.** `scheduleRun` there returns
> `TasksOperationsNotAllowed` — ACR Tasks is not permitted on a free-trial subscription, which
> is what `sandbox/Dockerfile.sandbox` already records. Everything else in the pipeline has
> been exercised end to end against real Azure; that one call has not, and BIAL's registry is
> the first place it can be.

---

## 2. The `DEPLOY__*` app settings

Set on the **backend** App Service. The ACA targeting mirrors the `SANDBOX__*` block already
there; only the two ACR resource fields are genuinely new.

| Setting | Value |
|---|---|
| `DEPLOY__ACR_SERVER` | `bialgenaicr.azurecr.io` — the login **host** |
| `DEPLOY__ACR_NAME` | `bialgenaicr` — the ARM **resource** name |
| `DEPLOY__ACR_RESOURCE_GROUP` | `BIAL-GENAI-AIML-RG` |
| `DEPLOY__ACR_SUBSCRIPTION_ID` | `5aa185fd-1f15-4769-94c9-b7dae72cadd5` |
| `DEPLOY__ACR_USERNAME` / `_PASSWORD` | same admin credential as `SANDBOX__ACR_*` |
| `DEPLOY__SUBSCRIPTION_ID` | `5aa185fd-1f15-4769-94c9-b7dae72cadd5` |
| `DEPLOY__RESOURCE_GROUP` | `BIAL-GENAI-DEV-RG` |
| `DEPLOY__REGION` | `centralindia` |
| `DEPLOY__MANAGED_ENVIRONMENT_NAME` | `bial-citizen-dev-aca-env` |

`ACR_SERVER` and `ACR_NAME` look interchangeable and are not — one goes into an image
reference and the ACA pull credential, the other is what the Tasks API addresses. A startup
validator refuses a mismatch rather than letting it fail hours later.

Everything else has a working default: scale-to-zero, `maxReplicas 2`, port 3000,
`citizen-apps` repository prefix, and the build/ready timeouts.

**There is no production gate on this block.** `deploy is None` means publishing is off,
which is the correct posture until the grant lands — the backend boots fine without it. Add
`_require_deploy_in_production` in the same commit that makes the portal show a Deploy
control unconditionally.

---

## 3. Two pre-flight checks — 15 minutes each, from the Kudu console

Both can silently sink a deploy, and both are cheaper to check than to debug.

**Can the backend PUT to an arbitrary `*.blob.core.windows.net` host?** ACR hands back an
upload URL on a Microsoft-managed storage account. The private endpoint for
`bialvibecodingdatast01` links `privatelink.blob.core.windows.net` to the VNet, which can
capture that whole namespace and blackhole a host we cannot add to the zone. If it fails,
`sourceLocation` also accepts a remote tarball URL — upload to `bialvibecodingdatast01` and
pass a short-lived read SAS instead.

**Is the ACA environment's private DNS zone linked to `bial-vnet-12`?** Without it nobody can
open a published URL from the corporate network. Already an open tracker item, and it
unblocks sandbox previews too.

While there: **read the environment's `defaultDomain`** and record it in the tracker. Every
published URL is `pub-<28 hex>.<defaultDomain>`, and it is still noted as "read from
`env show`".

---

## What is already true in production

No new infrastructure. Published apps run in the **existing** `bial-citizen-dev-aca-env`
beside the per-user sandboxes — different name prefix (`pub-` against `sbx-`), and
structurally invisible to the sandbox reaper, which sweeps the Redis registry that publish
never writes to. They use the project's **existing** per-project database and Blob container.
At `minReplicas: 0` a sleeping app costs nothing and holds no environment cores, which is
what keeps the `/27` infrastructure subnet viable as app count grows.

---

## Out of scope, and worth stating plainly

**Published apps have no authentication, and are reachable on the public internet by
anyone with the URL — not just staff.** (Corrected per issue #115: this doc previously
claimed the ACA environment was internal-only, which contradicted `deploy/config.py`'s
own `ingress: "external"` default and `sandbox/config.py`'s honest comment on the
identical `bial-citizen-dev-aca-env` this section already says published apps share —
"POC = public ingress; internal/VNet ingress is deferred hardening." Published apps
inherit that same public-ingress posture; there is no separate, more restrictive network
boundary for them.) The URL is unguessable but not secret once shared, and that is the
whole of the current protection. Closing it — an authenticated proxy, or moving to actual
VNet-internal ingress — is a separate task.

Also deferred: blue/green traffic splitting, custom domains, ACR image retention, and
rollback beyond ACA keeping the previous revision serving when a new one fails to activate.
