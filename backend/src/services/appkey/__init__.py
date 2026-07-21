"""The SPA's CORS layer — the app's only one (`main.py` installs it INSTEAD of Starlette's
`CORSMiddleware`). The package is named for the per-request app-key auth chain it used to
hold; that chain was retired with the shared data plane it guarded, since deployed apps now
talk to their own per-project database. Public surface via explicit re-exports."""

from src.services.appkey.cors import ScopedCORSMiddleware as ScopedCORSMiddleware
