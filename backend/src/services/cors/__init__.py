"""The SPA's CORS layer — the app's only one (`main.py` installs it INSTEAD of Starlette's
`CORSMiddleware`). Formerly the `appkey` package that held the per-request app-key auth
chain; that chain was retired with the shared data plane it guarded (deployed apps now talk
to their own per-project database), leaving only the CORS middleware — hence the rename to
`cors`. Public surface via explicit re-exports."""

from src.services.cors.middleware import ScopedCORSMiddleware as ScopedCORSMiddleware
