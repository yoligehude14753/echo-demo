# EchoDesk 公共后端安全部署与回滚

本 SOP 使用 [`scripts/public-backend-deploy.sh`](../../scripts/public-backend-deploy.sh) 管理公共后端。目标是让代码、数据和凭证拥有不同生命周期：代码进入不可变的 versioned release；canary 使用独立端口、数据库和数据目录；生产切换只替换 `current` symlink；数据库与配置在停服后做一致快照。

> **Breaking cutover：最低公共客户端版本 0.3.2。** 当前稳定版 v0.2.50 没有
> enrollment / bearer session 协议，切到 0.3.2 公共后端后会被
> `426 client_upgrade_required` fail closed。首次 bootstrap 前必须先公开可安装且已验证的
> 0.3.2 GitHub prerelease（渠道可标 prerelease，包内必须自报 `0.3.2`），并完成所有仍声明支持的平台公共 transport 验证；不得把 v0.2.50 标记为
> 兼容通过，也不得先切公网再补客户端。

## 安全不变量

- release、canary、backup、deployment record 一旦存在就拒绝覆盖。
- `.env*`、SQLite 文件、venv、cache 不会从源码复制进 release。
- secret env 永远位于 release 外，要求普通文件且权限不宽于 `0600`；脚本不 `source`、不 `cat`、不打印其内容。
- 所有会构建、切换或回滚 release 的命令要求 Python 3.11+；在当前服务器上显式传入 `--python /usr/bin/python3.11`，不要依赖指向 Python 3.10 的 `python3`。
- production unit 使用 `systemctl --user`，不是系统级 `sudo systemctl`；unit 路径是 `~/.config/systemd/user/<service>.service`。
- canary 从生产 SQLite 做在线一致性副本，但使用独立 DB、storage、RAG、skill build、workspace state 和端口。
- legacy bootstrap 的 canary marker 会记录 SQLite 副本的源路径；bootstrap 只接受明确来自 `--legacy-db` 的有效 marker。
- bootstrap 对 legacy storage、RAG 和过滤后的 user root 做运行中预同步；停服后再做最终同步。所有 `rsync --delete` 只作用于新数据目录，legacy 源永远不写入。
- bootstrap 停止 production 前先禁用 unit；后续任何异常都会再次 stop + disable，绝不启动不具备隔离能力的 legacy。机器重启也不会绕过 fail-closed。
- production cutover 必须使用 release 内只读的 `echodesk-ingress-gate.py`。未显式传 `--ingress-gate` 时脚本默认解析当前 `--release` 内的 helper；默认路径不存在、权限不安全或 gate 状态不可证明时，会在任何 stop 前拒绝执行。
- gate token 固定在 `ROOT/shared/deployment-gate.token`：closed 时是 owner-only 0600 随机 token 文件，业务 HTTP 返回 503、WebSocket 返回 1013；`/healthz`、`/readyz` 保持可用。本机 isolation smoke 通过文件读取 token 并携带 header，脚本、日志和 deployment record 都不读取或保存 token 内容/摘要。
- 新 target 始终保持 start-disabled，并在 closed gate 后手动启动；loopback health、ready、PID cwd、release 完整性和带 gate header 的 isolation smoke 全部通过后才原子 open gate。随后经公网执行一次不带 header 的 isolation smoke，成功后才 enable unit。
- bootstrap 使用 SQLite backup API 把 legacy DB 复制到全新的 production DB；legacy DB、storage、RAG、user root 保持原样，供审计和离线恢复。
- release manifest 同时绑定源码摘要、实际 venv installed distributions、每个 wheel `RECORD` 和对应安装文件聚合；prepare 最后把 release 全树封为不可写。bootstrap、promote、rollback 与恢复路径都会重新计算并拒绝漂移。
- deployment record 绑定 canonical deploy root、service、port、production DB、runtime env、unit、ingress helper、gate-file 路径、public URL、release manifest 和相应摘要；rollback 在 stop 前验证 status=active、backup containment、CLI 完全匹配和防重放状态。
- promote 在停生产服务后才备份 DB/config，随后原子替换 symlink。目标进程必须同时通过 `/healthz`、`/readyz`、`/proc/<MainPID>/cwd` release 校验和双身份 isolation smoke。
- promote 的 stop→backup→switch→record 全程有 EXIT 恢复闸；中途异常会尝试恢复旧 release。若旧 release 无法证明健康且隔离，服务保持停止，不把不安全版本重新暴露。
- rollback 会恢复部署前 DB 快照，因此会丢弃 promote 之后产生的写入。执行前另做一份“回滚前”安全快照；若回滚目标验证失败，自动恢复回滚前状态。

