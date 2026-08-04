import { useNavigate } from 'react-router-dom'
import { Boxes, ArrowRight, Info } from 'lucide-react'
import Navbar from '../components/layout/Navbar'
import { getStoredUser } from '../utils/auth'

export default function Dashboard() {
  const navigate = useNavigate()

  // FIXED here (the one deliberate behavior change in this migration, not a
  // types-only diff): this used to read user?.name || user?.username, but
  // UserProfile (utils/auth.ts) has neither field — only id/email/display_name/
  // is_admin/isAdmin/limits. Both reads were always undefined, so the greeting
  // has shown "Hello, there" to every user since the cookie-session profile
  // shape landed. There was no behavior-preserving option that kept the test
  // suite green: hardcoding 'there' breaks Dashboard.test.jsx's real assertion,
  // and editing that test to expect 'there' would enshrine a live bug instead
  // of fixing it. Uses display_name, matching Navbar.tsx's read of the same
  // field for the same purpose.
  const user = getStoredUser()
  const greetingName = user?.display_name || 'there'

  return (
    <div className="min-h-screen font-manrope flex flex-col" style={{ background: 'linear-gradient(160deg, #ffffff 0%, #f0f9f9 100%)' }}>
      <Navbar />

      <main className="flex-1 max-w-5xl mx-auto w-full px-6 py-14">
        {/* Welcome header */}
        <p className="text-xs font-worksans font-semibold tracking-widest uppercase text-primary mb-2">
          Welcome Back
        </p>
        <h1 className="text-4xl font-extrabold text-tertiary mb-3">
          Hello, {greetingName}
        </h1>
        <p className="text-neutral text-base leading-relaxed max-w-2xl mb-6">
          Ready to build the future of aviation? Every tool you build lives in a project — open one, or start a new one.
        </p>

        {/* Pilot (POC) disclaimer — sets expectations that this is an early
            proof-of-concept, not a production system. */}
        <div className="flex items-start gap-3 max-w-2xl mb-10 rounded-2xl border border-bial-border bg-primary/5 px-4 py-3">
          <Info size={16} className="text-primary flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-tertiary">Pilot (POC)</p>
            <p className="text-xs text-neutral leading-relaxed">
              This is an early proof-of-concept of the Citizen Developer Portal.
            </p>
          </div>
        </div>

        {/* One front door. A project is where a tool's app, its shared description, and
            its chats — the build composer included — all live. */}
        <div className="max-w-xl">
          {/* Projects — the container a citizen developer opens and returns to */}
          <div
            onClick={() => navigate('/projects')}
            className="relative rounded-2xl p-6 flex flex-col overflow-hidden cursor-pointer transition-transform hover:-translate-y-1 bg-primary text-white shadow-xl shadow-primary/20"
          >
            <div className="absolute top-0 right-0 w-32 h-32 rounded-bl-full opacity-10 bg-white" />

            <div className="w-10 h-10 rounded-xl flex items-center justify-center mb-4 bg-white/20 text-white">
              <Boxes size={18} />
            </div>

            <h2 className="text-lg font-bold mb-2 text-white">Projects</h2>
            <p className="text-sm leading-relaxed flex-1 mb-6 text-white/80">
              Each project is one tool — its app, the description every chat shares, and the chats that shaped it. Open one to describe, build, and refine your app.
            </p>

            <button className="flex items-center gap-1 text-sm font-semibold text-white hover:text-white/80 transition">
              Open Projects
              <ArrowRight size={14} />
            </button>
          </div>
        </div>
      </main>

      <footer className="border-t border-bial-border bg-white py-4 px-6">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <p className="text-xs text-neutral">Kempegowda International Airport Bengaluru &middot; V 2.4.0-Build</p>
          <div className="flex gap-5">
            <button
              onClick={() => navigate('/help')}
              className="text-xs text-neutral hover:text-primary transition"
            >
              Support
            </button>
          </div>
        </div>
      </footer>
    </div>
  )
}
