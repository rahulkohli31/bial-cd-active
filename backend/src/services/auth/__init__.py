"""Backend-owned Entra ID authentication (ADR-0007).

The FastAPI control-plane runs the OpenID Connect Authorization-Code + PKCE flow
itself (Authlib), validates the Entra token fail-closed against a single
hard-configured tenant, upserts the user by the stable Entra Object ID, and mints
its own cookie session (short-lived session JWT + rotating reuse-detected refresh
token + CSRF). This package holds the config, crypto primitives, OIDC client, and
refresh-rotation service; the HTTP endpoints live under `api/v1/auth/`.
"""