## 目录

默认根目录是 `~/echodesk-public`：

```text
~/echodesk-public/
├── releases/<release-id>/       # backend、锁定依赖、PPT Node runtime、RELEASE.json
├── current -> releases/<id>     # 同文件系统原子替换
├── canaries/<release-id>/       # canary DB、data、unit、PASSED.json
├── backups/<deployment-id>/     # 停服后的 DB + secret/path config 快照
├── deployments/<id>.json        # 成功 bootstrap/promote 记录
├── deployments/latest.json      # 指向最近成功记录
└── shared/
    ├── runtime.env              # secret，0600，永不进入 release
    ├── production.env           # 仅路径/端口，0600
    ├── deployment-gate.token    # 仅 closed 时存在；0600；绝不读取/记录内容
    └── data/                    # production data root
```

生产 `PUBLIC_HTTP_URL`、`PUBLIC_WS_URL` 和 API keys 放在 `runtime.env`。`production.env` 不覆盖公网 URL；canary env 才强制使用 loopback URL。

首次 bootstrap 会写入 `deployment_kind=bootstrap` 的部署记录，其中保存 canonical bindings、非 token 文件摘要、release 和恢复策略，不保存 env 或 gate token 内容。对该记录执行 `rollback` 会恢复 legacy unit 证据后立即 persistent-mask，服务保持 stopped + disabled + masked；它不会把不隔离的 legacy 重新暴露到公网。

## 前置检查

```bash
chmod 600 ~/echodesk-public/shared/runtime.env

# user manager 必须在登出后仍可运行；首次由管理员检查/配置
loginctl show-user "$USER" -p Linger
# 如为 no：sudo loginctl enable-linger "$USER"

systemctl --user show echodesk-demo-backend.service \
  -p LoadState,ActiveState,FragmentPath,WorkingDirectory

scripts/public-backend-deploy.sh --help
scripts/public-backend-deploy.sh --python /usr/bin/python3.11 self-test
```

不要启用 `set -x`；shell trace 会把 systemd 读取的环境变量传播路径扩大。

## 首次从 legacy 迁移到 versioned layout

当前服务器的 legacy `0.2.49` 不是可制作为 release 的 clean checkout，也不具备 `/readyz` 和公共身份隔离。因此不要尝试把它伪造成 baseline；首次切换使用 `bootstrap`，把首个已经通过隔离 canary 的安全版本直接设为 `current`。

开始以下步骤前，先确认 GitHub prerelease 中存在与目标提交绑定、且包内自报 `0.3.2` 的客户端资产。
Android 与 TV 必须分别完成真实覆盖升级和公共入口 transport smoke；每个仍公开声明支持的
Desktop OS 也必须完成对应安装态验证。无法提供资产或验证的平台必须在切流前显式撤下支持
声明，不能用另一个平台的一次安装代替。`/bootstrap` 必须返回
`minimum_client_version=0.3.2`；缺失/非法/低版本请求必须返回带最低版本和升级链接的
`426 client_upgrade_required`，受支持版本但无 session 的业务请求必须返回
`401 session_required`。任一条件不满足都停止切流。

当前 legacy 路径如下。这些路径是迁移输入，不是脚本的通用默认值：

```text
secret env: /home/ai/echodesk-demo-backend/.env
user root:  /home/ai/.echodesk-demo
storage:    /home/ai/.echodesk-demo/storage
RAG:        /home/ai/.echodesk-demo/rag_index
SQLite:     /home/ai/.echodesk-demo/echodesk.db
unit:       /home/ai/.config/systemd/user/echodesk-demo-backend.service
```

先选择已经通过 CI 的精确提交，并在服务器上创建干净、detached 的源码目录。禁止从本地 dirty worktree 直接 rsync：

