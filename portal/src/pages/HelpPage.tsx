import { useState } from 'react'
import { ChevronDown, CheckCircle, XCircle } from 'lucide-react'
import Navbar from '../components/layout/Navbar'

// The one support address this page states, rather than the two hardcoded copies it
// carried (each under its own "TODO: confirm support address"). The CONFIGURED address
// is `ApiSettings.SUPPORT_CONTACT_EMAIL`, but it is backend-only: it reaches a citizen
// solely inside the server-rendered at-limit sentence that `TurnBanner` turns into a
// mailto. No endpoint returns it, and the portal reads no VITE_* vars, so this page
// cannot read it without the config endpoint filed under #157 B3. Until then a single
// constant is the honest shape: one place to fix when the address is confirmed, instead
// of two that can drift apart — which is the trap B3 removed one level down.
const SUPPORT_EMAIL = 'citizen-developer-support@bialairport.com'

const EXAMPLE_PROMPTS = [
  {
    level: 'Simple',
    color: 'bg-green-100 text-green-700',
    text: 'Build a shift handover checklist for Ground Ops supervisors. Include checkboxes for each task, a notes field, and a timestamp when the handover is signed off.',
  },
  {
    level: 'Intermediate',
    color: 'bg-blue-100 text-blue-700',
    text: 'Create a gate assignment dashboard that shows all Terminal 2 gates on a map layout. Color-code gates by status: green for available, amber for scheduled, red for delayed. I will upload the current day\'s gate schedule to populate it.',
  },
  {
    level: 'Advanced',
    color: 'bg-purple-100 text-purple-700',
    text: 'Build a predictive delay alert system for Terminal 3 operations. Use the flight schedule and historical delay patterns I upload to flag flights likely to experience delays exceeding 20 minutes. Show a priority-ranked list with estimated delay duration, affected gate, and suggested resource reallocation. Include a one-tap escalation button that notifies the shift supervisor.',
  },
  {
    level: 'Full-stack',
    color: 'bg-amber-100 text-amber-700',
    text: 'Create a staff cab and carpool sharing app called RideLink BLR. Include a staff profile setup across 3 departments, a ride request flow with shift-time matching, a carpool offer flow with seat management, and a mobile-first dashboard. Use Bangalore Airport theme.',
  },
]

const DOS = [
  'Name the specific terminal, gate, or zone',
  'Describe the data the app works with (uploads or records it captures)',
  'Describe 3–5 key features explicitly',
  'Specify the user type (ground ops, control room, passenger-facing)',
  'Mention layout preference (mobile-first, dashboard, kiosk)',
  'Iterate with the AI chat after generation — ask for specific changes',
]

const DONTS = [
  'Say "make an app for the airport" with no specifics',
  'Assume the AI knows which system to connect to',
  'Write a single vague sentence',
  'Leave out who the app is for',
  'Expect the AI to guess the right layout',
  'Expect a perfect app on the first prompt',
]

/** Exported for HelpPage.test.tsx. Asserting on the DATA rather than a render is
 *  deliberate: AccordionItem renders its answer as `{open && ...}`, so a collapsed
 *  accordion puts no answer text in the DOM at all and every "this false claim is
 *  gone" assertion would pass vacuously. That exact trap cost a round in the #157
 *  browser harness; the data cannot go vacuous the same way. */
export const FAQS = [
  {
    q: 'What is the BIAL Citizen Developer portal?',
    a: 'It is an internal platform that empowers Bangalore International Airport staff to build custom operational tools and digital solutions without writing code. You describe what you need in plain English, and the AI generates a working application for terminal operations.',
  },
  {
    q: 'Who can use this portal?',
    a: 'Sign in with your BIAL Microsoft account — the only sign-in the portal offers. Access is limited to the BIAL organisation. No programming experience is required.',
  },
  {
    q: 'What happens when I click "Start Chat"?',
    a: 'That depends on the mode you picked. The composer has three modes and defaults to Plan. In Ask, it opens a chat that answers questions about your app and changes nothing. In Plan, it works out an approach with you and waits for your confirmation before anything is built. In Write, it starts the multi-minute build straight away, with no plan step — so from Plan you reach a build by approving the plan first, and from Write you are already in one. Changes stay in your workspace until you save them.',
  },
  {
    q: 'Can I edit the app after it is generated?',
    a: 'Yes. The chat panel on the left side of the builder lets you request changes in plain English. You can ask to change colors, add tables, remove sections, switch to mobile layout, and more.',
  },
  {
    q: 'How does my app get its data?',
    a: 'Apps work with the data you provide — upload a CSV file to view and analyze it, or let the app capture and store records in its own private database, which the platform creates for each project automatically. The portal does not connect to external airport systems during this pilot.',
  },
  {
    q: 'What files can I attach in chat?',
    a: 'Images (PNG, JPEG, GIF, WebP), PDFs, and text files (CSV, TXT) — up to 4 MB each. Images and PDFs are attached as they are; a CSV or TXT rides inside the message itself, so the assistant reads its contents directly. The original file is always kept and can be re-downloaded from its chip. Anything else is not accepted — save the part you need as a PDF, or export a spreadsheet as CSV.',
  },
  {
    q: 'Is there a limit to how many apps I can build?',
    a: 'There is no limit on how many apps you can create. There is a daily limit on how much AI work you can use across all of them: the token counter in the header shows what you have left and turns amber once you have used 80% of it. When the allowance runs out it stops every AI action, not just builds — Ask and Plan included — until it resets at midnight IST.',
  },
  {
    q: 'Who do I contact for help?',
    a: `Use the Feedback button in the header to record a note for the platform team, or email ${SUPPORT_EMAIL} directly.`,
  },
]

