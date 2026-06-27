import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import legacy from "@vitejs/plugin-legacy";
import path from "node:path";
import { readFileSync } from "node:fs";

const pkg = JSON.parse(
  readFileSync(path.resolve(__dirname, "package.json"), "utf-8"),
) as { version: string };

export default defineConfig({
  plugins: [
    react(),
    legacy({
      targets: ["Chrome >= 49", "Android >= 5"],
      modernPolyfills: true,
      renderLegacyChunks: true,
    }),
  ],
  base: "./", // 让 Electron file:// 加载 dist/index.html 时能找到资源
  build: {
    // 会议室 Android TV WebView 可能停在 Chrome 60~70，不能解析 optional
    // chaining / nullish coalescing。桌面 Electron 也能运行这份更保守的 bundle。
    target: "chrome61",
  },
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
