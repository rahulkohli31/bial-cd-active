"""The `X-App-Key` auth chain + sandbox CORS — the security spine of the
deployed-app data plane. Public surface via explicit re-exports."""

from src.services.appkey.chain import AppContext as AppContext
from src.services.appkey.chain import InjectedUser as InjectedUser
from src.services.appkey.chain import RequireAppKey as RequireAppKey
from src.services.appkey.chain import make_per_app_limiter as make_per_app_limiter
from src.services.appkey.chain import require_app_key as require_app_key
from src.services.appkey.chain import require_login_if_required as require_login_if_required
from src.services.appkey.cors import ScopedCORSMiddleware as ScopedCORSMiddleware
from src.services.appkey.cors import is_data_route as is_data_route
