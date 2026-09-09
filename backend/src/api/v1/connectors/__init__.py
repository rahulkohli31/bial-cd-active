"""The `connectors` domain: the citizen's own connector access — what the registry offers, where
this person stands with each entry, and the two writes they may make about themselves (ask, and
withdraw the ask).

ACCESS BELONGS TO THE PERSON (R2), so everything here keys on `user_id` alone. The per-project
switch and the days a project reads are a different surface entirely, under
`/v1/projects/{project_id}/connectors`; the administrator's queue is a third, under `/v1/admin`.
"""
