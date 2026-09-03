/**
 * START A CHAT — the rail's composer, its kind picker, and the mint-and-navigate protocol.
 *
 * ═══ WHY THIS FILE EXISTS, AND WHAT IT SETTLES ═══
 *
 * The protocol below — mint a v7 id, navigate to a flat chat address, carry the draft in router
 * state — lived in `ProjectBuilder.tsx`, which this plan deletes. Two plans each described it as
 * the other's: this plan's earlier draft said it "travels with Plan D's composer", and Plan D's
 * scope boundaries said the mint site is retired by this plan's U1. Each named the other, neither
 * had it in a Files list, and a protocol two plans both disown is a protocol nobody writes. It is
 * this plan's, and it is here.
 *
 * ═══ R15's PICKER, WHICH NO PLAN HAD BUILT ═══
 *
 * A citizen has to be able to choose a Plan chat or a Build chat BEFORE the first message, because
 * a chat's kind is fixed at creation and never mutates. Plan B owns the server half — creation
 * takes the kind and refuses to change it — and nothing owned the client half: `ProjectBuilder`
 * hardcoded `kind=build` into the minted address, and its own docstring called the picker for the
 * other kind "a picker nobody has designed yet". Without it this rail can only mint Build chats and
 * R11/R12 have nothing to distinguish. It belongs here because R6 already requires the kind chooser
 * beside the composer in the rail at rest.
 *
 * THE TWO OPTIONS' WORDS COME FROM ONE PLACE AND ARE NOT RE-WRITTEN HERE. `utils/chatKind.ts` reads
 * the kind catalogue off the bootstrap profile — the same catalogue the server's toolset registry
 * sits beside — so what a kind is CALLED and what it DOES have exactly one author. Typing "Plan" and
 * "Shape it first" into this file would create a second, and the two would drift the first time the
 * server's wording changed.
 */
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileSpreadsheet, FileText, Paperclip, ShieldAlert, Sparkles, X } from 'lucide-react'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { validatePrompt } from '../../utils/promptGuardrails'
import type { PromptViolation } from '../../utils/promptGuardrails'
import { uuidv7 } from '../../utils/conversationApi'
import { chatKindFor } from '../../utils/chatKind'
import { usePendingAttachments } from '../../hooks/usePendingAttachments'
import { ACCEPT_ATTR } from '../../utils/attachmentInput'
import type { ChatKind } from '../../pages/ChatRoute'

/**
 * The two kinds, in the order a citizen meets them. `build` is first and is the default, which is
 * the behaviour this rail inherits rather than a new decision: the retired composer minted a Build
 * chat for every send, so defaulting to Plan would silently change what the existing control does.
 */
const KINDS: readonly ChatKind[] = ['build', 'plan']

export interface RailComposerProps {
  /** The project every chat minted here is filed under. Required: it is what removes the picker
   *  gate the standalone builder had, where a chat had to be told which project it belonged to. */
  projectId: string
}

