# One-click deploy — what production needs

Everything in this PR runs today against the personal dev subscription. Three things are
required before it runs against BIAL, and only the first has a lead time.

**Since the connector data plane landed there is a fourth**, needed only if BIAL wants published
apps to read connector data — see "The connector's managed identity" below. It has its own lead
time, because it is a grant on somebody else's resource.

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

**Is the ACA environment's private DNS zone linked to `bial-vnet-12`?** This is about NAME
RESOLUTION, not network reachability — without it, a client on the corporate network
cannot resolve a published URL's hostname to an address at all, regardless of whether the
underlying network path would otherwise be reachable from the internet, the VNet, or both
(see "Out of scope" below — that's still unconfirmed). Already an open tracker item, and it
unblocks sandbox previews too.

While there: **read the environment's `defaultDomain`** and record it in the tracker. Every
published URL is `pub-<28 hex>.<defaultDomain>`, and it is still noted as "read from
`env show`".

---

## 4. The connector's managed identity — only if published apps read connector data

A published app reads the connected system's data **directly**, with a user-assigned managed
identity attached to its container app. There is no server-side proxy and no copy of that data
inside the platform, which is what keeps the platform out of the data path — and it means the
grant has to exist in ARM before the first publish, not at first read.

**Two grants, on two different resources, for two different principals.** They are commonly
confused, and getting them the wrong way round produces two different failures that look alike.

| | Principal | Role | Scope |
|---|---|---|---|
| **To attach the identity** | the control plane's own principal — the backend App Service's managed identity in production, i.e. whatever `DefaultAzureCredential` resolves to when the API calls ARM | **Managed Identity Operator** | the user-assigned identity itself |
| **To read the data** | the user-assigned identity | **Storage Blob Data Reader** | the one container, never the account |

The first is the one that has never been checked, and it is easy to miss precisely because the
platform *already* creates container apps: creating them needs rights on the resource **group**,
while assigning an identity needs a role on **the identity**, which is a different resource. The
underlying permission is `Microsoft.ManagedIdentity/userAssignedIdentities/assign/action`; an
administrator who prefers to verify rather than grant should check for that action.

Its symptom is loud and immediate rather than silent: without it **every container create is
refused**, so a missing grant stops builds, not just connector reads.

```sh
# the control plane may attach this identity
az role assignment create \
  --assignee <backend App Service principal id> \
  --role "Managed Identity Operator" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<identity name>"

# the identity may read the one container (BIAL reports this already exists)
az role assignment create \
  --assignee <identity's principal id> \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>/blobServices/default/containers/<container>"
```

### The identity must also be attached to the BACKEND App Service — easy to miss, silent when missed

The table above covers the two grants. There is a third thing, and it is not a role assignment at
all: **the user-assigned identity has to be attached to the backend App Service itself.**

The control plane copies each project's window into Redis with
`ManagedIdentityCredential(client_id=<the lake identity>)`, running inside the backend App
Service. Azure's identity endpoint will only mint a token for an identity that is **assigned to
the resource asking** — a role grant is not enough, and neither is the fact that the same identity
is attached to every container app. Without the attachment the credential fails on every attempt,
and because the copy is a detached task whose failures are deliberately logged-and-swallowed,
**nothing user-facing ever changes**: builds succeed, the rail says the connector is on, and the
copy simply never happens. The evidence is a `lake_window_copy_failed` line and nothing else.

```sh
az webapp identity assign -n low-code-no-code-platform -g <rg> \
  --identities "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<name>"
```

**Then check what that did to `DefaultAzureCredential`.** Three call sites build a credential with
no client id — `services/storage/azure_backend.py`, `services/sandbox/aca.py`,
`services/deploy/aca_publish.py` — and on App Service the identity endpoint resolves a bare
request to the **system-assigned** identity. If the App Service has a system-assigned identity
today, adding a user-assigned one alongside it changes nothing for those three. If it does not,
and they are resolving a lone user-assigned identity implicitly, adding a second makes the request
ambiguous and **blob storage, sandbox creation and publish all start failing at once**. Confirm
which posture is live before attaching:

```sh
az webapp identity show -n low-code-no-code-platform -g <rg> \
  --query "{system: principalId, user: userAssignedIdentities}"
```

A non-null `principalId` means a system-assigned identity exists and the attachment is safe.

### Pre-flight: confirm that a redeploy really does revoke

The platform's revocation story for a published app is "redeploy it after the owner's access is
withdrawn, and the identity comes off". The code now sends ARM's explicit detach
(`identity: {type: "None"}`) rather than omitting the property, which is the documented way to do
it — but **it has never been observed**, because ARM writes are blocked on the development
subscription. Confirm it once, against a throwaway app, before relying on it in an incident:

```sh
# attach, then redeploy with the gate closed, then read it back
az containerapp show -n <app> -g <rg> --query "identity"
```

Expect `{"type": "None"}` and an empty `userAssignedIdentities`. If the identity survives, the
revocation path is not the redeploy and the runbook needs a manual
`az containerapp identity remove` step instead.

### The three app settings

```
CONNECTOR_LAKE__URL=https://<account>.blob.core.windows.net/<container>/<folder>/
CONNECTOR_LAKE__IDENTITY_CLIENT_ID=<the identity's CLIENT id>
CONNECTOR_LAKE__IDENTITY_RESOURCE_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<name>
```

The last two are **the same identity in two different vocabularies** and are not
interchangeable. The client id is what the credential inside a container names; the resource id
is the key ARM's identity map uses, and the only value that can attach anything. Leave all three
unset and the feature is simply off: no build and no published app is handed coordinates or an
identity, and nothing is copied.

None of the three is a bearer credential — a URL and two identity identifiers are labels. The
credential is the managed identity, which Azure mints inside the container and which cannot be
copied out. That is why they ride the container spec as plain environment values rather than as
ACA secret references.

### The admin go-live lineage takes this line, not an abstraction

The one-click path above builds the identity block into the container spec. The **manual**
go-live path is a human running `az containerapp create`; it has no envelope in code, so it
needs `--user-assigned <resource id>` added by hand, and the app settings above supplied the
same way. Written down here rather than abstracted, because there is no seam to put an
abstraction in.

### Turn on Storage diagnostic logging before the first production read

It is the only record of what an app actually read. Reads are direct by design, so the control
plane sees none of them, and the identity is shared across apps with no revocation story — so
without diagnostic logging a misuse question has no evidence on either side, for or against. It
is an ops setting on the storage account, it is cheap, and it is not code.

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

**Published apps have no authentication.** `deploy/config.py`'s `ingress: "external"`
default (same as `sandbox/config.py`'s identical setting on this same
`bial-citizen-dev-aca-env`) is reachable outside the Container Apps environment — that
part is verified, this repo's own config. Whether the managed environment's own network
posture (VNet integration, an internal load balancer) further restricts that to the
corporate network is **unconfirmed**; settle it against the live resource before relying
on either posture for a security decision:
```
az containerapp env show -n bial-citizen-dev-aca-env -g <rg> \
  --query "{internal: properties.vnetConfiguration.internal}"
```
Until confirmed, treat a published app as reachable on the public internet by anyone with
the URL, not just staff — the safer assumption. The URL is unguessable but not secret once
shared, and that is the whole of the current protection either way. Closing it — an
authenticated proxy, or confirmed + enforced VNet-internal ingress — is a separate task.

**A published app's connector reach exceeds its builder's grant, and there is no revocation.**
Both are accepted and recorded rather than solved. The app reads with the shared identity, which
is scoped to one container and read-only — but it is not scoped to the window the project picked,
and an app published by somebody whose access is later withdrawn keeps reading. Closing either
means proxying reads through the control plane, which was considered and rejected: it would put
the platform back in the data path the direct-read design exists to keep it out of.

**The build-time sandbox now holds the same identity, and it has public ingress.** The accepted
risk above was reasoned about the *published* app, which sits behind the portal's login. The
sandbox does not: it defaults to external Container Apps ingress by a recorded POC decision, and
it runs agent-authored code with outbound network access. Whether sandbox egress is restricted is
**unestablished**, and it decides whether "read-only, one container, behind a login" is still an
adequate description once a build holds the credential. Establish it, and tell BIAL the answer
alongside the revocation gap — do not inherit a note written about a different surface.

**The Redis copy budget can fail to converge once enough projects are active.** The 300 MB
family is shared and evicted oldest-first, which is recorded and accepted — but the second-order
effect is not obvious: a project whose files were evicted re-copies them on its next container
birth and evicts somebody else's, so past roughly four concurrent 75 MB windows the platform can
sit in a steady state of re-downloading the same windows from Azure forever. It costs egress and
Redis writes, not correctness, and nothing reads the copy yet. The `evicted` and `copied` fields
on the `lake_window_copied` log line are the signal; a copy that evicts as much as it copies, on
every birth, is this condition. Two candidate remedies when it matters: cap a single window at a
share of the budget, or hold a short-TTL "attempted" marker keyed by the file-set digest so a
birth does not re-copy a set it just lost.

**Switching a connector on takes effect at the next container birth, not immediately.** The
session lock refuses the change while a turn is in flight, but between turns the change is
accepted and the container is not rebuilt — a container gets its environment exactly once, at
birth. So the rail can honestly say "Reading 30 days of flight data" while the running container
holds neither the coordinates nor the identity. Today nothing reads the copy, so the gap is
invisible; before anything does, either the refusal widens to "a container exists" or the rail has
to say "takes effect when this app next starts".

**Approval is per-person; the marketplace is org-wide.** A citizen's approval is granted to them,
and the consent copy says so. But a published app is listed automatically while it has a live
deployment, and it carries the identity regardless of who opens it — so one person's approval
becomes an org-wide read path the moment they publish. This is an owner decision, not a code
defect: say it in the approver's panel, keep connector-reading apps out of the automatic listing,
or check the viewer at the app's front door. Settle it before the first production approval.

**A build sandbox can read its own bearer token out of its environment.** `IDENTITY_HEADER` is
injected into the supervisor and redacted from `/exec`, `/dev/logs` and `/files` output as a
literal. Agent-authored code running in the sandbox can still read the variable and re-encode it,
which no literal redaction can catch — so the honest statement is that a build sandbox holds the
connector credential and anything running inside it can use it, for as long as the token lives.
This is the same surface as the sandbox-public-ingress note above and should be answered with it.

Also deferred: blue/green traffic splitting, custom domains, ACR image retention, and
rollback beyond ACA keeping the previous revision serving when a new one fails to activate.
