import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Users, BarChart3, Palette, Sparkles, LayoutGrid, ChevronDown, ShieldAlert, X, Paperclip, FileText, FileSpreadsheet, Presentation, MessageSquare, Hammer } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import ProjectPicker from '../components/projects/ProjectPicker'
import { validatePrompt } from '../utils/promptGuardrails'
import { usePendingAttachments } from '../hooks/usePendingAttachments'
import { ACCEPT_ATTR, TEXT_MEDIA_TYPES, OFFICE_MEDIA_TYPES, DECK_MEDIA_TYPES, officeFormat } from '../utils/attachmentInput'

const THEMES = [
  { id: 'bial', name: 'Bangalore Airport Theme', subtitle: 'Official BIAL brand colors and typography' },
  { id: 'mobile', name: 'App Style (iOS/Android)', subtitle: 'Clean mobile-first material design' },
  { id: 'dashboard', name: 'Dashboard / Analytics', subtitle: 'Data-dense layout with charts and metrics' },
  { id: 'kiosk', name: 'Kiosk / Public Display', subtitle: 'Large text, high contrast, touch-friendly' },
]

const EXAMPLES = [
  {
    icon: LayoutGrid,
    color: 'text-primary',
    bg: 'bg-primary/10',
    title: 'Resource Management',
    desc: '"Build a system to track gate equipment maintenance logs and schedules."',
    prompt: 'Build a system to track gate equipment maintenance logs and schedules. Include a calendar view for upcoming maintenance, a status dashboard showing equipment health across all gates, and alert notifications when equipment is overdue for service.',
  },
  {
    icon: Users,
    color: 'text-secondary',
    bg: 'bg-secondary/10',
    title: 'Staff Coordination',
    desc: '"An app for roster updates and emergency broadcast notifications for T1 teams."',
    prompt: 'Create an app for roster updates and emergency broadcast notifications for Terminal 1 teams. Include a shift calendar, one-tap emergency broadcast to all on-duty staff, and a message board for shift handover notes.',
  },
  {
    icon: BarChart3,
    color: 'text-primary',
    bg: 'bg-primary/10',
    title: 'Flight Metrics',
    desc: '"Visual dashboard for tracking turn-around times by airline partner."',
    prompt: 'Create a visual dashboard for tracking turn-around times by airline partner. Show a comparison chart of target vs actual times, drill-down by gate number, and highlight delays exceeding 15 minutes with automatic escalation flags.',
  },
]

