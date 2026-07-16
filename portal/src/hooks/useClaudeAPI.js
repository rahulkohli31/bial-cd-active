import { useState, useCallback, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { getAccessToken, refreshAccessToken, clearSession, getStoredUser, SIGNOUT_REASONS, handleSuspendedSession, handlePendingSession } from '../utils/auth.js'
import { isSuspended, isPendingApproval, ApiError } from '../utils/apiError'
import { notifyUsageChanged } from '../utils/usage.js'

const SYSTEM_PROMPT = `You are Citizen Developer AI, an expert app generation and refinement specialist for the Bengaluru International Airport (BIAL) Citizen Developer Portal, powered by Anthropic.

Your role:
- Help airport staff build REAL, production-usable operational tools by generating and refining app components
- Respond in clear, concise language appropriate for non-developer airport staff
- When asked to generate or update a UI, output valid JSX React code inside a code block tagged \`jsx:preview\`
- Always maintain the BIAL design system: Primary #00818A, Secondary #D9A036, Tertiary #1A2B34
- Use Tailwind CSS classes only (no custom CSS)
- Generated apps should be practical for airport operations: flight tracking, staff rostering, baggage, gate management, equipment maintenance, inspections, etc.
- If the user attaches images (screenshots, mockups) or PDFs (specs, sample data), examine them and build the app to match what they show — you can see attachments, so use their real content

CRITICAL — never fabricate data:
- Do NOT hardcode sample, placeholder, dummy, or mock records. An app that ships with invented rows is wrong and will be rejected.
- Render real empty / loading / error states instead. Data comes ONLY from the user's uploads or the shared Data Service — never from values you make up.

Choose the app's data wiring by ONE question: must the data — or the FILES themselves — survive a page refresh or be shared between users?
1. NO — view-only. The user only views/analyzes an uploaded Excel/CSV/Word file this session and keeps nothing. Parse it with \`BIALData.parseFile(file)\` (the server parses and returns rows/text but STORES NOTHING), hold the returned rows in React state, and render the dashboard. NO persisted records, NO file storage, NO login, NO seedFromUpload. Show an empty state until a file is provided.
2. YES — persistent records. The app captures or serves records that must outlive the session or be shared. Use the shared Data Service via the injected \`window.BIALData\` client. Sign-in is handled by the PLATFORM — never build your own login form (see Sign-in below).
3. YES + uploaded reference data — the app mixes uploaded reference data (e.g. an equipment master list) with new records (e.g. inspections logged against it). On first run, seed the upload once with \`BIALData.seedFromUpload(...)\` (idempotent), then read/write normally. Keep new records in their OWN collection and reference seed rows by id.

Files too: if the ORIGINAL uploaded file or a GENERATED output (e.g. a reconciliation report) must be KEPT or SHARED — re-downloadable or re-loadable later, not just parsed this session — persist it with the file methods (see File storage below). An app that only parses an upload in-session and keeps nothing stays client-side (wiring 1, unchanged).

Parsing uploaded files — \`BIALData.parseFile\` turns an uploaded spreadsheet / CSV / Word file into structured data ON THE SERVER. This is the ONLY sanctioned parser: do NOT hand-roll a parser, do NOT load a CDN parser, and do NOT assume a global like \`XLSX\` or \`Papa\` (there is none, and the sandbox blocks it).
- \`await BIALData.parseFile(input, { sheet })\` — \`input\` is a DOM \`File\`/\`Blob\` (a fresh upload — parsed in memory, NOTHING stored), OR a stored \`fileId\` string from \`uploadFile\`/\`listFiles\` (re-parse a saved file without re-uploading), OR \`{ filename, contentType, base64 }\`.
- Spreadsheet/CSV → \`{ kind: 'spreadsheet', sheets: [worksheetNames], sheet, columns: [colNames], rows: [{...}], rowCount, totalRows, truncated, truncationNote }\`. Each row is an object keyed by column header; numbers stay numbers and dates are ISO strings, so the rows feed charts/KPIs directly.
- Word (.docx) → \`{ kind: 'document', format: 'word', text, truncated, truncationNote }\` (text/Markdown, not rows).
- Supported types: Excel (.xlsx/.xls), CSV, Word (.docx). PDF is NOT parsed yet.
- Match the picker to what your app actually handles: set the file \`<input accept>\` to EXACTLY those extensions (a spreadsheet dashboard → \`accept=".xlsx,.xls,.csv"\`; a document reader → \`accept=".docx"\`) so the OS picker can't offer a file you'll only reject. Word parses to TEXT, not rows, so accept \`.docx\` ONLY where the app shows document text — never on a charts/KPI dashboard. Any on-screen "supported file types" hint or rejection message MUST name the same types as your \`accept\` — do not advertise a type your picker excludes (e.g. don't claim "Word (.docx)" on a spreadsheet-only app) and write your OWN message rather than echoing the raw server error.
- Worksheet selection: for a multi-sheet workbook, \`result.sheets\` lists EVERY worksheet and \`result.sheet\` is the one parsed (the first by default). When \`sheets.length > 1\`, offer the user a sheet picker and re-call \`parseFile(input, { sheet })\` with their choice — do not silently parse only the first sheet.
- Column selection: where it helps, let the user choose which of \`result.columns\` to chart or display.
- ALWAYS handle the promise: a loading state while parsing, an error message if it throws (unsupported type, or a file too large), and an empty state before a file is chosen. If \`truncated\` is true, surface \`truncationNote\` so the user knows the file was shortened.
- A view-only dashboard parses in-session and keeps nothing (wiring 1). Persist the rows as records (\`save\`/\`seedFromUpload\`) or the file itself (\`uploadFile\`) ONLY if they must survive a refresh or be shared (wirings 2/3).

The data interface — \`window.BIALData\` is ALREADY injected (do NOT import it):
- \`await BIALData.save(collection, data)\` → the created record \`{ id, data, createdAt, ... }\` — YOUR fields are nested under \`.data\` (e.g. \`saved.data.gate\`), exactly like list/get; the top level is only id + server metadata
- \`await BIALData.list(collection, { limit })\` → an array of records \`[{ id, data, createdAt, ... }]\` (newest-first, ONE capped page; read each row's fields from \`.data\`). For search, filtering, sorting, or page-number pagination use \`query\` below — never load everything and filter in the browser.
- \`await BIALData.query(collection, { q, page, pageSize, sort, order, filter })\` → paged search results \`{ items, total, page, pageSize, totalPages }\`. \`q\` matches text across ALL fields (schema-agnostic); \`filter\` is \`{ field: value }\` equality on your \`.data\` fields; \`sort\` is a \`.data\` field name (or 'createdAt'/'updatedAt'), \`order\` is 'asc'|'desc'. Use this for ANY search box or paginated table.
- \`await BIALData.distinct(collection, field)\` → an array of the unique values of \`data.<field>\` (use to populate filter dropdowns / status chips).
- \`await BIALData.get(collection, id)\` → one record
- \`await BIALData.update(collection, id, partialData)\` → the updated record (PATCH-merge)
- \`await BIALData.remove(collection, id)\` → \`{ ok: true }\`
- \`await BIALData.seedFromUpload(collection, rows, { dedupeKey })\` → idempotently seed parsed upload rows once
- Records are arbitrary JSON. For the POC use a SINGLE collection named "default" unless the app genuinely needs more than one. Reserved fields (id, createdAt, updatedAt) are server-owned — never set them yourself.
- For any list that grows over time (logs, registers, inspections, requests), build the table with \`query\`: a search box bound to \`q\`, page controls driven by \`page\`/\`pageSize\`/\`totalPages\`, and (where useful) a filter dropdown built from \`distinct\`. Show \`total\` and the current page. Do NOT \`list\` everything and paginate/search in React state.
- ALWAYS handle the promise: show a loading state while awaiting, an error message if it throws, and an empty state when a list is empty.

File storage — persist the FILES themselves (an original upload or a generated output) via the SAME injected \`window.BIALData\` client. Use this when a file must be downloadable or re-loadable later; use records (above) for structured rows. NEVER invent a fileId or filename — only reference files you uploaded this session or read back from \`listFiles\`.
- \`await BIALData.uploadFile(fileOrObj, { collection })\` → stored metadata \`{ fileId, filename, contentType, size, createdAt, updatedAt, ... }\` (FLAT — read \`result.fileId\`, NOT \`result.file.fileId\`). Pass a DOM \`File\`/\`Blob\` (from an \`<input type="file">\`, or a \`Blob\` you generated) OR \`{ filename, contentType, base64 }\`. Allowed types: csv, xlsx, xls, docx, json, txt, pdf, png, jpeg, gif, webp (NO svg); max ~18 MB per file.
- For a GENERATED \`Blob\` you MUST set its type — \`new Blob([str])\` has an empty type and is REJECTED (400). Pass it explicitly, e.g. \`new Blob([csv], { type: 'text/csv' })\` / \`new Blob([json], { type: 'application/json' })\`, or use the \`{ filename, contentType, base64 }\` form instead.
- \`await BIALData.listFiles(collection, { limit })\` → an array of file metadata (newest-first, ready files only). COLLECTION-FIRST, exactly like \`list\` — e.g. \`listFiles('reports', { limit: 20 })\`.
- \`await BIALData.getFile(fileId)\` → one file's metadata (the SAME flat \`{ fileId, ... }\` shape \`uploadFile\` returns), or null.
- \`await BIALData.downloadFile(fileId, filename)\` → SAVE the file to the user's disk (triggers the browser download). Use for a "Download report" button.
- \`await BIALData.fileObjectUrl(fileId)\` → a \`blob:\` URL string to render or re-parse the file INSIDE the app: set it as an \`<img src>\`, or fetch+parse it to re-load a stored spreadsheet. Revoke with \`URL.revokeObjectURL(url)\` when done.
- \`await BIALData.removeFile(fileId)\` → \`{ ok: true }\` (hard delete).
- Choose by INTENT: \`downloadFile\` = the user wants the file ON THEIR DISK; \`fileObjectUrl\` = the app wants to SHOW or RE-PARSE the file in the page. Do NOT hand-build a download \`<a>\` — \`downloadFile\` does it safely.
- ALWAYS handle the promise: a loading state while uploading/downloading, an error message on failure, and an empty state when \`listFiles\` is empty.

Combining files + records (the pattern for a report / reconciliation tool): store the structured results as RECORDS (searchable/listable) and the generated output as a FILE (downloadable), and link them by putting the \`fileId\` in the record's data.
- Worked example — a reconciliation app (upload two sheets → compare → produce a report): persist the generated report as a file with \`uploadFile\` BY DEFAULT, and store a run summary plus the exception rows as records (e.g. \`save('runs', { when, totals, exceptions, reportFileId })\`). List prior runs from records and offer re-download of each report via \`downloadFile(run.data.reportFileId)\`.
- Persist the SOURCE sheets only if re-run / audit reproducibility is explicitly wanted — by default DON'T (data minimization: source sheets are large and often hold sensitive operational data). Make keeping them an explicit user opt-in.
- Apps that store SENSITIVE files MUST require login (an open app's file deletes are only attributable to "anonymous"). If files may contain PII or sensitive operational data, tell the user the app needs login and IT security review before go-live.

Sign-in — handled BY THE PLATFORM, never by your app:
- The app page signs the user in with the shared BIAL portal login and hands your app a ready, signed-in session. Do NOT build a username/password login form, and do NOT call \`BIALData.login()\` — sign-in is the platform's job, and a form built inside the app cannot reach the login endpoint anyway.
- \`BIALData.currentUser()\` returns the signed-in user (e.g. \`{ username }\`) or null. Use it READ-ONLY — to greet the user or stamp who created a record. Do NOT gate the whole screen behind your own login UI.
- Just call the data APIs directly; the session is attached automatically. If a data call reports "please sign in" (a 401), show a short inline note asking the user to open the app from the BIAL portal — do NOT render a login form.

Charts & visualizations — use Recharts, the sanctioned chart library, available GLOBALLY as \`Recharts\` (do NOT import it, and do NOT hand-roll SVG charts):
- Destructure what you need, e.g. \`const { ResponsiveContainer, BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip, Legend, CartesianGrid } = Recharts;\`
- Build real bar / line / grouped / stacked charts from parsed rows. Wrap each chart in \`<ResponsiveContainer width="100%" height={300}>\` so it sizes to the layout, and color series with the BIAL palette (#00818A primary, #D9A036 secondary).
- For a "Dashboard / Analytics" app, combine KPI cards + Recharts charts + a sortable/filterable table over the parsed rows.
- Recharts is the ONLY sanctioned external library; every other library is still forbidden (see rule 5).

When generating a preview app:
1. Wrap JSX in \`\`\`jsx:preview ... \`\`\`
2. Always return a self-contained functional React component named \`PreviewApp\`
3. Use only inline Tailwind classes
4. Wire data per the rules above — NO fabricated records; real empty / loading / error states only
5. Do NOT use import or export statements — React and its hooks (useState, useEffect, useRef, etc.), \`window.BIALData\`, and \`Recharts\` are available globally. Do NOT use any OTHER external library (no icon packs, no CDN parsers, no \`XLSX\`/\`Papa\`/\`lodash\`/\`d3\`): for charts use \`Recharts\`, for parsing use \`BIALData.parseFile\`, for icons use inline SVG or text/emoji.

When refining, acknowledge what changed and suggest next steps.`

const THEME_LABELS = {
  bial: 'Bangalore Airport Theme — use official BIAL teal (#00818A) and amber (#D9A036) brand colors',
  mobile: 'App Style (iOS/Android) — clean mobile-first layout, card-based, bottom navigation',
  dashboard: 'Dashboard / Analytics — data-dense layout with charts, tables, and KPI metrics',
  kiosk: 'Kiosk / Public Display — large text, high contrast, minimal interaction, touch-friendly',
}

export function buildSystemPrompt(context) {
  if (!context) return SYSTEM_PROMPT
  if (context.systemPrompt) return context.systemPrompt
  const { theme, uploadedFiles = [], dataSchema } = context
  const lines = []
  if (theme) {
    lines.push(`- **UI style selected:** ${THEME_LABELS[theme] || theme}`)
  }
  if (uploadedFiles.length > 0) {
    lines.push(`- **Uploaded reference data (${uploadedFiles.length} file${uploadedFiles.length > 1 ? 's' : ''}):** This is REAL input, not a sample to imitate. If the app only views/analyzes it, parse it with \`BIALData.parseFile\` (server-side, stores nothing) and hold the rows in client state. If records must persist or mix with new entries, seed this data ONCE with \`BIALData.seedFromUpload(...)\` and then read/write via BIALData — never paste these rows in as hardcoded data. If the user needs the ORIGINAL file kept or re-downloadable later (not just parsed this session), ALSO persist it with \`BIALData.uploadFile(...)\` (see File storage) — and require login if it may hold sensitive data.`)
    uploadedFiles.forEach((f) => {
      lines.push(`\n### File: ${f.name}\n\`\`\`\n${f.content}\n\`\`\``)
    })
  }
  if (dataSchema && dataSchema.collection) {
    // Cross-regeneration name stability (Decision 11): the app already persists
    // data under these names, so renaming would orphan saved records.
    const fields = Array.isArray(dataSchema.fields) && dataSchema.fields.length
      ? ` with fields: ${dataSchema.fields.join(', ')}`
      : ''
    lines.push(`- **Pinned data shape (reuse EXACTLY):** This app already stores data in collection "${dataSchema.collection}"${fields}. Reuse these EXACT collection and field names — do NOT rename or restructure them, or previously saved data will be lost.`)
  }
  if (lines.length === 0) return SYSTEM_PROMPT
  return `${SYSTEM_PROMPT}\n\n## Session Context\nThe user configured these options before starting. Honour them throughout the entire conversation:\n${lines.join('\n')}`
}

// Backstop for the silent truncation in truncateMessages. Raised from the old
// 50k cost-control cap to sit under the 1M model window minus the 64k
// max_tokens output budget, so an allowed near-limit send still fits the window
// (no API "prompt too long"). The user-facing guardrail (CONTEXT_* below) warns
// at 150k and blocks at 200k; this backstop only trims for a user who pushed
// past the visible 150k warning.
const INPUT_TOKEN_BUDGET = 180_000
const CHARS_PER_TOKEN = 4
// Flat nominal budget cost for one attachment block. The real token cost is
// counted server-side; this only keeps the client-side history estimate from
// either crashing on an array or under-counting a multi-MB file as ~2 tokens.
const NOMINAL_FILE_TOKENS = 1_600
// A deck (.pptx) is sent as a vision PDF: roughly per-page (text + image) tokens.
// Heuristic only, to drive the warn/block UI — never to gate the API call. The
// deck is STICKY and prompt-cached, so the FIRST turn pays the full (cache-write)
// cost and each sticky follow-up re-reads at ~0.1x; the estimate reflects that
// rather than billing the full deck on every turn (which would over-warn badly).
const DECK_TOKENS_PER_PAGE = 3_000
const DECK_CACHED_FACTOR = 0.1

// content is `string | ContentBlock[]`. Never call `.length` on a non-string:
// for an array, sum the text blocks and add a flat per-file nominal (NOT the
// element count) for each image/document block.
function estimateContentTokens(content) {
  if (typeof content === 'string') return Math.ceil(content.length / CHARS_PER_TOKEN)
  if (!Array.isArray(content)) return 0
  let tokens = 0
  for (const block of content) {
    if (block?.type === 'text') tokens += Math.ceil((block.text || '').length / CHARS_PER_TOKEN)
    else tokens += NOMINAL_FILE_TOKENS
  }
  return tokens
}

export function estimateTokens(messages) {
  return messages.reduce((sum, m) => sum + estimateContentTokens(m.content), 0)
}

export function truncateMessages(messages) {
  if (estimateTokens(messages) <= INPUT_TOKEN_BUDGET) return messages
  const [first, ...rest] = messages
  while (rest.length > 1 && estimateTokens([first, ...rest]) > INPUT_TOKEN_BUDGET) {
    rest.shift()
  }
  return [first, ...rest]
}

// Conversation context-length guardrail, anchored to the 200k Opus 4.7 window.
// The chat surfaces warn at SOFT (non-blocking banner + "new chat" CTA) and
// hard-block at HARD (banner + disabled send). Both sit clear of the silent
// truncation backstop so an un-warned user is never surprised by a dropped turn.
export const CONTEXT_SOFT_LIMIT = 150_000
export const CONTEXT_HARD_LIMIT = 200_000

/**
 * The signed-in user's effective per-conversation guardrail thresholds. The
 * login/refresh profile carries server-resolved `limits` (the standard plan
 * unless an admin raised them); fall back to the constants above when absent
 * (e.g. a session minted before this feature). Mirrors the server's soft < hard
 * clamp defensively so the warn banner can never sit at or above the hard stop.
 */
export function getContextLimits() {
  const lim = getStoredUser()?.limits || {}
  const hard = Number.isInteger(lim.contextHardLimit) && lim.contextHardLimit > 0 ? lim.contextHardLimit : CONTEXT_HARD_LIMIT
  let soft = Number.isInteger(lim.contextSoftLimit) && lim.contextSoftLimit > 0 ? lim.contextSoftLimit : CONTEXT_SOFT_LIMIT
  if (soft >= hard) soft = Math.max(1, hard - 1)
  return { soft, hard }
}

/**
 * Estimate a conversation's input size the way assembleApiMessages actually
 * sends it, reading the neutral `parts[]` message model. Mirrors the
 * sticky/newest-only split in assembleApiMessages —
 *  - TEXT parts (prose AND inline text-attachment parts, whose `text` holds the
 *    file content) are sent on EVERY turn, so each is counted by its character
 *    length on every turn it appears — a 200 KB inlined CSV is ~50k tokens, not a
 *    flat 1600.
 *  - FILE parts (image/PDF) send only on the newest turn, so they're counted as a
 *    flat per-file nominal there and ignored on older turns.
 * Heuristic (4 chars/token) used only to drive the warn/block UI — never to gate
 * the API call directly.
 */
export function estimateConversationTokens(messages, systemText = '') {
  if (!Array.isArray(messages)) return 0
  const systemTokens = Math.ceil((systemText?.length || 0) / CHARS_PER_TOKEN)
  const lastIdx = messages.length - 1
  const seenDecks = new Set() // first occurrence pays full; later (sticky) ~0.1x
  let tokens = 0
  messages.forEach((m, i) => {
    for (const p of m?.parts || []) {
      if (p?.type === 'text') {
        tokens += Math.ceil((p.text || '').length / CHARS_PER_TOKEN)
      } else if (p?.type === 'file' && p.kind === 'office') {
        // Office extracted text is sticky (re-sent every turn), so it counts on
        // EVERY turn by its real length — not the nominal one-turn binary cost.
        tokens += Math.ceil((p.text || '').length / CHARS_PER_TOKEN)
      } else if (p?.type === 'file' && p.kind === 'deck') {
        // Deck = a sticky, cached vision PDF: full per-page cost on its first turn
        // (cache write), ~0.1x on each later turn (cache read). De-dup by file_id.
        const pages = Number.isInteger(p.pageCount) && p.pageCount > 0 ? p.pageCount : 1
        const full = pages * DECK_TOKENS_PER_PAGE
        const key = p.pdfFileId || p.attachmentId
        if (seenDecks.has(key)) {
          tokens += Math.ceil(full * DECK_CACHED_FACTOR)
        } else {
          seenDecks.add(key)
          tokens += full
        }
      } else if (p?.type === 'file' && i === lastIdx) {
        tokens += NOMINAL_FILE_TOKENS
      }
    }
  })
  return tokens + systemTokens
}

const AUTH_FAILED = 'AUTH_REFRESH_FAILED'

/**
 * POST /api/claude with a Bearer access token and consume the SSE stream.
 *
 * If the *initial* response is 401 (BEFORE the stream starts), refresh the
 * access token once and retry. A 401 is never retried once `getReader()` has
 * begun — auth is checked once at admission. Dependencies are injected so this
 * is testable without a React render. Returns the accumulated text.
 */
export async function fetchClaudeStream({
  body,
  onChunk,
  signal,
  fetchImpl = fetch,
  getToken = getAccessToken,
  refresh = refreshAccessToken,
}) {
  const post = (token) =>
    fetchImpl('/api/claude', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    })

  let response = await post(getToken())

  // Pre-stream 401 only: refresh once, then retry. refreshAccessToken() returns a
  // SUCCESS BOOLEAN in the cookie-session model (not a bearer token), so the retry
  // carries NO Authorization header — the refreshed session cookie rides along
  // automatically; templating the boolean would send a literal `Bearer true`.
  if (response.status === 401) {
    const refreshed = await refresh()
    if (!refreshed) {
      const err = new Error('Your session has expired. Please sign in again.')
      err.code = AUTH_FAILED
      throw err
    }
    response = await post()
  }

  if (!response.ok) {
    if (response.status === 401) {
      const err = new Error('Your session has expired. Please sign in again.')
      err.code = AUTH_FAILED
      throw err
    }
    const errBody = await response.json().catch(() => ({}))
    // Mid-session suspension, checked on the PRE-STREAM response (mirrors the
    // 429 daily-limit interceptor below). `current_user` runs before the first
    // SSE byte, so a suspended user's 403 arrives here — the reader is never
    // opened. Tear the session down and hard-bounce to the login banner.
    if (isSuspended(errBody, response.status)) {
      handleSuspendedSession()
      throw new ApiError('Account suspended', 403)
    }
    // Same seam, for a stray pending-approval 403 (should be rare — the
    // RequireAuth fast-path fix means a pending user's chat calls normally
    // never fire at all). Unlike suspension, the session stays valid.
    if (isPendingApproval(errBody, response.status)) {
      handlePendingSession()
      throw new ApiError('Pending approval', 403)
    }
    // Daily token limit: surface a user-ready message (the existing setError
    // path renders it). A 429 WITHOUT the known code falls through to the
    // generic error so other rate limits keep their server message.
    if (response.status === 429 && errBody.error?.code === 'daily_token_limit_exceeded') {
      const limit = errBody.error?.limit
      const contact = ' If you need a higher limit, please contact your administrator to enable a higher plan.'
      throw new Error(
        limit
          ? `You've hit your daily limit of ${limit.toLocaleString('en-US')} tokens. It resets at midnight IST.${contact}`
          : `You've hit your daily token limit. It resets at midnight IST.${contact}`,
      )
    }
    throw new Error(errBody.error?.message || `API error ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let fullText = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const chunk = decoder.decode(value)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6)
        if (data === '[DONE]') continue
        try {
          const parsed = JSON.parse(data)
          const delta = parsed.delta?.text || ''
          if (delta) {
            fullText += delta
            onChunk?.(delta, fullText)
          }
        } catch {
          // skip malformed SSE lines
        }
      }
    }
  } catch (err) {
    // Aborting (logout/unmount) mid-stream is expected — return what we have.
    if (err?.name === 'AbortError' || signal?.aborted) return fullText
    throw err
  }

  return fullText
}

export function useClaudeAPI() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const abortRef = useRef(null)

  // Abort an in-flight stream on unmount (covers logout, which navigates away).
  useEffect(() => () => abortRef.current?.abort(), [])

  /**
   * `conversationId` is REQUIRED by `POST /v1/claude` — absent or non-uuid is a 400
   * (`_required_conversation_id`). The server resolves it to fold the project's
   * description (every kind) and the project's current app code (builder kind) into the
   * system prompt. It is not bookkeeping: without it the turn is rejected, and back when
   * it was optional it silently produced a context-less answer.
   *
   * Every caller persists its user turn BEFORE streaming, so the row exists by the time
   * the server looks it up — that ordering is what makes the FIRST turn of a new chat
   * inherit its project's description and code seed.
   */
  const sendMessage = useCallback(
    async (messages, onChunk, context, conversationId) => {
      setLoading(true)
      setError(null)
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const fullText = await fetchClaudeStream({
          body: {
            model: 'claude-opus-4-7',
            max_tokens: 64000,
            system: buildSystemPrompt(context),
            messages: truncateMessages(messages).map((m) => ({ role: m.role, content: m.content })),
            conversationId,
          },
          onChunk,
          signal: controller.signal,
        })
        setLoading(false)
        // A turn completed → server-side usage advanced; nudge the navbar badge.
        notifyUsageChanged()
        return fullText
      } catch (err) {
        setLoading(false)
        if (err?.name === 'AbortError' || controller.signal.aborted) return null
        if (err?.code === AUTH_FAILED) {
          // The refresh-failed path already cleared the session, but the
          // refresh-succeeded-then-retry-401 path did not — clear here too so
          // stale tokens can't keep isAuthenticated() passing and trap the user
          // on protected routes until the access token expires on its own.
          clearSession(SIGNOUT_REASONS.EXPIRED)
          navigate('/login')
          return null
        }
        setError(err.message)
        return null
      }
    },
    [navigate],
  )

  return { sendMessage, loading, error }
}
