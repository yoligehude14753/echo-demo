import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { readFileSync } from "node:fs";

const pkg = JSON.parse(
  readFileSync(path.resolve(__dirname, "package.json"), "utf-8"),
) as { version: string };

export default defineConfig({
  plugins: [react()],
  base: "./", // 让 Electron file:// 加载 dist/index.html 时能找到资源
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8769",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target:
          process.env.VITE_API_TARGET?.replace(/^http/, "ws") ??
          "ws://localhost:8769",
        ws: true,
      },
    },
  },
});
