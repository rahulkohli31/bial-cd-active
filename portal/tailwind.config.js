/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: {
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
  plugins: [require('@tailwindcss/typography')],
}
