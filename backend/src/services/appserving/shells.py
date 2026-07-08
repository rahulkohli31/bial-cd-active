"""Runner + preview HTML shells (R20, R21) — ported from Express `runner.js`
(`renderShell`/`renderFrame`) and `server.js` (`PREVIEW_SHELL`/`renderPreview`).

Three iframes, one trust model:
* **Shell** (`render_shell`) — the same-origin host page. Owns auth: for a
  login-required app it mints a short-lived token via the cookie-authed
  `/apps/{id}/runner-token` endpoint (P1 — the session cookie is HttpOnly, so there
  is no JS-readable token to read; the shell fetches a fresh one) and injects
  `{config, accessToken, user}` into the frame via postMessage. The refresh cookie
  and the raw session JWT are NEVER exposed to JS.
* **Frame** (`render_frame`) — the opaque-origin sandbox
  (`sandbox="allow-scripts allow-forms allow-downloads"`, NO `allow-same-origin`)
  serving the PRE-COMPILED app JS (no eval). Injected globals `window.__BIAL_CONFIG
  / __BIAL_TOKEN / __BIAL_USER` are set BEFORE mount.
* **Preview** (`PREVIEW_SHELL`) — the builder's live preview: same as the frame but
  compiles JSX in-browser via `@babel/standalone` and takes `previewCode` over
  postMessage; nothing is stored server-side.

The literal `</script` breakout in app code is neutralized before embedding, and
error output is rendered via `textContent` (never `innerHTML`), so app error text
cannot inject markup even inside the sandbox.
"""

# This module embeds HTML/JS/CSS template blobs verbatim; line length there is
# content, not code style, so the 99-col limit does not apply.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from src.services.appserving.data_client import bial_data_client_script

# The portal OIDC login (browser-facing, matching AUTH__REDIRECT_URI's /api/v1 edge
# convention) the shell links to when a login-required app has no session.
_SIGNIN_URL = "/api/v1/auth/login"

# Base path the injected data client prefixes onto /apps/{id}/records|files|parse.
# The edge (nginx `location /api/` + the Vite dev proxy) rewrites `^/api(/.*)$ -> /v1$1`,
# so the client must send `/api/apps/...` (which the edge turns into backend `/v1/apps/...`).
# Using `/api/v1` here would double-prefix to `/v1/v1/apps/...` and 404 every data call.
DATA_SERVICE_BASE_URL = "/api"

# Shared error renderer for the sandboxed frames: builds a red <pre> via
# createElement + textContent (never innerHTML) so untrusted app error text is
# inserted as TEXT, not markup.
_SHOW_ERROR_JS = (
    "function __bialShowError(root,msg){root.textContent='';"
    "var pre=document.createElement('pre');"
    "pre.style.cssText='color:#b91c1c;padding:16px;white-space:pre-wrap;';"
    "pre.textContent=msg;root.appendChild(pre);}"
)


def _neutralize_script(compiled: str) -> str:
    """Neutralize a literal `</script>` breakout in app code (Express
    `String(compiled).replace(/<\\/script/gi, '<\\\\/script')`). A function
    replacement so no regex-group escapes are processed."""
    return re.sub(r"</script", lambda _m: "<\\/script", compiled, flags=re.IGNORECASE)


# --- runner frame (opaque-origin, pre-compiled app) ----------------------------

_FRAME_HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/react@18/umd/react.development.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
<script src="https://unpkg.com/prop-types@15.8.1/prop-types.min.js"></script>
<script src="https://unpkg.com/recharts@2.15.4/umd/Recharts.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={theme:{extend:{colors:{primary:'#00818A',secondary:'#D9A036',tertiary:'#1A2B34'},fontFamily:{manrope:['Manrope','sans-serif']}}}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>body{margin:0;font-family:'Manrope',sans-serif;background:#fff;}</style>
</head>
<body>
<div id="root"></div>
<script>
"""

_FRAME_MID = r"""
</script>
<script>
(function(){
var {useState,useEffect,useRef,useMemo,useCallback,useReducer,useContext,Fragment}=React;
"""

_FRAME_TAIL = (
    r"""
window.__PreviewApp=(typeof PreviewApp!=="undefined")?PreviewApp:null;
})();
</script>
<script>
"""
    + _SHOW_ERROR_JS
    + r"""
var __bialRoot=null;
function mount(){
if(__bialRoot)return;
var root=document.getElementById('root');
if(!window.__PreviewApp){__bialShowError(root,'App did not define a PreviewApp component.');return;}
try{__bialRoot=ReactDOM.createRoot(root);__bialRoot.render(React.createElement(window.__PreviewApp));}
catch(e){__bialShowError(root,'App error:\n'+((e&&e.message)||e));}
}
window.addEventListener('message',function(e){
if(!e.data)return;
if(e.data.config)window.__BIAL_CONFIG=e.data.config;
if('accessToken' in e.data)window.__BIAL_TOKEN=e.data.accessToken||null;
if('user' in e.data)window.__BIAL_USER=e.data.user||null;
if(e.data.config)mount();
});
window.parent.postMessage({runnerReady:true},'*');
setTimeout(mount,1500);
</script>
</body>
</html>"""
)


def render_frame(compiled: str) -> str:
    """The opaque-origin frame HTML serving the pre-compiled app JS. The data client
    is inlined; the app JS runs inside the React-destructuring IIFE; globals arrive
    via postMessage before mount."""
    return (
        _FRAME_HEAD
        + bial_data_client_script()
        + _FRAME_MID
        + _neutralize_script(compiled)
        + _FRAME_TAIL
    )


# --- runner shell (same-origin host page) --------------------------------------

_SHELL_HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
body{margin:0;font-family:'Manrope',sans-serif;background:#f3f5f7;}
#login{display:none;align-items:center;justify-content:center;position:fixed;inset:0;}
#login .box{background:#fff;padding:32px;border-radius:12px;box-shadow:0 8px 30px rgba(26,43,52,.12);text-align:center;max-width:360px;}
#login h1{color:#1A2B34;font-size:20px;margin:0 0 8px;}
#login p{color:#b91c1c;min-height:18px;margin:8px 0;}
#login a{display:inline-block;margin-top:12px;background:#00818A;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;}
</style>
</head>
<body>
<div id="login"><div class="box"><h1>Sign in required</h1><p id="err"></p><a id="signin">Sign in with Microsoft</a></div></div>
<div id="appwrap"></div>
<script>
var CONFIG="""

