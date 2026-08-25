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
      },
      fontFamily: {
        manrope: ['Manrope', 'sans-serif'],
        worksans: ['"Work Sans"', 'sans-serif'],
      },
    },
  },
  plugins: [require('@tailwindcss/typography'), require('tailwindcss-animate')],
}
