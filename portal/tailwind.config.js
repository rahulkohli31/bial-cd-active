/** @type {import('tailwindcss').Config} */
export default {
  // 'class' strategy for shadcn/ui (U13 prep). No behavior change today: the portal has
  // zero `dark:` usages, so nothing activates until an element opts in with class="dark".
  darkMode: ['class'],
  // Streamdown's dist ships its own Tailwind utility classes (code-block controls,
  // table controls, etc.) — scan it too so those classes aren't purged.
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}', './node_modules/streamdown/dist/**/*.js'],
  theme: {
    extend: {
      /**
       * THE STACKING THRESHOLD, AS THE BOARD NUMBERS IT (plan 002, U7).
       *
       * `ResizeBounds` is explicit: the handle is "ignored below 1100px of window — there is not
       * enough room for two useful columns, so the app pane stacks under the conversation and the
       * handle disappears rather than becoming a control that cannot help."
       *
       * Its own screen rather than Tailwind's `lg` (1024px), because the number is a design
       * decision with a stated reason and borrowing a framework default would make it look like
       * one. Named `wide` rather than `xl` so it cannot be mistaken for a position in the stock
       * ramp — it sits between `lg` and `xl` and belongs to one layout.
       */
      screens: {
        wide: '1100px',
      },
      colors: {
        // shadcn/ui token names (U13 prep) — ADDITIVE ONLY. Every pre-existing name
        // (primary/secondary/accent/…) keeps its literal hex DEFAULT so the 70+ existing
        // bg-primary/bg-accent/… call sites resolve byte-identically; shadcn components
        // pick the SAME brand values up through the new `*-foreground`/token names, whose
        // HSL variables live in src/index.css.
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        // Streamdown's dist code-block chrome (Streamdown variant, A2) styles itself with
        // `bg-sidebar`/`bg-sidebar/80`/`border-sidebar` — undefined here left the card
        // transparent, so it needs a real token even though nothing else in the portal
        // uses "sidebar" as a concept yet.
        sidebar: {
          DEFAULT: 'hsl(var(--sidebar))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        primary: {
          foreground: 'hsl(var(--primary-foreground))',
          DEFAULT: '#0D7377',
          dark: '#0A5C5F',
          50: '#E0F5F6',
          100: '#B3E6E9',
          200: '#80D5DA',
          300: '#4DC4CC',
          400: '#26B7C0',
          500: '#0D7377',
          600: '#0A5C5F',
          700: '#084B4E',
          800: '#063A3C',
          900: '#1A2B34',
        },
        secondary: {
          foreground: 'hsl(var(--secondary-foreground))',
          DEFAULT: '#D9A036',
          50: '#FDF5E6',
          100: '#FAE7BF',
          200: '#F7D896',
          300: '#F3C96C',
          400: '#F1BD4E',
          500: '#D9A036',
          600: '#C08A2E',
          700: '#A67326',
          800: '#8C5D1E',
          900: '#6B430F',
        },
        accent: {
          DEFAULT: '#F5A623',
          light: '#FFF4E0',
          foreground: 'hsl(var(--accent-foreground))',
        },
        tertiary: '#1A1A2E',
        neutral: '#6B7280',
        surface: {
          DEFAULT: '#FFFFFF',
          muted: '#F8F9FA',
        },
        bial: {
          bg: '#F0F4F8',
          surface: '#FFFFFF',
          border: '#E2E8F0',
        },
        success: '#22C55E',
        warning: '#EAB308',
        danger: '#EF4444',
        /**
         * THE UX CANVAS'S OWN PALETTE — the roles the brand ramp has no name for. Every value
         * below is a hex read off `docs/ux-canvas/boards/*.dc.html`; nothing here is invented,
         * and nothing here is a second name for a colour the brand ramp already owns (the
         * canvas's ink #1A2B34 is `primary-900`, its hairline #E2E8F0 is `bial-border`, its
         * muted text #6B7280 is `neutral`, its teal #0D7377 is `primary`).
         *
         * They live as tokens rather than as `bg-[#B45309]` literals because the status panel
         * and the chip beside a chat title have to agree state by state, and nine hard-coded
         * pairs in two files is exactly how they would stop agreeing.
         */
        canvas: {
          label: '#9CA3AF',        // the small-caps section and row labels
          placeholder: '#9AA5B1',  // composer placeholder text
          sha: '#B4BCC6',          // the muted short build id beside a date
          tile: '#F1F5F9',         // an activity tool tile's ground
          group: '#FCFDFD',        // an activity group's ground
          rule: '#D8E0E8',         // the app card's border, a shade darker than a hairline
          track: '#F7F9FB',        // the resize handle's track
          grip: '#DCE3EA',         // the resize handle's grip border
          savedirty: '#F5FCFC',    // the pale teal ground behind an unsaved Save control
          sendoff: '#D6DDE4',      // the send control with nothing to send — a ground, not an opacity
          tilelive: '#E0F5F6',     // a tool tile whose step is running (= primary-50)
          tileedge: '#B3E6E9',     // …and its border (= primary-100)
          offer: '#F5FBFB',        // the plan-ready strip's ground, fixed to the top of the box
          offerrule: '#D9EBEC',    // …the hairline under it
          offeredge: '#CDE9EA',    // …and the box's own border while the offer is live
          offerink: '#0A5C5F',     // the strip's headline — darker than the action teal, on purpose
          offerlock: '#F8FAFC',    // …and the input row's ground while that offer waits to be answered
        },
        /**
         * The nine status states of `StatusCardStates`, as text / ground / dot triples. Six
         * colour families cover the nine states because three pairs of states share a look and
         * differ only in their words.
         */
        /**
         * A GROUP WHOSE WORK FAILED, from `ActivityAnatomy` panel 4. Its own family rather than
         * the `status.red` pair: those are pill colours at pill weights, and this is a container
         * that has to sit quietly in a transcript while still being unmistakable.
         */
        problem: {
          edge: '#F4C7C7',
          ground: '#FEF7F7',
          ink: '#B4483F',
        },
        status: {
          'faint-fg': '#6B7280', 'faint-bg': '#F1F4F8', 'faint-dot': '#B4BCC6',  // nothing built yet
          'grey-fg': '#4B5563', 'grey-bg': '#F1F4F8', 'grey-dot': '#94A3B8',     // draft
          'amber-fg': '#B45309', 'amber-bg': '#FEF3C7', 'amber-dot': '#D97706',  // in review; and the drifted date
          'red-fg': '#B91C1C', 'red-bg': '#FEE2E2', 'red-dot': '#DC2626',        // changes requested; didn't start
          'green-fg': '#15803D', 'green-bg': '#DCFCE7', 'green-dot': '#16A34A',  // starting up; live
          'off-fg': '#4B5563', 'off-bg': '#E5E7EB', 'off-dot': '#9CA3AF',        // switched off
        },
      },
      boxShadow: {
        /** The canvas's selected-segment elevation — the whole of how the Plan/Build control
         *  signals its choice, since the board gives that control no hue at all. */
        segment: '0 1px 2px rgba(16,24,40,.08)',
        /** The app card's lift off the `#F0F4F8` pane ground — `PreviewOff`, `NothingBuilt` and
         *  `PreviewStarting` all give the empty pane the same white card the running app gets. */
        'app-card': '0 4px 16px rgba(16,24,40,.07), 0 1px 3px rgba(16,24,40,.05)',
      },
      fontFamily: {
        manrope: ['Manrope', 'sans-serif'],
        worksans: ['"Work Sans"', 'sans-serif'],
      },
      // `--radius` has been declared in index.css since the shadcn token prep and was never
      // wired to a Tailwind key, so every `rounded-lg` in a copied component silently fell
      // back to Tailwind's STOCK radius. That is not a visible error — it is a component
      // that looks almost right — which is the whole failure class the token guard exists
      // for. These three names are the shadcn convention and are what copied sources use.
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      // Radix Collapsible drives the activity group's expand-in-place. It measures the
      // panel and publishes `--radix-collapsible-content-height`; the animation is ours to
      // declare. `tailwindcss-animate` does NOT ship these — it ships enter/exit utilities —
      // so `animate-collapsible-down` resolves to nothing without this and the group snaps.
      keyframes: {
        'collapsible-down': {
          from: { height: '0' },
          to: { height: 'var(--radix-collapsible-content-height)' },
        },
        'collapsible-up': {
          from: { height: 'var(--radix-collapsible-content-height)' },
          to: { height: '0' },
        },
        'pane-leave': {
          from: { opacity: '1', transform: 'translateX(0)' },
          to: { opacity: '0', transform: 'translateX(6%)' },
        },
        'pane-return': {
          from: { opacity: '0', transform: 'translateX(6%)' },
          to: { opacity: '1', transform: 'translateX(0)' },
        },
      },
      animation: {
        'collapsible-down': 'collapsible-down 0.2s ease-out',
        'collapsible-up': 'collapsible-up 0.2s ease-out',
        /**
         * THE APP PANE LEAVING AND RETURNING (plan 002, U6). `T2Sliding` is a whole board about
         * this one movement — "the app card is sliding out to the right and fading as it goes" —
         * and its annotation is the point: it is the MOVEMENT, not a broken screen, and "nothing
         * about the app is stopped or reloaded — it is only taken off the screen."
         *
         * A TRANSITION, NOT A LIBRARY. There is no motion library in this project and none is
         * added: the pane is one element whose visibility the shell already toggles, so a
         * keyframe pair is the whole mechanism. Both are suppressed under
         * `prefers-reduced-motion` in `index.css`, which is where every other one in this build
         * is suppressed too.
         */
        'pane-leave': 'pane-leave 0.24s ease-in forwards',
        'pane-return': 'pane-return 0.24s ease-out',
      },
    },
  },
  plugins: [require('@tailwindcss/typography'), require('tailwindcss-animate')],
}
