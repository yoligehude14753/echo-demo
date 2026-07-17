# F03 Packaged Layout Contract

状态：dry-run only，`BUNDLE_ADAPTER_REQUIRED`。

## 当前 Echo package layout

```text
app.asar/
  dist/**
  electron/**
  backend.config.json
  package.json
Resources/
  backend/echodesk-backend[.exe]
```

当前 `desktop/package.json` 的 `files`/`extraResources` 没有 `agent-runtime` worker、manifest、chunks、WASM/native 资源或 `asarUnpack` 规则。

## 未来 kernel layout（设计，不是当前配置）

```text
Resources/agent-runtime/
  manifest.json
  worker.mjs
  chunks/**
  resources/**
  wasm/**
  native/<platform>-<arch>/**
```

manifest 必须绑定 Echo effective baseline、Claude source snapshot、Electron major、platform/arch、worker entry、每个文件的 size/SHA-256、load mode 和 excluded dependency list。native/WASM 必须明确在 asar 外置或 unpack；未知动态 specifier、hash/ABI mismatch、manifest 缺失均 fail closed。

## Dry-run coverage

- macOS 空格/中文路径：shape 通过；Windows drive/UNC/long-path shape 在真实 Sunny Windows 上通过。
- macOS/Sunny embedded Electron main + worker/API probe：通过。
- UNC share、long-path filesystem 实际写读、asar/unpacked 真实 readback、Program Files/NSIS、签名/ACL/UAC：未执行。
- 未构建 DMG/NSIS，未替换安装版本，未启动 EchoDesk 产品。

因此本文件是 adapter contract 输入，不是 installed/package acceptance。
