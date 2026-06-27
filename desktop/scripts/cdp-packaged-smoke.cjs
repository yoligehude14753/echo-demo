#!/usr/bin/env node
const fs = require("node:fs");

const endpoint = process.argv[2] || "http://127.0.0.1:9335";
const platform = process.argv[3] || process.platform;
const outPng = process.argv[4] || `/tmp/echodesk-${platform}-packaged-smoke.png`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} HTTP ${response.status}`);
  return response.json();
}

async function connectCdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("websocket timeout")), 15_000);
    ws.onopen = () => {
      clearTimeout(timeout);
      resolve();
    };
    ws.onerror = (event) => {
      clearTimeout(timeout);
      reject(new Error(`websocket error: ${event.message || "unknown"}`));
    };
  });

  let id = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject, timeout } = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timeout);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result || {});
  };

  const send = (method, params = {}) => {
    const messageId = ++id;
    ws.send(JSON.stringify({ id: messageId, method, params }));
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(messageId);
        reject(new Error(`${method} timeout`));
      }, 20_000);
      pending.set(messageId, { resolve, reject, timeout });
    });
  };

  return { ws, send };
}

async function main() {
  const version = await getJson(`${endpoint}/json/version`);
  const targets = await getJson(`${endpoint}/json/list`);
  const target =
    targets.find(
      (candidate) =>
        candidate.type === "page" &&
        !String(candidate.url || "").startsWith("devtools://"),
    ) || targets[0];
  if (!target) throw new Error("no CDP page target");

  const conn = await connectCdp(target.webSocketDebuggerUrl);
  try {
    await conn.send("Runtime.enable");
    await conn.send("Page.enable");
    await sleep(3_000);

    async function evalJs(expression) {
      const result = await conn.send("Runtime.evaluate", {
        expression,
        returnByValue: true,
        awaitPromise: true,
      });
      if (result.exceptionDetails) {
        throw new Error(JSON.stringify(result.exceptionDetails));
      }
      return result.result?.value;
    }

    await evalJs(`window.localStorage.setItem("echodesk.onboarding.completed", "1")`);
    await conn.send("Page.reload", { ignoreCache: true });
    await sleep(4_000);

    const checks = await evalJs(`(() => {
      const text = document.body ? document.body.innerText : "";
      const rectOf = (selector) => {
        const el = document.querySelector(selector);
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          right: rect.right,
          bottom: rect.bottom,
          display: style.display,
          visibility: style.visibility,
          fontSize: style.fontSize,
        };
      };
      return {
        textSample: text.slice(0, 1400),
        brand: text.includes("EchoDesk"),
        connected: text.includes("\\u5df2\\u8fde\\u63a5"),
        hasOutputs: text.includes("outputs") || text.includes("\\u672c\\u4f1a\\u8bae\\u4ea7\\u7269"),
        hasWorkspace: text.includes("\\u5de5\\u4f5c\\u533a"),
        viewport: { width: window.innerWidth, height: window.innerHeight },
        body: {
          scrollWidth: document.documentElement.scrollWidth,
          scrollHeight: document.documentElement.scrollHeight,
        },
        settingsButton: rectOf('[data-testid="open-settings"]'),
        workspaceConfig: rectOf('[data-testid="workspace-config-btn"]'),
        captureStatus: rectOf('[data-testid="capture-status"]'),
        command: rectOf('textarea'),
      };
    })()`);

    for (const key of ["brand", "connected", "hasOutputs", "hasWorkspace"]) {
      if (!checks[key]) throw new Error(`missing required text: ${key}`);
    }

    for (const key of ["settingsButton", "workspaceConfig", "captureStatus", "command"]) {
      const rect = checks[key];
      if (!rect) throw new Error(`missing required element: ${key}`);
      if (rect.display === "none" || rect.visibility === "hidden") {
        throw new Error(`hidden element: ${key}`);
      }
      if (rect.width < 20 || rect.height < 20) throw new Error(`tiny element: ${key}`);
      if (rect.x < -1 || rect.right > checks.viewport.width + 2) {
        throw new Error(`horizontal overflow: ${key}`);
      }
      if (rect.bottom > checks.viewport.height + 2) {
        throw new Error(`vertical overflow: ${key}`);
      }
    }

    if (checks.body.scrollWidth > checks.viewport.width + 2) {
      throw new Error(`body horizontal overflow: ${checks.body.scrollWidth}`);
    }

    const clickedSettings = await evalJs(`(() => {
      const el = document.querySelector('[data-testid="open-settings"]');
      if (!el) return false;
      el.click();
      return true;
    })()`);
    if (!clickedSettings) throw new Error("settings click failed");
    await sleep(1_500);

    const settingsVisible = await evalJs(`(() => {
      const text = document.body ? document.body.innerText : "";
      return text.includes("\\u79fb\\u52a8\\u7aef\\u8fde\\u63a5") ||
        text.includes("\\u68c0\\u67e5\\u66f4\\u65b0") ||
        text.includes("\\u77e5\\u8bc6\\u5e93");
    })()`);
    if (!settingsVisible) throw new Error("settings panel not visible after click");

    const shot = await conn.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
    });
    fs.writeFileSync(outPng, Buffer.from(shot.data, "base64"));

    console.log(
      JSON.stringify(
        {
          platform,
          version,
          target: { url: target.url, title: target.title },
          checks,
          screenshot: outPng,
        },
        null,
        2,
      ),
    );
  } finally {
    conn.ws.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
