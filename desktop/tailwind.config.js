/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: {
          900: "#0b0f17",
          800: "#11161f",
          700: "#1a2030",
        },
        accent: "#5b8cff",
        ok: "#3ecf8e",
        warn: "#f0b429",
        err: "#ff6b6b",
      },
    },
  },
  plugins: [],
};
