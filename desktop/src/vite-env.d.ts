/// <reference types="vite/client" />

// mammoth 自带 lib/index.d.ts，但 browser bundle (mammoth.browser.js) 是 UMD
// 没单独 d.ts；ArtifactPreviewModal 动态 import 时 TS 报 7016。这里只声明 module 形状即可。
declare module "mammoth/mammoth.browser.js" {
  export function convertToHtml(input: {
    arrayBuffer: ArrayBuffer;
  }): Promise<{ value: string; messages: unknown[] }>;
  const _default: {
    convertToHtml: (input: {
      arrayBuffer: ArrayBuffer;
    }) => Promise<{ value: string; messages: unknown[] }>;
  };
  export default _default;
}
