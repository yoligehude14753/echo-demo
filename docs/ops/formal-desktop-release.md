# 正式桌面候选与一次性凭证迁移 SOP

本文只描述可审计的发布候选链。GitHub Actions artifact、本地 `release/` 目录、unsigned
Windows 包和 ad-hoc macOS 包都不是公开 Release。

## 1. 正式桌面候选链

工作流：`.github/workflows/build-desktop-release-candidates.yml`

工作流只有 `workflow_dispatch`，权限中没有 `contents: write`，也不执行 `gh release
upload`。它会生成、验签、测试、attest 并上传候选 artifact，但不会自动公开发布。正式公开
Release 仍需在独立验收精确提交和候选证据后单独执行。

### 1.1 Windows v0.3.2 已知边界与当前阻塞

以下是 2026-07-13 的服务端快照，执行发布前必须重新查询，不能把它当作永久状态：

- [`Build Windows Installer` run 29243474851](https://github.com/yoligehude14753/echo-demo/actions/runs/29243474851)
  在分支 `codex/runtime-asr-agent-fix` 的提交
  `c18c98be48a1e8b54f0a30a835f7354873f7c078` 上成功完成 unsigned NSIS/ZIP 构建、安装态
  smoke、便携 ZIP smoke、内容扫描、SBOM 和 SHA-256 校验。
- 该 run 只生成名为 `echodesk-windows-unsigned-test` 的测试 artifact；没有 Authenticode
  身份、RFC 3161 timestamp 或 build provenance，因此不是正式候选，也不得复制、重命名或
  上传到 GitHub Release。
- repository environment API 当时只返回 `android-release` 与 `github-pages`，不存在
  `desktop-release-windows`；repository secret 名称列表也不包含下述四个 Windows
  Authenticode 输入。正式链当前因此处于 `Blocked`，不能用 unsigned run 的成功替代。

run 29243474851 只证明 unsigned 包装链、安装态/便携启动和本地完整性合同可执行；它不证明
证书所有权、发布者身份、签名链、timestamp、受保护 environment、provenance 或公开 Release
资格。后续正式 run 必须来自 `main` 的精确 SHA，并重新执行全部 signed 候选门禁。

### 1.2 受保护 environment

在 canonical repository 中建立两个 environment：

- `desktop-release-macos`
- `desktop-release-windows`

两者都必须只允许 `main`、启用 required reviewers、禁止 admin bypass。环境规则属于 GitHub
服务端配置，不能由 workflow YAML 自证；首次运行前和每次规则变更后都应从 API/UI 留存证据。

`desktop-release-macos` 只保存以下 environment secrets：

```text
ECHODESK_MAC_CERTIFICATE_P12_BASE64
ECHODESK_MAC_CERTIFICATE_PASSWORD
ECHODESK_MAC_DEVELOPER_ID_APPLICATION
ECHODESK_MAC_NOTARY_APPLE_ID
ECHODESK_MAC_NOTARY_APP_PASSWORD
ECHODESK_MAC_TEAM_ID
```

`ECHODESK_MAC_DEVELOPER_ID_APPLICATION` 必须是完整的 `Developer ID Application: <Name>
(<TEAMID>)`，末尾 Team ID 必须等于 `ECHODESK_MAC_TEAM_ID`。

`desktop-release-windows` 只保存以下 environment secrets：

```text
ECHODESK_WINDOWS_CERTIFICATE_PFX_BASE64
ECHODESK_WINDOWS_CERTIFICATE_PASSWORD
ECHODESK_WINDOWS_CERTIFICATE_SHA1
ECHODESK_WINDOWS_EXPECTED_PUBLISHER
```

SHA-1 thumbprint 必须是目标 Authenticode 证书的 40 位指纹；publisher 必须是证书完整
Subject。缺任一 secret、证书/身份不匹配、签名链或 timestamp 不通过时，工作流以带缺项名称的
`Blocked` error 结束，不能降级为 unsigned/ad-hoc 候选。

首次正式运行前，发布操作员必须从服务端验证 environment、保护规则和 secret **名称**。以下
命令不会读取 secret 值；任一步失败都必须停止，不得先触发 workflow 期待它自动创建 environment：

```bash
repo="yoligehude14753/echo-demo"
environment="desktop-release-windows"

environment_json="$(gh api "repos/${repo}/environments/${environment}")"
jq -e '
  .name == "desktop-release-windows"
  and .can_admins_bypass == false
  and any(.protection_rules[]?; .type == "required_reviewers")
  and any(.protection_rules[]?; .type == "branch_policy")
' <<<"${environment_json}" >/dev/null

gh api "repos/${repo}/environments/${environment}/deployment-branch-policies" \
  --jq 'any(.branch_policies[]; .name == "main")' | grep -Fxq true

required=(
  ECHODESK_WINDOWS_CERTIFICATE_PFX_BASE64
  ECHODESK_WINDOWS_CERTIFICATE_PASSWORD
  ECHODESK_WINDOWS_CERTIFICATE_SHA1
  ECHODESK_WINDOWS_EXPECTED_PUBLISHER
)
secret_names="$(gh secret list --repo "${repo}" --env "${environment}" \
  --json name --jq '.[].name')"
for name in "${required[@]}"; do
  grep -Fxq "${name}" <<<"${secret_names}"
done
```

四个 secret 必须只放在 `desktop-release-windows` environment，不得复制到 repository
secrets、代码、本地 `.env`、日志或发布说明。名称存在只证明配置槽位存在；PFX 内容、私钥、
thumbprint 和 publisher 的一致性仍由正式 workflow 的 import/preflight 验证。

### 1.3 只构建 main 的精确提交

先取得并记录远端 `main` 的精确 SHA，并确认 `.github/workflows/ci.yml` 对该 SHA 的
`push/main` workflow run 整体成功，且该 run 内 required `check` job 已经
`completed/success`：

```bash
git fetch origin main
release_sha="$(git rev-parse origin/main)"
test "${#release_sha}" -eq 40
ci_run="$(gh api \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/yoligehude14753/echo-demo/actions/workflows/ci.yml/runs?branch=main&event=push&status=completed&head_sha=${release_sha}&per_page=100" \
  --jq '[.workflow_runs[] | select(.head_branch == "main" and .event == "push" and .status == "completed" and .conclusion == "success" and .path == ".github/workflows/ci.yml")] | sort_by(.id) | last')"
jq -e --arg sha "${release_sha}" '
  .head_sha == $sha and .head_branch == "main" and .event == "push"
  and .status == "completed" and .conclusion == "success"
' <<<"${ci_run}" >/dev/null
ci_run_id="$(jq -r '.id' <<<"${ci_run}")"
gh api \
  "repos/yoligehude14753/echo-demo/actions/runs/${ci_run_id}/jobs?filter=latest&per_page=100" \
  --jq 'any(.jobs[]; .name == "check" and .status == "completed" and .conclusion == "success")' |
  grep -Fxq true

gh workflow run build-desktop-release-candidates.yml \
  --repo yoligehude14753/echo-demo \
  --ref main \
  -f release_sha="${release_sha}"
gh run list \
  --repo yoligehude14753/echo-demo \
  --workflow build-desktop-release-candidates.yml \
  --limit 1
```

工作流在任何签名 environment 之前使用最小 `actions: read` 权限重复查询指定 `ci.yml`，只接受
同一 `release_sha` 的 `push/main` 且整个 workflow run 为 `completed/success`，随后再查询该 run
内的 `check` job。仍要求 `GITHUB_REF=refs/heads/main`、输入是小写 40 位 SHA、checkout HEAD
等于输入，且输入等于触发本次 run 的 `GITHUB_SHA`。PR run、其他 workflow 的同名 job、部分
job 失败、旧 SHA、pending/cancelled/failed 结果都不能授权签名。

### 1.4 必须同时存在的候选证据

macOS 候选必须包含：

```text
EchoDesk-<version>-arm64.dmg
EchoDesk-<version>-arm64.dmg.blockmap
EchoDesk-<version>-arm64-mac.zip
EchoDesk-<version>-arm64-mac.zip.blockmap
latest-mac.yml
EchoDesk-SBOM.cdx.json
SHA256SUMS-macOS.txt
```

Windows 候选必须包含：

```text
EchoDesk.Setup.<version>.exe
EchoDesk.Setup.<version>.exe.blockmap
EchoDesk-<version>-win-x64.zip
latest.yml
EchoDesk-SBOM.cdx.json
SHA256SUMS-Windows.txt
```

这六个文件是一个不可拆分的集合：EXE 是已签名 NSIS installer，`.exe.blockmap` 与
`latest.yml` 是 updater 合同，ZIP 是已签名 portable 内容，SBOM 绑定锁文件与 frozen backend
实际依赖，`SHA256SUMS-Windows.txt` 精确覆盖前五项。GitHub build provenance attestation 不是
伪装成第七个普通文件；它必须分别绑定上述六个最终字节对象。Actions artifact 的外层 ZIP 只
是候选传输容器，不能代替内部文件、hash 或 attestation，也不能整体作为 Release 资产上传。

macOS 在真实 hosted macOS runner 上完成 Developer ID 签名、notarization、ticket staple、
Gatekeeper 检查和只读 DMG 安装态 smoke；作为 updater 主载荷的最终 ZIP 还必须重新解压，
对其中的 App 与 bundled backend 核验同一 Developer ID/Team，并从该 ZIP 内 App 完成 lifecycle
与持久化 smoke。Windows 在真实 hosted Windows runner 上完成 Authenticode/RFC 3161 timestamp
验证；NSIS 静默安装后、首次执行前必须对实际落盘的 App 与 bundled backend 再次核验同一
thumbprint、publisher、证书链和 RFC 3161 timestamp，完成安装态 smoke 后再受控卸载。portable
ZIP 解压后的 App 与 bundled backend 也必须分别重新验签，再从解压目录执行 smoke。只验证
`win-unpacked`、原始 installer 或 DMG 不能替代安装后文件和最终 ZIP 字节内容的验证。
两个 job 都为所有 updater 资产和 SBOM 生成 SHA-256 manifest，并生成 GitHub build provenance
attestation。Desktop SBOM 必须同时绑定 Python runtime lock、desktop npm lock，以及 frozen
backend 实际打包的 `ppt_ib_deck/package-lock.json`。

下载候选后再次独立验证；Windows 六个对象缺一、manifest 多一项/少一项、任一 hash 不符或
任一 attestation 无法验证，都必须拒绝整套候选：

```bash
shasum -a 256 -c SHA256SUMS-macOS.txt
sha256sum --check --strict SHA256SUMS-Windows.txt

version="0.3.2"
windows_subjects=(
  "EchoDesk.Setup.${version}.exe"
  "EchoDesk.Setup.${version}.exe.blockmap"
  "EchoDesk-${version}-win-x64.zip"
  latest.yml
  EchoDesk-SBOM.cdx.json
  SHA256SUMS-Windows.txt
)
for asset in "${windows_subjects[@]}"; do
  test -s "${asset}"
  gh attestation verify "${asset}" --repo yoligehude14753/echo-demo
done
```

只有候选 run 的两个 job、安装态 smoke、hash、SBOM、attestation 全部通过，并且独立审计确认
候选 SHA 正确后，才可以另行创建 prerelease/release。不得上传 CI 中的
`echodesk-macos-arm64-adhoc-test` 或 `echodesk-windows-unsigned-test`。

Windows 失败语义必须保持明确且关闭式：

| 失败点 | 必须结果 | 禁止降级 |
|---|---|---|
| environment 不存在或保护规则不完整 | 不触发正式候选；先完成服务端配置 | 自动创建无保护 environment |
| 四个 secret 任一缺失 | 在依赖安装和打包前列出全部缺项并失败 | 改跑 unsigned 构建 |
| PFX、thumbprint、publisher 或 private key 不匹配 | import/preflight 失败，零候选 | 忽略发布者或换临时证书 |
| installer/App/backend 签名链或 RFC 3161 timestamp 失败 | 签名验证失败，零候选 | 只验证其中一个文件 |
| EXE/ZIP/`latest.yml`/SBOM/checksum/provenance 任一缺失 | 整套候选拒绝 | 上传已有子集 |
| 安装态或便携 smoke 失败 | 整套候选拒绝 | 复用 run 29243474851 的 unsigned smoke |

## 2. Android secrets 一次性迁移与清理

迁移工作流 `.github/workflows/migrate-android-release-secrets.yml` 只在迁移完成前存在，严格拆成：

1. `copy`：复制到 `android-release`，repository 源 secret 原样保留。
2. 正式 Android workflow：真实读取目标 environment，完成签名、升级测试、attestation 和候选上传。
3. `cleanup`：同时验证 copy run 与后发生的正式签名 run，才删除 repository 源 secret。

复制阶段绝不删除；目标 environment 只出现同名 secret 不构成值正确的证明。

### 2.1 迁移前门禁

1. 确认工作流已合入 `main`；`android-release` 与 `android-release-migration` 都只允许 `main`、
   需要 reviewer 且禁止 admin bypass。
2. 创建一次性、只限 canonical repository、仅具备本次 Actions secret/environment 管理所需
   最小权限的 token，只放在 `android-release-migration` environment secret
   `PUBLIC_RELEASE_TOKEN`；不得放在 repository、目标 `android-release`、本地 `.env`、命令行
   历史或日志。
3. `android-release-migration` 不得包含 10 个 signing secret 名称，避免 environment precedence
   覆盖 repository 源值。确认 10 个源 secret 仍是 repository secrets，并运行源码门禁：

```bash
python3 scripts/check-ci-action-pins.py
npm --prefix desktop run test:electron
gh secret list --repo yoligehude14753/echo-demo
```

### 2.2 执行并验证迁移

```bash
git fetch origin main
release_sha="$(git rev-parse origin/main)"
test "${#release_sha}" -eq 40
gh workflow run migrate-android-release-secrets.yml \
  --repo yoligehude14753/echo-demo \
  --ref main \
  -f phase=copy \
  -f release_sha="${release_sha}"
gh run list \
  --repo yoligehude14753/echo-demo \
  --workflow migrate-android-release-secrets.yml \
  --limit 1
```

记录成功 copy run 的数字 ID。此时逐项确认目标 secret 已存在，同时 repository 源 secret
仍全部存在；任一源已消失都必须停止：

```bash
required=(
  ECHODESK_ANDROID_LEGACY_KEYSTORE_BASE64
  ECHODESK_ANDROID_LEGACY_KEY_ALIAS
  ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD
  ECHODESK_ANDROID_LEGACY_KEY_PASSWORD
  ECHODESK_ANDROID_LEGACY_CERT_SHA256
  ECHODESK_ANDROID_CURRENT_KEYSTORE_BASE64
  ECHODESK_ANDROID_CURRENT_KEY_ALIAS
  ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD
  ECHODESK_ANDROID_CURRENT_KEY_PASSWORD
  ECHODESK_ANDROID_CURRENT_CERT_SHA256
)
environment_names="$(gh secret list \
  --repo yoligehude14753/echo-demo \
  --env android-release \
  --json name \
  --jq '.[].name')"
repository_names="$(gh secret list \
  --repo yoligehude14753/echo-demo \
  --json name \
  --jq '.[].name')"
for name in "${required[@]}"; do
  grep -Fxq "${name}" <<<"${environment_names}"
  grep -Fxq "${name}" <<<"${repository_names}"
done
```

随后按 §1.2 确认同一 SHA 的完整 CI workflow run，再触发正式签名候选并记录成功 run ID：

```bash
git fetch origin main
release_sha="$(git rev-parse origin/main)"
test "${#release_sha}" -eq 40
gh workflow run build-android-tv-release.yml \
  --repo yoligehude14753/echo-demo \
  --ref main \
  -f release_sha="${release_sha}"
```

Android 签名 workflow 也会在进入 `android-release` environment 前，以最小 `actions: read`
权限重复执行 exact-SHA `ci.yml push/main` 整体绿灯校验。确认受保护 environment
中的 secret 能完成双签名、真实 emulator 覆盖安装、
`SHA256SUMS-Android.txt`、`EchoDesk-<version>-Android-SBOM.cdx.json`、provenance 和 artifact
上传。下载候选后必须再次运行：

```bash
sha256sum --check --strict SHA256SUMS-Android.txt
gh attestation verify EchoDesk-<version>-android.apk \
  --repo yoligehude14753/echo-demo
gh attestation verify EchoDesk-<version>-Android-SBOM.cdx.json \
  --repo yoligehude14753/echo-demo
```

仅在上述正式 run 成功后，执行独立 cleanup phase：

```bash
gh workflow run migrate-android-release-secrets.yml \
  --repo yoligehude14753/echo-demo \
  --ref main \
  -f phase=cleanup \
  -f release_sha="${release_sha}" \
  -f copy_run_id="${copy_run_id}" \
  -f validated_run_id="${validated_run_id}"
```

cleanup 会验证两个 run 都属于 canonical repository 和同一精确 SHA、copy job 成功、正式签名
run 在 copy 完成后才启动、正式签名 job 成功且候选 artifact 仍存在。任一证明缺失都失败关闭。

### 2.3 成功后的强制清理

Android 签名工作流验证通过后，在同一个收尾 PR 中：

1. 删除 `.github/workflows/migrate-android-release-secrets.yml`。
2. 从 `desktop/electron/tests/release-gates.test.cjs` 删除读取 `migration` 文件及其整段一次性
   workflow 断言；保留正式 Android release workflow 的长期断言。
3. 运行 `python3 scripts/check-ci-action-pins.py` 与
   `npm --prefix desktop run test:electron`，合入 `main`。
4. 删除 migration environment 中的一次性 token：

```bash
gh secret delete PUBLIC_RELEASE_TOKEN \
  --repo yoligehude14753/echo-demo \
  --env android-release-migration
if gh secret list --repo yoligehude14753/echo-demo \
  --env android-release-migration --json name --jq '.[].name' |
  grep -Fxq PUBLIC_RELEASE_TOKEN; then
  exit 1
fi
```

5. 在 token 的签发端立即 revoke/delete 该 token。删除 Actions secret 只移除仓库副本，不能替代
   签发端吊销；保留 token 已吊销的审计证据，不记录 token 值。
6. 最后确认仓库不再包含一次性 workflow、测试不再引用它、repository secrets 不含 Android
   signing 名称或 `PUBLIC_RELEASE_TOKEN`，而 `android-release` environment 仍精确包含 10 个
   signing secret 名称。
