import { useState, useEffect } from 'react'
import { Zap, Sparkles, Lightbulb, MessageSquare, Wrench } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import Navbar from '../components/layout/Navbar'
import ProjectPicker from '../components/projects/ProjectPicker'
import { loadHistory, relativeTime } from '../utils/chatHistory'
import { loadBuilds } from '../utils/builderHistory'

const VIBE_STEPS = [
  { n: 1, text: 'Type a prompt describing the tool you want and the data it should work with.' },
  { n: 2, text: 'Review the generated app and refine the UI components visually.' },
  { n: 3, text: 'Your work is saved automatically — find it under Recent Conversations to continue anytime.' },
]

// Merge the logged-in user's recent chats + builds into one timeline, tagging
// each with kind so the card can route + badge correctly, newest first. The two
// stores are now server-backed (async), so fetch them in parallel.
async function loadRecents() {
  const [chats, builds] = await Promise.all([loadHistory(), loadBuilds()])
  const items = [
    ...chats.map((c) => ({ ...c, kind: 'chat' })),
    ...builds.map((b) => ({ ...b, kind: 'build' })),
  ]
  return items.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt))
}

function RecentCard({ item, onClick }) {
  const isChat = item.kind === 'chat'
  return (
    <div
      onClick={onClick}
      className="bg-white rounded-xl border border-gray-100 shadow-sm p-5 flex flex-col gap-3 cursor-pointer hover:shadow-md hover:-translate-y-0.5 transition"
    >
      <div className="flex items-start justify-between">
        <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${isChat ? 'bg-primary/10 text-primary' : 'bg-secondary/10 text-secondary'}`}>
          {isChat ? <MessageSquare size={16} /> : <Wrench size={16} />}
        </div>
        <span className={`flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full ${isChat ? 'bg-primary/10 text-primary' : 'bg-secondary/10 text-secondary'}`}>
          {isChat ? 'Chat' : 'Build'}
        </span>
      </div>
      <div>
        <h3 className="text-sm font-bold text-tertiary mb-1 truncate">{item.title}</h3>
      </div>
      <div className="flex items-center justify-end pt-2 border-t border-gray-50">
        <span className="text-[10px] text-neutral">{relativeTime(item.updatedAt)}</span>
      </div>
    </div>
  )
}

export default function Workspace() {
  const navigate = useNavigate()
  const [recents, setRecents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  // "Plan with AI" used to navigate to the literal `/workspace/chat/new`. Under flat
  // routing that resolves to a chat whose id is the word "new" — a 404 with no
  // ?projectId=, which bounces the user to /projects. A chat needs a project first.
  const [picking, setPicking] = useState(false)

  const startPlanChatInProject = (project) => {
    setPicking(false)
    navigate(`/chat/${crypto.randomUUID()}?projectId=${encodeURIComponent(project.id)}&kind=planning`)
  }

  // Fetch the per-user timeline on mount and (debounced) whenever the tab regains
  // focus, so returning from a chat/build shows the freshly-updated list without
  // firing a request on every rapid refocus.
  useEffect(() => {
    let active = true
    let timer
    const refresh = async () => {
      try {
        const items = await loadRecents()
        if (active) {
          setRecents(items)
          setError(false)
        }
      } catch {
        if (active) setError(true)
      } finally {
        if (active) setLoading(false)
      }
    }
    refresh()
    const onFocus = () => {
      clearTimeout(timer)
      timer = setTimeout(refresh, 400)
    }
    window.addEventListener('focus', onFocus)
    return () => {
      active = false
      clearTimeout(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [])

  return (
    <div className="min-h-screen bg-surface-muted font-manrope flex flex-col">
      <Navbar />

      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-10">
        {/* Header */}
        <div className="flex items-start justify-between mb-8">
          <div>
            <h1 className="text-3xl font-extrabold text-tertiary mb-1">App Builder</h1>
            <p className="text-neutral text-sm max-w-xl leading-relaxed">
              Talk through your requirements with AI first, then build — or jump straight into the build sandbox.
            </p>
          </div>
        </div>

        {/* Hero section — two-column layout */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mb-10">
          {/* Hero CTA card */}
          <div className="lg:col-span-2 bg-primary rounded-2xl p-10 flex flex-col items-center justify-center text-center shadow-xl shadow-primary/20 relative overflow-hidden min-h-72">
            {/* Decorative rings */}
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <div className="w-64 h-64 rounded-full border border-white/5" />
            </div>
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <div className="w-96 h-96 rounded-full border border-white/5" />
            </div>

            <div className="relative z-10 flex flex-col items-center gap-4">
              <div className="w-14 h-14 rounded-full bg-accent flex items-center justify-center shadow-lg">
                <Zap size={24} className="text-white" />
              </div>
              <h2 className="text-2xl font-extrabold text-white leading-tight max-w-sm">
                What operational tool do you want to build today?
              </h2>
              <p className="text-white/70 text-sm max-w-sm leading-relaxed">
                Describe your idea in plain English, then choose how to start.
              </p>
              <div className="flex flex-wrap items-center justify-center gap-3 mt-2">
                <button
                  onClick={() => setPicking(true)}
                  className="flex items-center gap-2 bg-white/15 hover:bg-white/25 text-white font-bold px-6 py-3 rounded-xl transition shadow-md"
                >
                  <MessageSquare size={15} /> Plan with AI
                </button>
                <button
                  onClick={() => navigate('/workspace/sandbox')}
                  className="flex items-center gap-2 bg-accent hover:bg-yellow-500 text-white font-bold px-6 py-3 rounded-xl transition shadow-md"
                >
                  Build an App <Sparkles size={15} />
                </button>
              </div>
              <p className="text-white/70 text-xs max-w-sm leading-relaxed mt-1">
                <strong className="font-semibold text-white">Plan with AI</strong> scopes your requirements in a guided chat — no code yet. <strong className="font-semibold text-white">Build an App</strong> jumps straight to a working draft.
              </p>
              <div className="flex items-center gap-2 bg-white/10 text-white/80 text-xs px-4 py-2 rounded-full mt-1">
                <Lightbulb size={12} />
                Try: "Build a baggage delay tracker for Gate B12"
              </div>
            </div>
          </div>

          {/* Right sidebar */}
          <div className="flex flex-col gap-4">
            {/* Vibe Coding 101 */}
            <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-5 flex-1">
              <p className="text-xs font-worksans font-semibold text-neutral uppercase tracking-wider mb-4">
                Vibe Coding 101
              </p>
              <div className="space-y-3">
                {VIBE_STEPS.map(({ n, text }) => (
                  <div key={n} className="flex gap-3 items-start">
                    <span className="w-5 h-5 rounded-full bg-primary text-white text-[10px] font-bold flex items-center justify-center flex-shrink-0 mt-0.5">
                      {n}
                    </span>
                    <p className="text-xs text-neutral leading-relaxed">{text}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Recent conversations */}
        <div>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold text-tertiary">Recent Conversations</h2>
            {recents.length > 0 && (
              <button
                onClick={() => navigate('/projects')}
                className="text-xs text-primary font-semibold hover:underline"
              >
                View all
              </button>
            )}
          </div>

          {loading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {[0, 1, 2].map((i) => (
                <div key={i} className="bg-white rounded-xl border border-gray-100 shadow-sm p-5 h-32 animate-pulse">
                  <div className="w-9 h-9 rounded-lg bg-gray-100 mb-3" />
                  <div className="h-3 bg-gray-100 rounded w-3/4 mb-2" />
                  <div className="h-2 bg-gray-50 rounded w-1/3" />
                </div>
              ))}
            </div>
          ) : error ? (
            <div className="bg-white rounded-xl border border-danger/20 p-10 flex flex-col items-center justify-center text-center">
              <p className="text-sm font-bold text-tertiary mb-1">Couldn't load your recent work</p>
              <p className="text-xs text-neutral mb-3">Check your connection and try again.</p>
              <button onClick={() => window.location.reload()} className="text-xs text-primary font-semibold hover:underline">
                Retry
              </button>
            </div>
          ) : recents.length === 0 ? (
            <div className="bg-white rounded-xl border border-dashed border-gray-200 p-10 flex flex-col items-center justify-center text-center">
              <div className="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center mb-3">
                <Sparkles size={20} className="text-primary" />
              </div>
              <p className="text-sm font-bold text-tertiary mb-1">No conversations yet</p>
              <p className="text-xs text-neutral max-w-sm leading-relaxed">
                Plan an app with AI or start a build, and your recent work will show up here.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {recents.map((item) => (
                // Both kinds now live at the same flat address; the kind only drives the badge.
                <RecentCard key={item.id} item={item} onClick={() => navigate(`/chat/${item.id}`)} />
              ))}
            </div>
          )}
        </div>
      </main>

      {picking && (
        <ProjectPicker
          title="Plan this app in which project?"
          onClose={() => setPicking(false)}
          onPick={startPlanChatInProject}
        />
      )}
    </div>
  )
}
