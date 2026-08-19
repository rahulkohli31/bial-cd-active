"""The pre-publish classification review — the AI check that pre-fills the six
data-classification questions from an app's last saved code.

Module map (mirrors `services/deploy`'s layout; consumers import the module they need,
`from src.services.classification import store`):

* `store` — the one-row-per-app review row: claim-or-return, guarded terminal writes.
* `agent` — the module-level review agent: no bound model, tool-calling structured
  output, the thinking-off guard, and `run_review` (the entry the runner calls).
* `schema` — the structured output: six verdicts in evidence → reason → verdict order,
  plus the completeness signal.
* `prompts` — the static rubric (byte-identical, cache-fronted) and the volatile
  per-run prompt carrying the file listing and the scan's hits.
* `constants` — the review loop's ceilings, budgets and cache/effort settings.
* `scan` — the model-free credential sweep over the extracted tree: the read-tools
  jail applied to its own walk, per-file truncation surfacing as an INCOMPLETE sweep.
* `merge` — the PURE per-question merge (three sources, one effective answer): the
  truth table U9's stricter-of gate consumes, with the recorded disagreement kinds.
* `service` — the runner: the two-verb contract (start / read), the detached run with
  its throwaway extraction, the scan-first prompt, the guided truncation retry, the
  failure taxonomy, the Tier A floor, and the per-run usage + P7 audit records.
"""
