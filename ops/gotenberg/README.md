# Gotenberg sidecar — `.pptx` → PDF for deck chat attachments

The portal accepts PowerPoint (`.pptx`) chat attachments by converting them to a
**PDF** server-side and handing that PDF to the model as a vision document — it
never sees raw `.pptx`. The conversion engine is a self-hosted
[Gotenberg](https://gotenberg.dev) sidecar (LibreOffice productized as a stateless
HTTP service), so confidential BIAL/KPMG decks **never leave the Azure tenant**.

The control plane talks to exactly one Gotenberg route
(`services/extract/deck._render_with_gotenberg`):

```
POST {GOTENBERG_URL}/forms/libreoffice/convert     (multipart, field name: files)
```

## Status: live code, no sidecar deployed

The pipeline is implemented and tested (`backend/src/services/extract/deck.py`,
`backend/tests/services/extract/test_deck.py`), and `POST /v1/attachments`
routes a `.pptx` through it. **Nothing in this repository builds, ships or runs a
Gotenberg container**, so the path is off everywhere: standing one up is a
deployment decision, not a command in this tree.

## The feature gate is one variable

```
GOTENBERG_URL     Base URL of a reachable sidecar. UNSET = deck uploads disabled.
```

That is the whole gate — `deck_attachments_enabled()` is
`bool(GOTENBERG_URL.strip())` and nothing else reads it. With it unset, an upload
is refused with a clear message rather than a 500.

**There is no `DECK_ATTACHMENTS_ENABLED`, no `MAX_DECK_PAGES` and no
`DECK_CONVERT_TIMEOUT_MS`.** Earlier revisions of this file documented all three;
they were ported from the retired Express service and never existed on the
FastAPI control plane. The three bounds they claimed to set are compile-time
constants in `deck.py`, overridable only per call:

| Bound | Constant | Value | Over-bound response |
| --- | --- | --- | --- |
| Page cap | `DEFAULT_MAX_DECK_PAGES` | 100 | clean `413` |
| Rendered-PDF size cap | `MAX_PDF_BYTES` | 50 MB | clean `413` |
| Render wall clock | `DEFAULT_TIMEOUT_SECONDS` | 60 s | clean `504` |

Change one and you are editing `deck.py`, not an environment.

## Standing a sidecar up

Point `GOTENBERG_URL` at a Gotenberg reachable **only** from the control plane —
an internal address, never a public one. Two shapes work; neither is built here:

- a sidecar container beside the API in the same deployment, on loopback; or
- a separate in-tenant service on a private address.

**Pin the Gotenberg base by digest to a release whose bundled LibreOffice meets the
security floor** (≥ 24.8.5 / 25.2.1) for CVE-2024-12425, CVE-2024-12426,
CVE-2025-1080 — the input bytes are untrusted uploads. Verify:
`docker run --rm <image> libreoffice --version`. Install the MS-metric fonts
(Carlito↔Calibri, Caladea↔Cambria + fallbacks) or decks reflow.

### Hardening checklist (mandatory, not optional)

This is the only place untrusted user bytes reach a **native renderer**
(LibreOffice) — a much larger attack surface than the in-process office
extractors. The control plane refuses a malformed or inflating deck **before** the
renderer is touched (decoded-size cap → OPC `ppt/presentation.xml` gate →
zip-bomb guard), but the page and output-size caps can only run **after** the
render, because the page count is read off the rendered PDF. An over-length deck
therefore costs one full render before it is refused — which is the reason the
container's own limits below are load-bearing rather than belt-and-braces:

- [ ] **No network egress.** Deny-by-default egress; allow only ingress from the
      control plane. The renderer never needs the internet.
- [ ] **Read-only root filesystem**, with a small writable `tmpfs` for Gotenberg's
      scratch dir only.
- [ ] **Drop all Linux capabilities**; run as the non-root `gotenberg` user
      (the base image already does); add a seccomp profile.
- [ ] **Per-job timeout + hard kill.** Set Gotenberg's API timeout at or below
      `DEFAULT_TIMEOUT_SECONDS` (60 s) so a hostile deck cannot wedge a worker
      (e.g. `gotenberg --api-timeout=60s`). The caller aborts independently too.
- [ ] **Macros disabled** (LibreOffice macro execution off — Gotenberg's default).
- [ ] **Resource limits.** Cap CPU/memory so a pathological deck degrades one job,
      not the node.
- [ ] **Patched + pinned.** Track the LibreOffice CVE floor above; redeploy on new
      advisories. Pin by digest, not a moving tag.

### Sizing

One sidecar, sized for pilot load; conversion is synchronous at attach time (1–8 s
typical). Autoscaling, an N-replica queue and async job-based conversion for very
large decks are deferred follow-up work.

## Why PDF, not text extraction

`.docx`/`.xlsx` survive as text, so the control plane extracts Markdown for them
(`services/extract/office.py`). A deck is a **visual** medium (diagrams, SmartArt,
charts, layout); text extraction would discard most of its meaning. Rendering to
PDF preserves the visuals, and the model reads PDFs with vision (per-page
rasterization + text). Accepted losses: LibreOffice may substitute fonts or drift
on complex SmartArt, and the PDF is a static final frame (no animations or
transitions).