_SHELL_TAIL = r""";
var accessToken=null, currentUser=null;
function showApp(){
var wrap=document.getElementById('appwrap');
var frame=document.createElement('iframe');
frame.setAttribute('sandbox','allow-scripts allow-forms allow-downloads');
frame.src=FRAME_SRC;
frame.title='App';
frame.style.cssText='position:fixed;inset:0;border:0;width:100vw;height:100vh;';
wrap.appendChild(frame);
window.__frame=frame;
}
function postToFrame(){
if(window.__frame&&window.__frame.contentWindow){
window.__frame.contentWindow.postMessage({config:CONFIG,accessToken:accessToken,user:currentUser},'*');
}
}
function showLogin(){
document.getElementById('signin').setAttribute('href',SIGNIN_URL);
document.getElementById('login').style.display='flex';
}
async function init(){
if(!CONFIG.loginRequired){accessToken=null;currentUser=null;showApp();return;}
try{
var res=await fetch(RUNNER_TOKEN_URL,{method:'POST',credentials:'same-origin'});
if(res.ok){var d=await res.json();accessToken=d.accessToken||null;currentUser=d.user||null;showApp();}
else{showLogin();}
}catch(e){showLogin();}
}
window.addEventListener('message',function(e){
if(e.data&&e.data.runnerReady)postToFrame();
});
init();
</script>
</body>
</html>"""


def render_shell(app_id: uuid.UUID, config: dict[str, Any]) -> str:
    """The same-origin shell: injects the config + short-lived token into the frame.

    `config` is server-controlled (appId/appKey/baseUrl/loginRequired), so
    `json.dumps` is safe to embed. The runner-token URL and sign-in URL are the
    shell's own same-origin paths."""
    frame_src = f"/apps/{app_id}/frame"
    runner_token_url = f"/apps/{app_id}/runner-token"
    return (
        _SHELL_HEAD
        + json.dumps(config)
        + ";\nvar FRAME_SRC="
        + json.dumps(frame_src)
        + ";\nvar RUNNER_TOKEN_URL="
        + json.dumps(runner_token_url)
        + ";\nvar SIGNIN_URL="
        + json.dumps(_SIGNIN_URL)
        + _SHELL_TAIL
    )


# --- builder preview (compiles JSX in-browser) ---------------------------------

PREVIEW_SHELL = (
    r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/react@18/umd/react.development.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
<script src="https://unpkg.com/prop-types@15.8.1/prop-types.min.js"></script>
<script src="https://unpkg.com/recharts@2.15.4/umd/Recharts.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={theme:{extend:{colors:{primary:'#00818A',secondary:'#D9A036',tertiary:'#1A2B34'},fontFamily:{manrope:['Manrope','sans-serif']}}}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>body{margin:0;font-family:'Manrope',sans-serif;background:#fff;}</style>
</head>
<body>
<div id="root"></div>
<script>
"""
    + bial_data_client_script()
    + r"""
</script>
<script>
"""
    + _SHOW_ERROR_JS
    + r"""
function renderPreview(code){
var root=document.getElementById('root');
try{
var cleaned=String(code)
.replace(/import\s+[^;]*?from\s*['"][^'"]+['"];?/g,'')
.replace(/import\s*['"][^'"]+['"];?/g,'')
.replace(/export\s+default\s+/g,'')
.replace(/export\s+/g,'');
var compiled=Babel.transform(cleaned,{presets:[['react',{runtime:'classic'}]]}).code;
var old=document.getElementById('__app');if(old)old.remove();
var s=document.createElement('script');s.id='__app';
s.textContent='(function(){var {useState,useEffect,useRef,useMemo,useCallback,useReducer,useContext,Fragment}=React;'+compiled+';window.__PreviewApp=(typeof PreviewApp!=="undefined")?PreviewApp:null;})();';
document.body.appendChild(s);
if(!window.__PreviewApp){__bialShowError(root,'App did not define a PreviewApp component.');return;}
ReactDOM.createRoot(root).render(React.createElement(window.__PreviewApp));
}catch(e){__bialShowError(root,'Preview error:\n'+((e&&e.message)||e));}
}
window.addEventListener('message',function(e){
if(!e.data)return;
if(e.data.config)window.__BIAL_CONFIG=e.data.config;
if('accessToken' in e.data)window.__BIAL_TOKEN=e.data.accessToken||null;
if('user' in e.data)window.__BIAL_USER=e.data.user||null;
if(typeof e.data.previewCode==='string')renderPreview(e.data.previewCode);
});
window.parent.postMessage({previewReady:true},'*');
</script>
</body>
</html>"""
)