```bash
SHA=<full-ci-passed-git-sha>
SHORT_SHA="$(printf '%s' "$SHA" | cut -c1-12)"
RELEASE="v0.3.2-$SHORT_SHA"
ROOT=/home/ai/echodesk-public
SRC="/home/ai/echodesk-src/$SHA"
PY=/usr/bin/python3.11
DEPLOY="$SRC/scripts/public-backend-deploy.sh"
ENV_FILE="$ROOT/shared/runtime.env"
NEW_DB="$ROOT/shared/data/echodesk.db"
PUBLIC_URL=https://echodesk.yoliyoli.uk
LEGACY_ENV=/home/ai/echodesk-demo-backend/.env
LEGACY_ROOT=/home/ai/.echodesk-demo
LEGACY_DB="$LEGACY_ROOT/echodesk.db"

git clone --filter=blob:none --no-checkout \
  https://github.com/yoligehude14753/echo-demo.git "$SRC"
git -C "$SRC" fetch origin "$SHA"
git -C "$SRC" checkout --detach "$SHA"
test -z "$(git -C "$SRC" status --porcelain)"
test "$(git -C "$SRC" rev-parse HEAD)" = "$SHA"
```

准备 immutable release。先 dry-run，再实际执行：

```bash
"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$NEW_DB" \
  --release "$RELEASE" --source "$SRC" --dry-run prepare

"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$NEW_DB" \
  --release "$RELEASE" --source "$SRC" prepare

INGRESS_GATE="$ROOT/releases/$RELEASE/scripts/echodesk-ingress-gate.py"
test -x "$INGRESS_GATE"
```

复制 secret env 时只复制文件，不读取或打印内容。不要启用 shell trace：

```bash
install -d -m 0700 "$ROOT/shared"
install -m 0600 "$LEGACY_ENV" "$ENV_FILE"
test "$(stat -c '%a' "$ENV_FILE")" = 600
```

canary 必须显式使用 legacy DB。`canary` 会通过 SQLite backup API 在线克隆 DB，使用独立端口和数据目录，并把源 DB 路径写入不可变 `PASSED.json`：

```bash
"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$LEGACY_DB" \
  --release "$RELEASE" --canary-port 8870 --dry-run canary

"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$LEGACY_DB" \
  --release "$RELEASE" --canary-port 8870 canary
```

canary 成功且保持 active 后执行 bootstrap。注意 `--db` 此时改为全新的 `NEW_DB`；bootstrap 会自动完成运行中预同步、disable/stop gate、停服后最终同步、SQLite 一致副本、原子 unit 切换和 production isolation smoke：

```bash
"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$NEW_DB" \
  --release "$RELEASE" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  --legacy-env "$LEGACY_ENV" --legacy-db "$LEGACY_DB" \
  --legacy-data-root "$LEGACY_ROOT" \
  --dry-run bootstrap

"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$NEW_DB" \
  --release "$RELEASE" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  --legacy-env "$LEGACY_ENV" --legacy-db "$LEGACY_DB" \
  --legacy-data-root "$LEGACY_ROOT" \
  bootstrap
```

bootstrap 不停止承载其他域名的共享 cloudflared。close gate 后 legacy 尚未停止时，旧版本还不识别 gate 文件，因此脚本会紧接着 disable/stop/mask legacy 并验证 8769 无 listener；target 启动后由新 middleware 拒绝业务流量。本机带 token smoke 和公网无 token smoke 均通过后才 enable。

若 bootstrap 在 stop gate 后失败，trap 会 close gate、stop + disable + persistent-mask service、验证 8769 无 listener、持久化 `PHASE.json` 并写入 `FAILED.json`。不要手工 start legacy。旧 DB 和 5GB 级 storage/RAG/user 数据从未被新版本覆盖，可供离线调查。

有停服 SQLite snapshot 时可以安全 resume；abort 不要求 snapshot。两者都必须复用完全相同的 CLI bindings：

```bash
DEPLOYMENT=<bootstrap-id>

"$DEPLOY" \
  --python "$PY" --root "$ROOT" --env-file "$ENV_FILE" --db "$NEW_DB" \
  --release "$RELEASE" --deployment "$DEPLOYMENT" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  --legacy-env "$LEGACY_ENV" --legacy-db "$LEGACY_DB" \
  --legacy-data-root "$LEGACY_ROOT" \
  --dry-run bootstrap-resume

# 去掉 --dry-run 才实际 resume；如决定安全退场，把命令改成 bootstrap-abort。
```

resume 会先重新 close gate 并 mask service，保存任何 partial new DB，然后从停服 snapshot 恢复、重做最终数据同步与完整验证。abort 只移除匹配的新 `current` symlink，保留所有新旧数据和证据，legacy 继续 stopped + masked。

## 正常发布

