import { defineConfig } from "vite";
import backendConfig from "./backend.config.json";
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
      // Electron loads the app over file://. plugin-legacy intentionally treats
      // file:// as non-modern and otherwise boots both modern and legacy entries,
      // producing duplicate React stores, WebSockets, and microphone capture.
      // A single legacy entry works for both the desktop shell and old TV WebViews.
      renderModernChunks: false,
    }),
  ],
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
        target:
          process.env.VITE_API_TARGET ??
          `http://${backendConfig.local.host}:${backendConfig.local.port}`,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target:
          process.env.VITE_API_TARGET?.replace(/^http/, "ws") ??
          `ws://${backendConfig.local.host}:${backendConfig.local.port}`,
        ws: true,
      },
    },
  },
});
