/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Manrope"', "system-ui", "sans-serif"],
        mono: ['"DM Mono"', "ui-monospace", "monospace"],
      },
      colors: {
        ink: "#151713",
        paper: "#ecebdc",
        panel: "#f5f4e8",
        acid: "#d8ff45",
        orange: "#ff7043",
        line: "#c7c7b8",
        muted: "#77796e",
        good: "#146c43",
        bad: "#a72822",
      },
    },
  },
  plugins: [],
};