/**
 * BIALData — the documented data + login interface the GENERATED app code uses.
 * Injected into BOTH the builder preview and the deployed runner frame; both run in
 * an opaque-origin sandboxed iframe, so this client NEVER reads localStorage — it
 * reads the app config and the short-lived access token from values injected via
 * postMessage (window.__BIAL_CONFIG, window.__BIAL_TOKEN). The refresh token is
 * NEVER injected. Ported verbatim from the Express `bial-data-client.js`; the
 * bootstrap at the bottom is the `bialDataClientScript()` output.
 */
function createBIALData({ getConfig, getToken, setToken, fetchImpl, getUser }) {
  function recordsUrl(suffix) {
    const { baseUrl, appId } = getConfig()
    return baseUrl + '/apps/' + appId + '/records' + (suffix || '')
  }

  function filesUrl(suffix) {
    const { baseUrl, appId } = getConfig()
    return baseUrl + '/apps/' + appId + '/files' + (suffix || '')
  }

  function parseUrl(suffix) {
    const { baseUrl, appId } = getConfig()
    return baseUrl + '/apps/' + appId + '/parse' + (suffix || '')
  }

  function baseHeaders() {
    const { appKey } = getConfig()
    const headers = { 'X-App-Key': appKey }
    const token = getToken()
    if (token) headers['Authorization'] = 'Bearer ' + token
    return headers
  }

  // The host injects this app's identity (appId + appKey + baseUrl) via postMessage.
  // In the builder preview those land a beat AFTER the app first mounts (provisioning
  // is async), so a data call fired on mount can beat them. `ready()` gates that window
  // so we never fetch a broken `undefined/apps/undefined` URL.
  function ready() {
    const c = getConfig() || {}
    return Boolean(c.appId && c.appKey && c.baseUrl)
  }
  const NOT_READY = 'The data service is still starting up — please try again in a moment.'

  async function call(url, method, body) {
    if (!ready()) {
      // Config not injected yet: reads resolve empty (the app shows its empty state),
      // writes reject so a Save is never silently dropped. The preview re-renders with
      // real data the instant config arrives.
      if (method && method !== 'GET') throw new Error(NOT_READY)
      return null
    }
    const headers = baseHeaders()
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const res = await fetchImpl(url, {
      method: method,
      headers: headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (res.status === 401) {
      throw new Error('Please sign in to use this app.')
    }
    if (!res.ok) {
      let message = 'Request failed (' + res.status + ').'
      let code = null
      try {
        const err = await res.json()
        if (err && err.error && err.error.message) message = err.error.message
        if (err && err.error && err.error.code) code = err.error.code
      } catch (e) {
        // non-JSON error body — keep the generic message
      }
      const e = new Error(message)
      if (code) e.code = code
      throw e
    }
    if (res.status === 204) return null
    return res.json()
  }

  function save(collection, data) {
    return call(recordsUrl(), 'POST', { collection: collection, data: data })
  }

  async function list(collection, opts) {
    const params = []
    if (collection) params.push('collection=' + encodeURIComponent(collection))
    if (opts && opts.limit) params.push('limit=' + encodeURIComponent(opts.limit))
    const suffix = params.length ? '?' + params.join('&') : ''
    const out = await call(recordsUrl(suffix), 'GET')
    return (out && out.records) || []
  }

  async function query(collection, opts) {
    opts = opts || {}
    const params = []
    if (collection) params.push('collection=' + encodeURIComponent(collection))
    if (opts.q) params.push('q=' + encodeURIComponent(opts.q))
    if (opts.page) params.push('page=' + encodeURIComponent(opts.page))
    if (opts.pageSize) params.push('pageSize=' + encodeURIComponent(opts.pageSize))
    if (opts.sort) params.push('sort=' + encodeURIComponent(opts.sort))
    if (opts.order) params.push('order=' + encodeURIComponent(opts.order))
    if (opts.filter) params.push('filter=' + encodeURIComponent(JSON.stringify(opts.filter)))
    const suffix = '/search' + (params.length ? '?' + params.join('&') : '')
    const out = await call(recordsUrl(suffix), 'GET')
    return out || { items: [], total: 0, page: 1, pageSize: 25, totalPages: 0 }
  }

  async function distinct(collection, field) {
    const params = ['field=' + encodeURIComponent(field)]
    if (collection) params.unshift('collection=' + encodeURIComponent(collection))
    const out = await call(recordsUrl('/distinct?' + params.join('&')), 'GET')
    return (out && out.values) || []
  }

  async function get(collection, id) {
    const out = await call(recordsUrl('/' + encodeURIComponent(id)), 'GET')
    return (out && out.record) || null
  }

  async function update(collection, id, data) {
    const out = await call(recordsUrl('/' + encodeURIComponent(id)), 'PATCH', { data: data })
    return (out && out.record) || null
  }

  function remove(collection, id) {
    return call(recordsUrl('/' + encodeURIComponent(id)), 'DELETE')
  }

  async function seedFromUpload(collection, rows, opts) {
    opts = opts || {}
    if (!Array.isArray(rows) || rows.length === 0) return { seeded: 0, skipped: true }
    const existing = await list(collection, { limit: 500 })
    if (opts.dedupeKey) {
      const seen = {}
      for (let i = 0; i < existing.length; i++) {
        const v = existing[i].data ? existing[i].data[opts.dedupeKey] : undefined
        if (v !== undefined) seen[v] = true
      }
      const fresh = rows.filter(function (row) {
        return !seen[row[opts.dedupeKey]]
      })
      for (let i = 0; i < fresh.length; i++) await save(collection, fresh[i])
      return { seeded: fresh.length, skipped: false }
    }
    if (existing.length > 0) return { seeded: 0, skipped: true } // already seeded
    for (let i = 0; i < rows.length; i++) await save(collection, rows[i])
    return { seeded: rows.length, skipped: false }
  }

  // ── File storage ───────────────────────────────────────────────────────────
  function bytesToBase64(bytes) {
    var binary = ''
    var chunk = 0x8000
    for (var i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
    }
    return btoa(binary)
  }

  function fileHeaders() {
    return baseHeaders()
  }

  function triggerAnchorDownload(href, filename, revokeAfter) {
    const a = document.createElement('a')
    a.href = href
    a.download = filename || ''
    if (document.body) document.body.appendChild(a)
    a.click()
    if (document.body && a.parentNode) document.body.removeChild(a)
    if (revokeAfter && typeof URL !== 'undefined' && URL.revokeObjectURL) {
      setTimeout(function () {
        URL.revokeObjectURL(href)
      }, 0)
    }
  }

  async function uploadFile(fileOrObj, opts) {
    opts = opts || {}
    let filename, contentType, base64
    if (fileOrObj && typeof fileOrObj.arrayBuffer === 'function') {
      const buf = await fileOrObj.arrayBuffer()
      base64 = bytesToBase64(new Uint8Array(buf))
      filename = fileOrObj.name || 'upload'
      contentType = fileOrObj.type || 'application/octet-stream'
    } else if (fileOrObj && typeof fileOrObj === 'object') {
      filename = fileOrObj.filename
      contentType = fileOrObj.contentType
      base64 = fileOrObj.base64
    } else {
      throw new Error('uploadFile needs a File/Blob or { filename, contentType, base64 }.')
    }
    const body = { filename: filename, contentType: contentType, base64: base64 }
    if (opts.collection) body.collection = opts.collection
    return call(filesUrl(), 'POST', body)
  }

  async function listFiles(collection, opts) {
    const params = []
    if (collection) params.push('collection=' + encodeURIComponent(collection))
    if (opts && opts.limit) params.push('limit=' + encodeURIComponent(opts.limit))
    const suffix = params.length ? '?' + params.join('&') : ''
    const out = await call(filesUrl(suffix), 'GET')
    return (out && out.files) || []
  }

  async function getFile(fileId) {
    const out = await call(filesUrl('/' + encodeURIComponent(fileId)), 'GET')
    return (out && out.file) || null
  }

  function getDownloadUrl(fileId) {
    return call(filesUrl('/' + encodeURIComponent(fileId) + '/url'), 'GET')
  }

  async function downloadFile(fileId, filename) {
    let info = null
    try {
      info = await getDownloadUrl(fileId)
    } catch (e) {
      info = null // 501 / network → fall back to the /content proxy
    }
    const url = info && info.url
    if (typeof url === 'string' && url.indexOf('https://') === 0) {
      triggerAnchorDownload(url, filename, false)
      return { downloaded: true, via: 'sas' }
    }
    const objectUrl = await fileObjectUrl(fileId)
    triggerAnchorDownload(objectUrl, filename, true)
    return { downloaded: true, via: 'content' }
  }

  async function fileObjectUrl(fileId) {
    // This read fetches raw bytes directly (not via `call`), so it needs its own
    // pre-config guard — else it would hit a broken `undefined/apps/undefined` URL.
    if (!ready()) throw new Error(NOT_READY)
    const res = await fetchImpl(filesUrl('/' + encodeURIComponent(fileId) + '/content'), {
      method: 'GET',
      headers: fileHeaders(),
    })
    if (res.status === 401) throw new Error('Please sign in to use this app.')
    if (!res.ok) throw new Error('Could not load the file (' + res.status + ').')
    const blob = await res.blob()
    return URL.createObjectURL(blob)
  }

  function removeFile(fileId) {
    return call(filesUrl('/' + encodeURIComponent(fileId)), 'DELETE')
  }

  async function parseFile(input, opts) {
    opts = opts || {}
    const body = {}
    if (input && typeof input.arrayBuffer === 'function') {
      const buf = await input.arrayBuffer()
      body.base64 = bytesToBase64(new Uint8Array(buf))
      body.filename = input.name || 'upload'
      body.contentType = input.type || 'application/octet-stream'
    } else if (typeof input === 'string') {
      body.fileId = input
    } else if (input && typeof input === 'object') {
      if (input.fileId) body.fileId = input.fileId
      else {
        body.filename = input.filename
        body.contentType = input.contentType
        body.base64 = input.base64
      }
    } else {
      throw new Error('parseFile needs a File/Blob, a stored fileId string, or { fileId } / { filename, contentType, base64 }.')
    }
    if (opts.sheet) body.sheet = opts.sheet
    return call(parseUrl(), 'POST', body)
  }

  async function login(username, password) {
    const injected = typeof getUser === 'function' ? getUser() : null
    if (injected) {
      currentUserValue = injected
      return { user: injected }
    }
    let res
    try {
      const { baseUrl } = getConfig()
      res = await fetchImpl(baseUrl + '/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password }),
      })
    } catch (e) {
      throw new Error('Please sign in from the BIAL portal — this app does not handle sign-in itself.')
    }
    if (!res.ok) {
      throw new Error('Incorrect username or password.')
    }
    const data = await res.json()
    setToken(data.accessToken || null)
    currentUserValue = data.user || null
    return { user: currentUserValue }
  }

  var currentUserValue = null
  function currentUser() {
    if (currentUserValue) return currentUserValue
    const injected = typeof getUser === 'function' ? getUser() : null
    return injected || null
  }

  return {
    save: save,
    list: list,
    query: query,
    distinct: distinct,
    get: get,
    update: update,
    remove: remove,
    seedFromUpload: seedFromUpload,
    uploadFile: uploadFile,
    listFiles: listFiles,
    getFile: getFile,
    getDownloadUrl: getDownloadUrl,
    downloadFile: downloadFile,
    fileObjectUrl: fileObjectUrl,
    removeFile: removeFile,
    parseFile: parseFile,
    login: login,
    currentUser: currentUser,
  }
}
window.__BIAL_CONFIG = window.__BIAL_CONFIG || {};
window.__BIAL_TOKEN = window.__BIAL_TOKEN || null;
window.__BIAL_USER = window.__BIAL_USER || null;
window.BIALData = createBIALData({
  getConfig: function () { return window.__BIAL_CONFIG; },
  getToken: function () { return window.__BIAL_TOKEN; },
  setToken: function (t) { window.__BIAL_TOKEN = t; },
  getUser: function () { return window.__BIAL_USER; },
  fetchImpl: window.fetch.bind(window),
});
