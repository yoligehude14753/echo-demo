import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  base: "./", // 让 Electron file:// 加载 dist/index.html 时能找到资源
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
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
