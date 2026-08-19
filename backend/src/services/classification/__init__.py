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
* `constants` — the review loop's ceilings and cache/effort settings.

The runner (`service`), the credential scan (`scan`) and the stricter-of merge (`merge`)
land in later units and join this package beside the store.
"""