以下命令都建议先加 `--dry-run` 检查路径、release id、service 和端口。

```bash
ROOT=/home/ai/echodesk-public
ENV_FILE="$ROOT/shared/runtime.env"
DB="$ROOT/shared/data/echodesk.db"
RELEASE=v0.3.2-<git-sha>
PY=/usr/bin/python3.11
PUBLIC_URL=https://echodesk.yoliyoli.uk
INGRESS_GATE="$ROOT/releases/$RELEASE/scripts/echodesk-ingress-gate.py"

# 1. 新 release：目标目录必须不存在；clean checkout 默认强制
scripts/public-backend-deploy.sh \
  --python "$PY" \
  --root "$ROOT" --env-file "$ENV_FILE" --db "$DB" \
  --release "$RELEASE" --source /path/to/clean/echo prepare

# 2. 独立 canary：默认 8870；成功后保持 active 并写 PASSED.json
scripts/public-backend-deploy.sh \
  --python "$PY" \
  --root "$ROOT" --env-file "$ENV_FILE" --db "$DB" \
  --release "$RELEASE" --canary-port 8870 canary

# 3. promote：重新核验 canary、停生产、快照、原子切换、生产 isolation smoke
scripts/public-backend-deploy.sh \
  --python "$PY" \
  --root "$ROOT" --env-file "$ENV_FILE" --db "$DB" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  --release "$RELEASE" promote

# 4. 非秘密状态
scripts/public-backend-deploy.sh --root "$ROOT" status
```

`public-isolation-smoke.py` 会清理 RAG 文档、会议 outputs 和 session family，但 meeting/workflow 审计记录没有公开删除接口，会保留带 `isolation-<smoke-id>` 前缀的记录。这是安全门禁的预期审计痕迹。

## 一键回滚

默认回滚最近一次成功 deployment，也可以显式传 id：

```bash
scripts/public-backend-deploy.sh \
  --python /usr/bin/python3.11 \
  --root "$ROOT" --env-file "$ENV_FILE" --db "$DB" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  --dry-run rollback

scripts/public-backend-deploy.sh \
  --python /usr/bin/python3.11 \
  --root "$ROOT" --env-file "$ENV_FILE" --db "$DB" \
  --ingress-gate "$INGRESS_GATE" --public-url "$PUBLIC_URL" \
  rollback

# 或：--deployment 20260712T010203Z-v0.3.2-abcdef
```

回滚不是“只换代码”：它恢复该 deployment 的切换前 DB/config。这样旧 schema 不会读取新 schema 写入的数据，但 promote 后的新写入会被替换。若业务必须保留这些写入，应保持当前安全版本运行，从 `rollback-*` 安全快照做离线数据迁移，不能直接让旧版本读取新 DB。

## 故障与手工恢复

- canary 失败：生产不受影响；证据保留在 `canaries/<release>`，不要复用同一 release id。
- bootstrap 失败：gate 保持 closed，service 保持 stopped + disabled + masked，legacy 不会被自动恢复或在重启后启动；检查 `PHASE.json`、`FAILED.json`，选择 `bootstrap-resume` 或 `bootstrap-abort`。
- 对 active bootstrap record 执行 rollback：必须同时传入记录绑定的 legacy 参数、gate helper 和 public URL。脚本保存当前安全版本 DB/config，恢复 legacy unit 证据后立即 persistent-mask 并验证无 listener；该动作是安全退场，不是把 legacy 重新上线。
- promote 自动恢复成功：命令仍以非零退出，旧 release 已重新通过 health/cwd/isolation。
- promote 自动恢复无法证明隔离：生产保持停止。使用 `backups/<deployment-id>/echodesk.db`、`config/` 和 `previous-release` 手工恢复，不要绕过 isolation smoke 强行 start。
- rollback 失败：脚本尝试恢复 `backups/rollback-*/` 中的回滚前状态；验证不通过同样保持停止。
- secret env 丢失或权限过宽：脚本在任何 stop 之前拒绝继续。

所有恢复操作完成后都检查：

```bash
systemctl --user is-active echodesk-demo-backend.service
systemctl --user show echodesk-demo-backend.service -p MainPID,WorkingDirectory
curl -fsS http://127.0.0.1:8769/healthz
curl -fsS http://127.0.0.1:8769/readyz
```

禁止删除旧 release、canary 或 backup 来“清理失败”；先保留审计证据，另开新的 release id 重试。