export default function RailComposer({ projectId }: RailComposerProps) {
  const navigate = useNavigate()
  const [prompt, setPrompt] = useState('')
  const [kind, setKind] = useState<ChatKind>('build')
  const [guardRailModal, setGuardRailModal] = useState<PromptViolation | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // The shared chat-attachment composer — the same allowlist and validation the conversation
  // surface uses, so this first step accepts everything a later message would.
  const { pendingAttachments, handleFileSelect, removePending, attachToast } = usePendingAttachments()

  /**
   * Mint a fresh chat and hand the draft to it. EVERY submit mints a NEW conversation — there is no
   * canonical thread per project; continuity lives in the app and its snapshots.
   */
  const startChat = () => {
    if (!prompt.trim()) return
    const violation = validatePrompt(prompt)
    if (violation) {
      setGuardRailModal(violation)
      return
    }
    // THROUGH THE SHARED `uuidv7`, never an inline `crypto.randomUUID()`. That mints a v4, and this
    // id becomes the conversation's PRIMARY KEY, which ADR-0006 wants sortable. The retired
    // composer's own comment records the failure this rule exists for: two sites each kept a
    // private copy of the one-liner and both went on minting v4 long after the shared mint moved
    // on. Do not make a third.
    //
    // THE KIND TRAVELS AS A QUERY PARAM, THE DRAFT AS ROUTER STATE, and the split is deliberate.
    // Router state dies on reload and never travels in a shared link, so a bookmarked `/chat/{id}`
    // must still be able to take its kind from somewhere — and the server is the authority once the
    // row exists. The draft is the opposite: it is this navigation's payload and has no business
    // surviving a reload or appearing in a URL.
    //
    // `freshlyMinted` tells `ChatRoute` the row does not exist yet, so it can skip a GET that can
    // only ever 404 — doubled by two hydration fetches and doubled again by StrictMode in dev.
    navigate(`/chat/${uuidv7()}?projectId=${encodeURIComponent(projectId)}&kind=${kind}`, {
      state: { prompt, pendingAttachments, freshlyMinted: true },
    })
  }

  const picked = chatKindFor(kind)

  return (
    // NO SECTION LABEL OF ITS OWN (plan 002, U3). "START A CHAT" is the RAIL's heading now, drawn
    // beside the other two in the board's own type, so this component is the control and not the
    // section around it. Two headings for one section is how the rail ended up with four cards.
    <div className="font-manrope">
      <div className="mt-2.5 w-full bg-white rounded-2xl border border-bial-border shadow-sm">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && e.metaKey) startChat()
          }}
          placeholder="Describe the app you want built... (e.g. 'Create a dashboard to track terminal 2 ground staff assignments with real-time delay alerts')"
          rows={4}
          className="w-full p-4 text-sm text-tertiary placeholder:text-gray-300 resize-none focus:outline-none rounded-t-2xl font-manrope leading-relaxed"
        />

        <div className="px-3 py-3 border-t border-bial-border space-y-2">
          {/* R15's picker. `type="single"` with no deselect: a chat is always one kind or the
              other, so an empty value is not a state this control may reach — Radix hands back
              `''` on a re-press of the active item, and ignoring that is what keeps the two
              options exhaustive rather than three-valued. */}
          <ToggleGroup
            type="single"
            value={kind}
            onValueChange={(next) => {
              if (next === 'build' || next === 'plan') setKind(next)
            }}
            variant="outline"
            size="sm"
            aria-label="What kind of chat"
            className="justify-start"
          >
            {KINDS.map((candidate) => {
              const look = chatKindFor(candidate)
              return (
                <ToggleGroupItem key={candidate} value={candidate} aria-label={look.word}>
                  {/* NO COLOUR OF ITS OWN. The board strokes the segment icon with the same
                      colour as the segment's label (#1A2B34 when selected, #6B7280 when not),
                      which `currentColor` gives for free. Tinting it separately is what put a
                      gold wrench on an orange ground at roughly 1.2:1. */}
                  <look.Icon size={13} />
                  {look.word}
                </ToggleGroupItem>
              )
            })}
          </ToggleGroup>
          {/* ONE LINE, FROM THE CATALOGUE. The empty string is the honest fallback for a bootstrap
              that has not resolved or a kind this build does not recognise; rendering an empty
              paragraph is better than inventing a description for it here. */}
          {picked.description && (
            <p data-testid="kind-description" className="text-[11px] text-neutral leading-snug">
              {picked.description}
            </p>
          )}

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
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className={`flex items-center gap-1.5 text-xs font-worksans font-medium border rounded-lg px-3 py-2 transition whitespace-nowrap flex-shrink-0 ${
                pendingAttachments.length > 0
                  ? 'bg-primary/5 border-primary text-primary'
                  : 'bg-white border-bial-border text-neutral hover:border-primary hover:text-primary'
              }`}
            >
              <Paperclip size={12} />
              {pendingAttachments.length > 0
                ? `${pendingAttachments.length} file${pendingAttachments.length > 1 ? 's' : ''}`
                : 'Upload File'}
            </button>
            <button
              type="button"
              onClick={startChat}
              disabled={!prompt.trim()}
              className="ml-auto flex items-center gap-2 bg-primary hover:bg-primary-600 disabled:opacity-40 text-white font-bold text-sm px-5 py-2 rounded-xl transition shadow-sm shadow-primary/30 flex-shrink-0"
            >
              Start Chat <Sparkles size={13} />
            </button>
          </div>

          {pendingAttachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {pendingAttachments.map((a) => (
                <span
                  key={a.id}
                  className="flex items-center gap-1 text-[10px] font-medium bg-primary/5 text-primary border border-primary/30 rounded-md px-2 py-1"
                >
                  {a.mediaType === 'text/csv' ? <FileSpreadsheet size={9} /> : <FileText size={9} />}
                  <span className="max-w-[160px] truncate">{a.name}</span>
                  <button
                    type="button"
                    onClick={() => removePending(a.id)}
                    aria-label={`Remove ${a.name}`}
                    className="ml-0.5 hover:text-danger transition"
                  >
                    <X size={9} />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {guardRailModal && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div role="dialog" aria-modal="true" aria-label="Prompt blocked" className="bg-white rounded-2xl shadow-2xl max-w-lg w-full p-6">
            <div className="flex items-start justify-between mb-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
                  <ShieldAlert size={20} className="text-red-500" />
                </div>
                <h2 className="text-base font-extrabold text-tertiary">Prompt Blocked</h2>
              </div>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setGuardRailModal(null)}
                className="text-neutral hover:text-tertiary"
              >
                <X size={16} />
              </button>
            </div>
            <p className="text-sm text-neutral leading-relaxed mb-4">{guardRailModal.message}</p>
            <div className="bg-red-50 border border-red-100 rounded-xl px-4 py-3 mb-6">
              <p className="text-xs font-semibold text-red-500 mb-2 uppercase tracking-wide">Flagged keywords</p>
              <div className="flex flex-wrap gap-2">
                {guardRailModal.flaggedKeywords.map((kw) => (
                  <span key={kw} className="text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded-full">
                    {kw}
                  </span>
                ))}
              </div>
            </div>
            <div className="flex gap-3 justify-end">
              <button
                type="button"
                onClick={() => setGuardRailModal(null)}
                className="text-sm font-bold bg-primary text-white px-5 py-2 rounded-xl hover:bg-primary/90 transition"
              >
                Edit My Prompt
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Attachment validation only. The guardrail modal's "Contact IT Support" was this
          component's other toast source until #157 B3 removed it: it announced a support address
          that was not a mailto, not clickable, not copyable, and wrong. */}
      {attachToast && (
        <div className="fixed bottom-6 right-6 bg-tertiary text-white text-xs font-semibold px-4 py-3 rounded-xl shadow-xl z-50 max-w-xs">
          {attachToast}
        </div>
      )}
    </div>
  )
}
