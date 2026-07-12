/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "PingFang SC",
          "Microsoft YaHei",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
      colors: {
        ink: {
          900: "#0d0d0d",
          800: "#1f1f1f",
          700: "#353535",
          600: "#525252",
          500: "#737373",
          400: "#a3a3a3",
        },
        paper: {
          50: "#ffffff",
          100: "#fafafa",
          150: "#f7f7f8",
          200: "#f0f0f0",
          300: "#e5e5e5",
          400: "#d4d4d4",
        },
        accent: "#10a37f",
        accentDark: "#0d8a6b",
        warn: "#d97706",
        err: "#dc2626",
      },
      boxShadow: {
        card: "0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.06)",
        pop: "0 12px 32px rgba(0,0,0,0.10)",
      },
    },
  },
  plugins: [],
};
