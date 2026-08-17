/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      colors: {
        // Core text/background — same names as before, evolved for a light,
        // card-on-canvas layout instead of the old dark editorial theme.
        ink: "#141a15",
        paper: "#f4f6f2",
        panel: "#ffffff",
        line: "#e5e8e1",
        muted: "#767d72",
        // Brand: deep pine for actions/active state, the original acid lime
        // kept as a small highlight accent so the identity carries over.
        brand: "#1c7a4d",
        "brand-dark": "#125c39",
        "brand-soft": "#e6f3ec",
        acid: "#c8f24c",
        "acid-soft": "#f1fbdc",
        orange: "#e8720c",
        "orange-soft": "#fdf1e2",
        good: "#1c7a4d",
        "good-soft": "#e6f3ec",
        bad: "#c23b2c",
        "bad-soft": "#fbe9e6",
      },
      boxShadow: {
        card: "0 1px 2px rgba(20, 26, 21, 0.04), 0 8px 24px -12px rgba(20, 26, 21, 0.10)",
        popover: "0 12px 32px -8px rgba(20, 26, 21, 0.22)",
      },
      borderRadius: {
        xl2: "1.1rem",
      },
    },
  },
  plugins: [],
};