function SelectDropdown({ icon: Icon, options, value, onChange, placeholder }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    const onOutside = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onOutside)
    document.addEventListener('keydown', onEsc)
    return () => { document.removeEventListener('mousedown', onOutside); document.removeEventListener('keydown', onEsc) }
  }, [])

  const selected = options.find((o) => o.id === value)

  return (
    <div className="relative flex-shrink-0" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-1.5 text-xs font-worksans font-medium border rounded-lg px-3 py-2 transition whitespace-nowrap ${
          value ? 'bg-primary/5 border-primary text-primary' : 'bg-white border-bial-border text-neutral hover:border-primary hover:text-primary'
        }`}
      >
        <Icon size={12} />
        <span className="max-w-[120px] truncate">{selected ? selected.name : placeholder}</span>
        <ChevronDown size={11} className={`transition-transform flex-shrink-0 ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1.5 w-64 bg-white rounded-xl border border-bial-border shadow-xl z-50 py-1 overflow-hidden">
          {options.map((opt) => (
            <button
              key={opt.id}
              onClick={() => { onChange(opt.id); setOpen(false) }}
              className={`w-full text-left px-4 py-2.5 hover:bg-primary/5 transition flex flex-col gap-0.5 ${value === opt.id ? 'bg-primary/5' : ''}`}
            >
              <span className={`text-xs font-bold ${value === opt.id ? 'text-primary' : 'text-tertiary'}`}>{opt.name}</span>
              <span className="text-[10px] text-neutral">{opt.subtitle}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function SandboxPage() {
  const navigate = useNavigate()
  const [prompt, setPrompt] = useState('')
  const [theme, setTheme] = useState('bial')
  const textareaRef = useRef(null)
  const fileInputRef = useRef(null)
  // Shared chat-attachment composer — same allowlist + validation as Plan/Builder
  // chat, so the Generate-App step accepts images, PDF, Word, Excel, and (flag on)
  // PowerPoint, not just spreadsheets. The picked files ride to the builder as
  // pending attachments and feed the FIRST generation turn via buildUserParts.
  const { pendingAttachments, handleFileSelect, removePending, attachToast } = usePendingAttachments()

  const [mode, setMode] = useState('build')
  const [guardRailModal, setGuardRailModal] = useState(null)
  const [sandboxToast, setSandboxToast] = useState(null)

  const showSandboxToast = (msg) => {
    setSandboxToast(msg)
    setTimeout(() => setSandboxToast(null), 3000)
  }

  // Both entry points need a project before they can open a chat: the server requires
  // `header.projectId` on the create branch, and there is no Default project. `picking`
  // records which flow the picker is gating so its callback knows where to go.
  const [picking, setPicking] = useState(null) // 'builder' | 'planning' | null

  const handleGenerate = () => {
    if (!prompt.trim()) return
    const guardResult = validatePrompt(prompt)
    if (guardResult) {
      setGuardRailModal(guardResult)
      return
    }
    setPicking('builder')
  }

  const handleChat = () => {
    if (!prompt.trim()) return
    setPicking('planning')
  }

  // Carry the picked files (in-memory base64) to the builder via router state; the
  // first generation turn uploads + sends them through buildUserParts. The chat id is
  // minted here and the project rides a transient query until the first append.
  const startChatInProject = (project) => {
    const kind = picking
    setPicking(null)
    const chatId = crypto.randomUUID()
    const state =
      kind === 'builder'
        ? { prompt, theme, pendingAttachments }
        : { initialMessage: prompt }
    navigate(`/chat/${chatId}?projectId=${encodeURIComponent(project.id)}&kind=${kind}`, { state })
  }

  const fillPrompt = (text) => {
    setPrompt(text)
    setTimeout(() => {
      textareaRef.current?.focus()
      textareaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 50)
  }

  return (
    <div className="min-h-screen bg-bial-bg font-manrope flex flex-col">
      <Navbar />

      <main className="flex-1 flex flex-col items-center px-6 py-14">
        <h1 className="text-4xl font-extrabold text-tertiary text-center mb-3">
          Build What You Need. No Code Required
        </h1>
        <p className="text-neutral text-center max-w-lg mb-10 leading-relaxed">
          Turn everyday operational ideas into working applications. Just describe what you need in plain English — the BLR Citizen Developer Suite handles the rest.
        </p>

        {/* Mode toggle */}
        <div className="w-full max-w-2xl mb-4 flex items-center gap-1 bg-white border border-bial-border rounded-xl p-1 shadow-sm">
          <button
            onClick={() => setMode('build')}
            className={`flex-1 flex items-center justify-center gap-2 text-sm font-bold rounded-lg px-4 py-2 transition ${
              mode === 'build'
                ? 'bg-secondary text-white shadow-sm'
                : 'text-neutral hover:text-tertiary'
            }`}
          >
            <Hammer size={14} />
            Build
          </button>
          <button
            onClick={() => setMode('chat')}
            className={`flex-1 flex items-center justify-center gap-2 text-sm font-bold rounded-lg px-4 py-2 transition ${
              mode === 'chat'
                ? 'bg-primary text-white shadow-sm'
                : 'text-neutral hover:text-tertiary'
            }`}
          >
            <MessageSquare size={14} />
            Plan with AI
          </button>
        </div>

        {mode === 'chat' && (
          <p className="text-xs text-neutral text-center max-w-md mb-6 -mt-2">
            Not sure what to build yet? Chat with the AI to plan your app first, then move to the builder when you're ready.
          </p>
        )}

        {/* Prompt card */}
        <div className="w-full max-w-2xl bg-white rounded-2xl border border-bial-border shadow-sm">
          <textarea
            ref={textareaRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && e.metaKey) {
                mode === 'chat' ? handleChat() : handleGenerate()
              }
            }}
            placeholder={
              mode === 'chat'
                ? "Describe what you're thinking… I'll help you plan it out."
                : "Describe the app you want to build... (e.g. 'Create a dashboard to track terminal 2 ground staff assignments with real-time delay alerts')"
            }
            rows={6}
            className="w-full p-5 text-sm text-tertiary placeholder:text-gray-300 resize-none focus:outline-none rounded-t-2xl font-manrope leading-relaxed"
          />

          {/* Controls row */}
          <div className="px-4 py-3 border-t border-bial-border space-y-2">
            {mode === 'build' && (
              <div className="flex flex-wrap items-center gap-2">
                <SelectDropdown
                  icon={Palette}
                  options={THEMES}
                  value={theme}
                  onChange={setTheme}
                  placeholder="Select Theme"
                />

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
                  onClick={handleGenerate}
                  disabled={!prompt.trim()}
                  className="ml-auto flex items-center gap-2 bg-secondary hover:bg-secondary-600 disabled:opacity-40 text-white font-bold text-sm px-5 py-2 rounded-xl transition shadow-sm shadow-secondary/30 flex-shrink-0"
                >
                  Generate App <Sparkles size={13} />
                </button>
              </div>
            )}

            {mode === 'chat' && (
              <div className="flex justify-end">
                <button
                  onClick={handleChat}
                  disabled={!prompt.trim()}
                  className="flex items-center gap-2 bg-primary hover:bg-primary-dark disabled:opacity-40 text-white font-bold text-sm px-5 py-2 rounded-xl transition shadow-sm shadow-primary/20 flex-shrink-0"
                >
                  Start Planning <MessageSquare size={13} />
                </button>
              </div>
            )}

            {mode === 'build' && pendingAttachments.length > 0 && (
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

        {/* Suggestion cards */}
        <div className="w-full max-w-2xl mt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          {EXAMPLES.map(({ icon: Icon, color, bg, title, desc, prompt: cardPrompt }) => (
            <button
              key={title}
              onClick={() => fillPrompt(cardPrompt)}
              className="bg-white rounded-xl border border-bial-border p-4 text-left hover:border-primary hover:shadow-md hover:-translate-y-0.5 transition cursor-pointer group"
            >
              <div className={`w-9 h-9 rounded-xl ${bg} flex items-center justify-center mb-3`}>
                <Icon size={17} className={color} />
              </div>
              <h3 className="text-sm font-bold text-tertiary mb-1 group-hover:text-primary transition">{title}</h3>
              <p className="text-xs text-neutral leading-relaxed">{desc}</p>
            </button>
          ))}
        </div>
      </main>

      <footer className="border-t border-bial-border bg-white py-4 px-6">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <p className="text-xs text-neutral">© 2026 Bangalore International Airport · Citizen Developer Suite</p>
          <div className="flex gap-5">
            <a href="/help" className="text-xs text-neutral hover:text-primary transition">Staff Support</a>
          </div>
        </div>
      </footer>

      {/* The project gate. There is no Default project, so neither entry point can open
          a chat until the user has named one. */}
      {picking && (
        <ProjectPicker
          title={picking === 'builder' ? 'Build this app in which project?' : 'Plan this app in which project?'}
          onClose={() => setPicking(null)}
          onPick={startChatInProject}
        />
      )}

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
                onClick={() => showSandboxToast('Reach out to citizen-developer-support@bialport.com')}
                className="text-sm font-semibold text-neutral border border-gray-200 px-4 py-2 rounded-xl hover:border-gray-300 transition"
              >
                Contact IT Support
              </button>
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

      {/* Sandbox toast (guardrail / save errors) + attachment validation toast */}
      {(attachToast || sandboxToast) && (
        <div className="fixed bottom-6 right-6 bg-tertiary text-white text-xs font-semibold px-4 py-3 rounded-xl shadow-xl z-50 max-w-xs">
          {attachToast || sandboxToast}
        </div>
      )}
    </div>
  )
}
