"""Dev/E2E tooling that is not itself a test.

Lives under `tests/` rather than `src/` on purpose: the Dockerfile ships only
`COPY src ./src`, so nothing here reaches a production image — which matters
for `mint_dev_session.py`, which mints real session cookies while bypassing the
Entra ID round-trip. `tests/` is still inside every ADR-0003 gate's scope
(ruff, ty, `mypy src tests`, and pyright's `include`), so the tooling stays
type-checked without being shipped.
"""
