/**
 * The project-scoped build composer — the Sandbox "Build What You Need" experience,
 * lifted out of the deleted standalone page so it can live INSIDE a project.
 *
 * The one structural change from Sandbox: there is no `ProjectPicker`. Sandbox had no
 * project, so it gated every handoff behind "pick a project first". Here the project is
 * a required prop, so `startChat` mints a chat in the selected mode and hands off directly —
 * mirroring the exact router-state shape `BuilderPage` / `ChatPage` already read
 * (`{ prompt, pendingAttachments }` for a build, `{ initialMessage }` for a plan).
 *
 * Rendered UNCONDITIONALLY by `ProjectPage` (D-fold): the composer is present whether or
 * not the project already has an app. There are no generic idea-starter cards here (F6) — a
 * dedicated project already has an established purpose; the mode helper copy + the mode-aware
 * placeholder are the first-run guidance.
 */
import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, ShieldAlert, X, Paperclip, FileText, FileSpreadsheet, Presentation } from 'lucide-react'
import { validatePrompt } from '../../utils/promptGuardrails'
import type { PromptViolation } from '../../utils/promptGuardrails'
import { uuidv7 } from '../../utils/conversationApi'
import { usePendingAttachments } from '../../hooks/usePendingAttachments'
import { ACCEPT_ATTR, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../../utils/attachmentInput'
import { ModeSwitcher } from '../chat/ModeSwitcher'
import type { ConversationMode } from '../../utils/turnStreamApi'

export interface ProjectBuilderProps {
  /** The project this composer builds/plans into. Required: it is what removes
   *  the ProjectPicker gate. */
  projectId: string
}

export default function ProjectBuilder({ projectId }: ProjectBuilderProps) {
  const navigate = useNavigate()
  const [prompt, setPrompt] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // Shared chat-attachment composer — same allowlist + validation as Plan/Builder
  // chat, so the Start Chat step accepts images, PDF, Word, Excel, and (flag on)
  // PowerPoint, not just spreadsheets. The picked files ride to the builder as
  // pending attachments and feed the FIRST generation turn via buildUserParts.
  const { pendingAttachments, handleFileSelect, removePending, attachToast } = usePendingAttachments()

  // U13: the Ask/Plan/Write toggle — DEFAULT PLAN. Every submit mints a NEW conversation
  // in the chosen mode (the canonical builder thread is retired; continuity lives in the
  // app + its snapshots, not in one blessed chat).
  const [mode, setMode] = useState<ConversationMode>('plan')
  const [guardRailModal, setGuardRailModal] = useState<PromptViolation | null>(null)
  /** Mint a fresh unified chat in the selected mode and hand the draft off to it. */
  const startChat = () => {
    if (!prompt.trim()) return
    const guardResult = validatePrompt(prompt)
    if (guardResult) {
      setGuardRailModal(guardResult)
      return
    }
    // Mint through the SHARED `uuidv7`, never an inline `crypto.randomUUID()` — that mints a v4,
    // and the id becomes the conversation's PRIMARY KEY (ADR-0006 wants v7). This site and
    // ChatPage's Launch-Builder each carried their own copy of the one-liner, which is precisely
    // why both kept minting v4 long after the store's mint moved on.
    //
    // `freshlyMinted` tells ChatRoute the row does not exist yet, so it can skip a GET that can
    // only 404. Router state — not a query param — because state dies on reload and never travels
    // in a shared link: a bookmarked `/chat/{id}?kind=…` must still take its kind from the server.
    navigate(
      `/chat/${uuidv7()}?projectId=${encodeURIComponent(projectId)}&kind=builder`,
      { state: { prompt, mode, pendingAttachments, freshlyMinted: true } },
    )
  }

  return (
    <div className="font-manrope">
      {/* The Ask / Plan / Write mode switch (U13/F5) — default Plan. Local DRAFT state:
          the chosen mode rides to the minted chat on submit; no server call here. */}
      <div className="mb-4">
        <ModeSwitcher value={mode} onSelect={setMode} composerRef={textareaRef} />
      </div>

      {mode === 'ask' && (
        <p className="text-xs text-neutral max-w-md mb-4">
          Ask questions about your app — the assistant reads its real code to answer.
        </p>
      )}
      {mode === 'plan' && (
        <p className="text-xs text-neutral max-w-md mb-4">
          Work out what to build together first — you confirm before anything is built.
        </p>
      )}
      {/* Write had NO helper line at all, because before U5 it was not a mode a user could
          usefully pick — it was where Build-it parked the thread. It is an ordinary mode
          now, so it needs the same one-line promise the other two make: this one builds
          straight away, which is exactly the thing a citizen should know before typing. */}
      {mode === 'write' && (
        <p className="text-xs text-neutral max-w-md mb-4">
          Describe a change and it gets built right away — no plan step. Changes stay in your
          workspace until you click Save to keep them.
        </p>
      )}

      {/* Prompt card */}
      <div className="w-full bg-white rounded-2xl border border-bial-border shadow-sm">
        <textarea
          ref={textareaRef}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && e.metaKey) startChat()
          }}
          placeholder={
            mode === 'ask'
              ? 'Ask anything about your app… (e.g. "What does the visitors form validate?")'
              : mode === 'plan'
                ? "Describe what you're thinking… we'll shape the plan together before building."
                : "Describe the app you want built... (e.g. 'Create a dashboard to track terminal 2 ground staff assignments with real-time delay alerts')"
          }
          rows={5}
          className="w-full p-5 text-sm text-tertiary placeholder:text-gray-300 resize-none focus:outline-none rounded-t-2xl font-manrope leading-relaxed"
        />

        {/* Controls row */}
        <div className="px-4 py-3 border-t border-bial-border space-y-2">
          {(
            <div className="flex flex-wrap items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                className="hidden"
                multiple
                accept={ACCEPT_ATTR}
                onChange={handleFileSelect}
              />

              <button
                onClick={() => fileInputRef.current?.click()}
                className={`flex items-center gap-1.5 text-xs font-worksans font-medium border rounded-lg px-3 py-2 transition whitespace-nowrap flex-shrink-0 ${
                  pendingAttachments.length > 0
                    ? 'bg-primary/5 border-primary text-primary'
                    : 'bg-white border-bial-border text-neutral hover:border-primary hover:text-primary'
                }`}
              >
                <Paperclip size={12} />
                {pendingAttachments.length > 0 ? `${pendingAttachments.length} file${pendingAttachments.length > 1 ? 's' : ''}` : 'Upload File'}
              </button>

              <button
                onClick={startChat}
                disabled={!prompt.trim()}
                className="ml-auto flex items-center gap-2 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white font-bold text-sm px-5 py-2 rounded-xl transition shadow-sm shadow-secondary/30 flex-shrink-0"
              >
                Start Chat <Sparkles size={13} />
              </button>
            </div>
          )}

          {pendingAttachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {pendingAttachments.map((a) => (
                <span key={a.id} className="flex items-center gap-1 text-[10px] font-medium bg-primary/5 text-primary border border-primary/30 rounded-md px-2 py-1">
                  {TEXT_MEDIA_TYPES.has(a.mediaType) ? (
                    a.mediaType === 'text/csv' ? <FileSpreadsheet size={9} /> : <FileText size={9} />
                  ) : OFFICE_MEDIA_TYPES.has(a.mediaType) ? (
                    officeFormat(a.mediaType) === 'excel' ? <FileSpreadsheet size={9} /> : <FileText size={9} />
                  ) : DECK_MEDIA_TYPES.has(a.mediaType) ? (
                    <Presentation size={9} />
                  ) : (
                    <FileText size={9} />
                  )}
                  <span className="max-w-[160px] truncate">{a.name}</span>
                  <button onClick={() => removePending(a.id)} className="ml-0.5 hover:text-danger transition">
                    <X size={9} />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* GuardRail Modal */}
      {guardRailModal && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl max-w-lg w-full p-6">
            <div className="flex items-start justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
                  <ShieldAlert size={20} className="text-red-500" />
                </div>
                <h2 className="text-base font-extrabold text-tertiary">Prompt Blocked</h2>
              </div>
              <button onClick={() => setGuardRailModal(null)} className="text-neutral hover:text-tertiary">
                <X size={16} />
              </button>
            </div>
            <p className="text-sm text-neutral leading-relaxed mb-4">{guardRailModal.message}</p>
            <div className="bg-red-50 border border-red-100 rounded-xl px-4 py-3 mb-6">
              <p className="text-xs font-semibold text-red-500 mb-2 uppercase tracking-wide">Flagged keywords</p>
              <div className="flex flex-wrap gap-2">
                {guardRailModal.flaggedKeywords.map((kw) => (
                  <span key={kw} className="text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded-full">{kw}</span>
                ))}
              </div>
            </div>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setGuardRailModal(null)}
                className="text-sm font-bold bg-primary text-white px-5 py-2 rounded-xl hover:bg-primary/90 transition"
              >
                Edit My Prompt
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast — attachment validation only. The guardrail modal's "Contact IT Support"
          was this component's other toast source until #157 B3 removed it: it announced a
          support address that was not a mailto, not clickable, not copyable, and wrong
          (bialport.com, not bialairport.com). */}
      {attachToast && (
        <div className="fixed bottom-6 right-6 bg-tertiary text-white text-xs font-semibold px-4 py-3 rounded-xl shadow-xl z-50 max-w-xs">
          {attachToast}
        </div>
      )}
    </div>
  )
}
