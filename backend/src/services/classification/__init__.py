"""The pre-publish classification review — the AI check that pre-fills the six
data-classification questions from an app's last saved code.

Module map (mirrors `services/deploy`'s layout; consumers import the module they need,
`from src.services.classification import store`):

* `store` — the one-row-per-app review row: claim-or-return, guarded terminal writes.

The runner (`service`), the credential scan (`scan`) and the stricter-of merge (`merge`)
land in later units and join this package beside the store.
"""
