# ASR Scheduler / Provider Failover Contract

> Task: `echo-core-asr-scheduler-failover`
> Owner: ASR line
> Evidence date: 2026-07-14 (Asia/Shanghai)

## Scope and hard boundaries

This candidate owns only the backend STT/ASR port-adapter-scheduler boundary,
provider routing, ASR HTTP error semantics, lifecycle, and ASR-focused tests.
The ASR scheduler queue is a separate domain from the v0.3.3 sync outbox:
there is no shared table, cursor, retry policy, or backpressure. The only
allowed cross-domain input is already-derived tenant/device scope plus typed
observability context. A completed ASR result is returned to the existing
application use case; that use case decides whether a transcript domain event
is emitted.

The following files are intentionally outside this candidate until Sol grants
an exact-path integration approval:

- `backend/app/config.py`
- `backend/app/main.py`
- `backend/app/api/capture.py`
- `backend/app/use_cases/ambient_capture.py`
- `backend/app/schemas/capture.py`
- `backend/app/schemas/events.py`
- all migrations and lockfiles

The capture line retains ownership of ambient admission, single-flight, and
capture stats. This candidate must not duplicate those state sources.

## Current source facts (baseline SHA)

Baseline: `9b5673ad104fbc015134e304b265871541eea1ca`.

| ID | Source fact | Evidence | Consequence |
|---|---|---|---|
| F-ASR-001 | `STTPort` exposes only async `transcribe(...)` and returns transcript segments. | `backend/app/ports/stt.py` | Scheduler must preserve the port and must not leak provider SDK types into the use case. |
| F-ASR-002 | FireRed is the current adapter; it creates an async HTTP client per call and has no circuit or bounded queue. | `backend/app/adapters/stt/firered.py` | Queue, timeout, retry, and circuit state belong above the adapter. |
| F-ASR-003 | Meeting chunk processing starts STT and diarization tasks directly, then gathers them. | `backend/app/use_cases/meeting_pipeline.py` | Direct STT calls need an explicit integration change; this isolated candidate does not silently rewrite capture ownership. |
| F-ASR-004 | Ambient capture has its own `_stt_lock` and converts adapter failures to capture-local failure states. | `backend/app/use_cases/ambient_capture.py` | ASR scheduler work cannot modify ambient single-flight or capture stats in this task. |
| F-ASR-005 | `/healthz/full` reads cached background probe results and does not perform a transcription. | `backend/app/api/health.py` | ASR readiness must be an additive, non-blocking view of scheduler/provider state, not a claim that a live transcription succeeded. |
| F-ASR-006 | The baseline contains no active local model adapter; FunASR was removed from the dependency path. | `backend/requirements.txt` and `backend/app/adapters/stt/` | A local adapter must fail typed and closed when its optional model/runtime is unavailable; it must not pretend to be live-ready. |

## StepFun protocol evidence

Official documentation was checked on 2026-07-14:

- [ASR WebSocket stream](https://platform.stepfun.com/docs/zh/api-reference/audio/asr-stream)
- [ASR SSE](https://platform.stepfun.com/docs/zh/api-reference/audio/asr-sse)

Confirmed from those documents:

- WebSocket stream uses `wss://api.stepfun.com/v1/realtime/asr/stream`, Bearer
  authentication, session events, audio append/commit, delta/completed
  transcription events, and provider error events. The documented delta text
  is cumulative and may include a correction tail (`stash`), so the adapter
  must expose typed partial events rather than append raw deltas blindly.
- SSE is a separate HTTP POST transport at
  `https://api.stepfun.com/v1/audio/asr/sse`; it accepts base64 audio and
  server-sent `transcript.text.delta`, `transcript.text.done`, and `error`
  events. It is a one-shot job, not a WebSocket session.
- The documents describe authentication, event shapes, and provider error
  types/codes, but do not establish this application's tenant quotas,
  `Retry-After`, scheduler queue, circuit, or failover guarantees.

The adapter contract therefore names two capabilities explicitly:

- `transport=sse_one_shot`: bounded request queue, deadline, per-request
  semaphore, and safe fallback only before a successful one-shot result.
- `transport=websocket_stream`: session-level admission and maximum concurrent
  sessions, bounded send/backpressure behavior, idle and maximum-duration
  deadlines, and fail-closed mid-stream behavior unless a finalized segment
  checkpoint plus idempotency boundary is present.

The two transports share provider health/circuit state only. They never share
an unfinished audio payload or in-flight session state. Both produce one
typed final result; typed partial/delta events are separate from metrics and
logs. Transcript text is never recorded in metrics or logs.

Live StepFun authentication and transcription are **BLOCKED** without a
credential. Fake-provider protocol tests are not live-provider proof.

## Falsifiable acceptance matrix

| ID | Contract | Falsifiable check | Required evidence |
|---|---|---|---|
| A1 | Empty/invalid/silent audio is rejected before provider queue admission. | Provider call count remains zero; response is `422` with stable `asr_audio_rejected`. | Focused unit test. |
| A2 | Global concurrency and queue are bounded. | With `C` workers and queue `Q`, at most `C+Q` accepted jobs exist; next admission is `503/asr_queue_full` with `Retry-After`. | Focused unit test plus queue counters. |
| A3 | Caller/tenant rate and concurrent quota are distinct from global queue. | Scope overflow returns `429/asr_rate_limited` with `Retry-After`; it never consumes provider queue capacity. | Focused unit test. |
| A4 | Every job has an absolute deadline and cancellation. | Provider task is cancelled at deadline; caller receives `504/asr_deadline_exceeded`, not an unbounded wait. | Timeout/cancel test. |
| A5 | Selection is configuration-driven and weighted least-loaded. | Under deterministic fake loads, selection follows eligible set, weight, current load, and circuit state; no vendor name is embedded in the algorithm. | Selection test and raw load sample. |
| A6 | Safe transient failover is bounded and idempotent. | One safe first-attempt failure may use a fallback within remaining budget; one idempotency key produces one successful provider operation/result. | Failure/fallback test with provider call counts. |
| A7 | Circuit breaker is closed/open/half-open. | Threshold opens; cooldown permits one probe; probe success closes and failure reopens. | State-transition test. |
| A8 | Shutdown is graceful and cancellation-safe. | New admission stops; accepted jobs finish within grace or receive typed shutdown/deadline failure; no worker/provider task leaks. | Lifecycle test. |
| A9 | ASR readiness is non-blocking and truthful. | Readiness reports scheduler accepting, queue capacity, eligible count, auth/config readiness, and last controlled probe timestamp/result without performing transcription per request. | Integration test after shared lifecycle approval; otherwise BLOCKED. |
| A10 | StepFun SSE and WebSocket capabilities remain distinct. | Fake SSE and fake WebSocket event streams parse into typed final/partial results; transport-specific limits are enforced. | Adapter tests; live auth remains BLOCKED. |
| A11 | Local model execution is isolated. | Local adapter uses one isolated worker by default and never runs sync model inference on the event loop; missing runtime/model is typed unavailable. | Structural/fake executor test; real model proof is environment-dependent. |
| A12 | Health/circuit metrics contain no transcript content. | Snapshot includes counts/state/latency only and no partial/final text fields. | Metrics assertion and source review. |

For load evidence, the fake-provider test will report the raw sample count and
p50/p95/p99 latency in milliseconds. A green `/healthz/full`, static test, or
unsigned artifact cannot promote A9/A10/A11 to live readiness.

## Initial status

Before implementation, A1-A12 are `RED` or `BLOCKED` as described above. The
candidate may report `PASS` only for executed fake/structural tests and must
report live StepFun separately as `BLOCKED` when no credential is available.
