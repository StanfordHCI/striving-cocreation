/** @type {import('tailwindcss').Config} */
module.exports = {
  // Widget zone + shadcn primitives + main app components.
  // preflight stays disabled below so main-app plain-CSS resets are preserved.
  content: [
    // shadcn ui primitives live here (configured via components.json) — must be
    // scanned so their classes (esp. has-[...] variants) get compiled.
    './src/components/ui/**/*.{js,jsx,ts,tsx}',
    './src/lib/**/*.{js,jsx,ts,tsx}',
    // Main app — added so Tailwind classes used in src/App.tsx and
    // src/components/*.tsx get compiled.
    './src/App.tsx',
    './src/components/*.{js,jsx,ts,tsx}',
  ],
  // Disable preflight globally — we re-enable a scoped reset inside .widget-root
  // via custom CSS. This guarantees Tailwind base styles never bleed into the
  // main app (which uses plain CSS files).
  corePlugins: {
    preflight: false,
  },
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      transitionTimingFunction: {
        'ease-out-expo': 'cubic-bezier(0.16, 1, 0.3, 1)',
        'spring': 'cubic-bezier(0.34, 1.45, 0.64, 1)',
        'spring-pop': 'cubic-bezier(0.34, 1.7, 0.64, 1)',
      },
    },
  },
  plugins: [],
};
