"""The pre-publish classification review — the AI check that answers the administrator-configured
classes Yes or No from an app's last saved code.

Module map (mirrors `services/deploy`'s layout; consumers import the module they need,
`from src.services.classification import store`):

* `store` — the one-row-per-app review row: claim-or-return, guarded terminal writes.
* `config` — the live configuration (the active classes and the policy) and the fingerprint of
  the class definitions a review reads.
* `agent` — the module-level review agent: no bound model, tool-calling structured
  output, the thinking-off guard, and `run_review` (the entry the runner calls).
* `schema` — the structured output: one answer per class in evidence → reason → verdict order.
* `prompts` — the static instruction block around the class definitions (byte-identical per
  fingerprint, cache-fronted) and the volatile per-run prompt.
* `constants` — the review loop's ceilings, budgets and cache/effort settings.
* `scan` — the model-free credential sweep over the extracted tree.
* `service` — the runner: the two-verb contract (start / read), the detached run with
  its throwaway extraction, the guided truncation retry, the failure taxonomy, and the
  per-run usage and audit records.
"""
