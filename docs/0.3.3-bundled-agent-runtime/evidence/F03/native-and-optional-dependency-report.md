# F03 Native / Optional / WASM / Resource Report

状态：`BUNDLE_ADAPTER_REQUIRED`，存在 `UNKNOWN_CRITICAL`。

## 实际观察

- `desktop/package-lock.json` 为 lockfile v3；Electron 43.1.0，optional 条目 77 个；当前 desktop 产品 `node_modules` 不存在。
- Claude source `/Users/yoligehude/Downloads/src` 约 1902 个 TS/TSX/JS/JSX/CJS/MJS 文件，但没有 package manifest、lockfile、tsconfig 或 bundler manifest。
- 静态证据包含 `audio-capture.node`、`bun:ffi`、`child_process`、动态 import/require、WASM/binary 资源语义、HOME/`.claude`/settings/plugins/memory 读取。
- 当前 Echo tracked tree 没有 Claude kernel worker、manifest、`.node`、`.wasm` 或 agent resource hash 清单。

## 风险矩阵

| 依赖面 | 当前状态 | 需要的 adapter/gate |
|---|---|---|
| native addon | 未绑定平台/ABI/路径 | macOS arm64 与 Windows x64 独立产物、ABI/hash、asar 外置 |
| `bun:ffi` / child process | 不在 kernel allowlist | 排除或改为 Echo capability adapter |
| optional imports | source feature-gated，未锁定 closure | capability-scoped inclusion list，未知 specifier fail closed |
| WASM/binary/resource | 未见 manifest/hash/readback | manifest + file URL + unpack policy + 两端 readback |
| package exports | SDK `.mjs` 与 `src/*.js` alias 未锁定 | source-aware bundler contract、exact dependency lock |

## 结论

Electron 43.1.0 embedded runtime 的最小 API probe 通过，不等于 Claude source closure 通过。只要上述 native/optional/WASM/resource 任一项仍未知，不能给出 `RUNTIME_COMPATIBLE`。
