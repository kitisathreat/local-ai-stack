import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:       "#0a0a0a",
        panel:    "#121212",
        border:   "#1f1f1f",
        muted:    "#888",
        accent:   "#5b5fc7",
      },
    },
  },
} satisfies Config;
