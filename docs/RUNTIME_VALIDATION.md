# EchoDesk 运行态验证

> 最近验证：2026-06-02 14:00 左右，本机 `/Applications/EchoDesk.app` + `http://127.0.0.1:8769`

## 一键冒烟脚本

脚本位置：

```bash
backend/scripts/stress/runtime_smoke.py
```

默认验证：

- `/healthz` 后端存活
- `/healthz/full` DB + 远程探针结构
- `/recap/today` 今日回顾 + `todos` 字段
- `/agent/run` Agent SSE：必须有 `delta` / `final` / `done`，并记录首个 `delta` 延迟

命令：

```bash
cd backend
.venv/bin/python scripts/stress/runtime_smoke.py
```

需要验证真实产物生成时显式加 `--artifact-type`：

```bash
cd backend
.venv/bin/python scripts/stress/runtime_smoke.py --artifact-type html
```

## 本次结果

默认运行态 smoke：

- `healthz`: OK，约 3ms
- `healthz_full`: OK，约 2ms，远程探针包含 `heyi_llm_fast` / `heyi_stt_firered` / `heyi_tts_qwen3` / `tavily` / `yunwu_llm_main`
- `recap_today`: OK，约 4.0s，`ambient=37`，`meetings=1`，`todos=1`
- `agent_run`: OK，约 1.1s，`first_delta_ms≈1004`，事件包含 `plan` / `delta` / `final` / `done`

带 HTML 产物的 smoke：

- `healthz`: OK
- `healthz_full`: OK
- `recap_today`: OK，约 5.0s
- `agent_run`: OK，`first_delta_ms≈1705`
- `artifact_html`: OK，约 30.3s，`artifact_id=html-a60c8fae52`，`size_bytes=5929`，`llm_chunk=92`，最终阶段 `saved`

回顾缓存优化后复跑：

- 连续两次 `/recap/today`：第一次 `cached=false`，第二次 `cached=true`
- 默认 smoke 中 `recap_today`: OK，约 2ms，说明启动提醒 / 手动今日回顾短时间重复触发时不会重复跑 LLM
- `agent_run`: OK，`first_delta_ms≈1873`

## 当前结论

这套 smoke 证明当前已安装的 EchoDesk 具备：

- 基础服务健康
- 今日回顾 / 待办抽取接口可用
- Agent SSE 简单问答可出首字并正常收尾
- HTML 产物生成链路可真实落盘
- `/recap/today` 有短 TTL 缓存，能避免启动提醒与用户手动回顾重复烧模型

它不能替代长时间真人语音 soak test，也不能覆盖所有 PPT/Word/Excel 视觉质量，但可以作为每轮开发后的快速运行态回归门槛。

## 语音唤醒文本层基线

新增测试：

```bash
cd desktop
npx playwright test tests/e2e/voice-wake-baseline.spec.ts tests/e2e/voice-wake.spec.ts --reporter=list
```

覆盖：

- 唤醒词 STT 文本变体召回：`echo` / `echodesk` / `pico` / `aiko` / `诶口` / `汉宜口` / `汉语狗` / `嘿依co` 等
- 常见口语误唤醒防护：`一口饭` / `eco 系统` / `ego` / `iQOO 手机` / `pico VR 设备` / `aiko 名字` / `爱狗人士`
- 跨 chunk 唤醒词单独出现
- 免唤醒续聊判定
- 今日回顾语音意图

本次结果：

- `voice-wake-baseline.spec.ts` + `voice-wake.spec.ts`: 77 passed
- baseline 首跑发现 4 个误触：`iqoo 这款手机怎么样`、`pico 是一款 VR 设备`、`aiko 这个名字挺可爱`、`爱狗人士很多`
- 已修复：加入歧义品牌/名字提及过滤与 `爱狗/艾狗` blocklist
- 复跑：77 passed
