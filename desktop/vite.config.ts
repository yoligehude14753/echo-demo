import { defineConfig } from "vite";
import backendConfig from "./backend.config.json";
import react from "@vitejs/plugin-react";
import legacy from "@vitejs/plugin-legacy";
import path from "node:path";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolveViteBackendTarget, websocketTarget } from "./vite-backend-target.cjs";

const pkg = JSON.parse(
  readFileSync(path.resolve(__dirname, "package.json"), "utf-8"),
) as { version: string };
const defaultBackendTarget = `http://${backendConfig.roles.localDevDiagnostic.host}:${backendConfig.roles.localDevDiagnostic.port}`;
const viteBackendTarget = resolveViteBackendTarget(process.env, defaultBackendTarget);

const CSP_INLINE_SCRIPT_HASHES = new Set([
  "tQjf8gvb2ROOMapIxFvFAYBeUJ0v1HCbOcSmDNXGtDo=",
]);

function verifyProductionInlineScripts() {
  return {
    name: "echodesk-csp-inline-script-gate",
    enforce: "post" as const,
    closeBundle(): void {
      const html = readFileSync(path.resolve(__dirname, "dist/index.html"), "utf8");
      const inlineScripts = Array.from(
        html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g),
        (match) => match[1],
      ).filter((source) => source.trim().length > 0);
      const hashes = inlineScripts.map((source) =>
        createHash("sha256").update(source).digest("base64"),
      );
      if (
        hashes.length !== CSP_INLINE_SCRIPT_HASHES.size ||
        hashes.some((hash) => !CSP_INLINE_SCRIPT_HASHES.has(hash))
      ) {
        throw new Error(
          "production HTML contains an inline script not authorized by the Electron CSP",
        );
      }
    },
  };
}

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
    verifyProductionInlineScripts(),
  ],
  base: "./", // 让 Electron file:// 加载 dist/index.html 时能找到资源
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
    __APP_UPDATE_VERSION__: JSON.stringify(
      process.env.VITE_ECHODESK_UPDATE_VERSION || pkg.version,
    ),
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: viteBackendTarget,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target: websocketTarget(viteBackendTarget),
        ws: true,
      },
    },
  },
});