function AccordionItem({ question, answer }: { question: string; answer: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-b border-bial-border last:border-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between py-4 text-left gap-4 hover:text-primary transition"
      >
        <span className="text-sm font-semibold text-tertiary">{question}</span>
        <ChevronDown size={16} className={`text-neutral flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="pb-4 pr-6">
          <p className="text-sm text-neutral leading-relaxed">{answer}</p>
        </div>
      )}
    </div>
  )
}

export default function HelpPage() {
  return (
    <div className="min-h-screen bg-bial-bg font-manrope flex flex-col">
      <Navbar />

      <main className="flex-1 px-6 py-10">
        <div className="max-w-3xl mx-auto space-y-8">
          {/* Header */}
          <div>
            <h1 className="text-3xl font-extrabold text-tertiary mb-2">Help Center</h1>
            <p className="text-neutral leading-relaxed">
              Everything you need to know about building and managing apps on the BIAL Citizen Developer portal.
            </p>
          </div>

          {/* Quick Start */}
          <div className="bg-primary rounded-2xl p-6 text-white">
            <h2 className="text-lg font-bold mb-1">New here? Start with the basics</h2>
            <p className="text-white/70 text-sm mb-5">Three steps to your first operational app.</p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {[
                { n: '1', title: 'Describe your app', body: 'Open a project and write a plain English prompt describing what tool you need.' },
                { n: '2', title: 'Refine with AI', body: 'Our AI assistant will generate the app. Use the chat to adjust layout, data, and features.' },
                { n: '3', title: 'Save & Revisit', body: 'Your app lives in its project — reopen it anytime to keep refining.' },
              ].map(({ n, title, body }) => (
                <div key={n} className="bg-white/10 rounded-xl p-4">
                  <div className="w-6 h-6 rounded-full bg-white/20 text-white text-xs font-bold flex items-center justify-center mb-3">{n}</div>
                  <p className="text-sm font-bold mb-1">{title}</p>
                  <p className="text-xs text-white/75 leading-relaxed">{body}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Prompt Engineering Guide */}
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
            <h2 className="text-lg font-bold text-tertiary mb-5">Writing Effective Prompts</h2>

            <div className="space-y-6">
              {/* What makes a good prompt */}
              <div>
                <h3 className="text-sm font-bold text-tertiary mb-3">What makes a good prompt?</h3>
                <div className="space-y-3">
                  {[
                    { title: 'Be specific about the problem', body: 'Instead of "Build a tracking app", say "Build a real-time baggage carousel status tracker for Terminal 3, showing load percentage, motor temperature, and maintenance alerts for belts 12A through 24C."' },
                    { title: 'Describe the data', body: 'Explain what information the app works with — the columns of a CSV you will upload, or the records the app should capture and store. Attaching a sample file in the builder grounds the app in real data.' },
                    { title: 'Describe the users', body: 'Who will use this app? Ground ops staff on mobile during shifts? Control room operators on desktop dashboards? Knowing the user shapes the layout.' },
                    { title: 'Specify key features', body: 'List the 3–5 most important features. For example: "Include a calendar view, a status dashboard, alert notifications, and a CSV export button."' },
                    { title: 'Set design expectations', body: 'Mention if you want mobile-first, dashboard-style, kiosk-friendly, or standard desktop layout.' },
                  ].map(({ title, body }) => (
                    <div key={title} className="flex gap-3">
                      <div className="w-1.5 h-1.5 rounded-full bg-primary mt-2 flex-shrink-0" />
                      <div>
                        <span className="text-sm font-semibold text-tertiary">{title}: </span>
                        <span className="text-sm text-neutral">{body}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Example prompts */}
              <div>
                <h3 className="text-sm font-bold text-tertiary mb-3">Example Prompts</h3>
                <div className="space-y-3">
                  {EXAMPLE_PROMPTS.map(({ level, color, text }) => (
                    <div key={level} className="border border-bial-border rounded-xl p-4">
                      <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${color} inline-block mb-2`}>{level}</span>
                      <p className="text-sm text-neutral leading-relaxed italic">"{text}"</p>
                    </div>
                  ))}
                </div>
              </div>

              {/* Do / Don't */}
              <div>
                <h3 className="text-sm font-bold text-tertiary mb-3">Prompt Dos and Don'ts</h3>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="bg-green-50 border border-green-100 rounded-xl p-4">
                    <p className="text-xs font-bold text-green-700 uppercase tracking-wider mb-3">Do</p>
                    <div className="space-y-2">
                      {DOS.map((item) => (
                        <div key={item} className="flex items-start gap-2">
                          <CheckCircle size={13} className="text-green-500 flex-shrink-0 mt-0.5" />
                          <span className="text-xs text-green-800 leading-relaxed">{item}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="bg-red-50 border border-red-100 rounded-xl p-4">
                    <p className="text-xs font-bold text-red-600 uppercase tracking-wider mb-3">Don't</p>
                    <div className="space-y-2">
                      {DONTS.map((item) => (
                        <div key={item} className="flex items-start gap-2">
                          <XCircle size={13} className="text-red-400 flex-shrink-0 mt-0.5" />
                          <span className="text-xs text-red-700 leading-relaxed">{item}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* FAQ */}
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
            <h2 className="text-lg font-bold text-tertiary mb-2">Frequently Asked Questions</h2>
            <div className="mt-4">
              {FAQS.map(({ q, a }) => (
                <AccordionItem key={q} question={q} answer={a} />
              ))}
            </div>
          </div>

          {/* Footer note */}
          <div className="text-center pb-8">
            <p className="text-sm text-neutral">
              Still need help?{' '}
              <a href={`mailto:${SUPPORT_EMAIL}`} className="text-primary font-semibold hover:underline">
                Contact IT Support Desk
              </a>
            </p>
          </div>
        </div>
      </main>
    </div>
  )
}
