const { Worker, MessageChannel } = require("node:worker_threads");
const { createHash, randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { resolvePackageResource } = require("./agent-runtime/package-layout-resolver.cjs");

const MAX_FRAME_BYTES = 16 * 1024 * 1024;

class PackagedFusedWorkerError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PackagedFusedWorkerError";
    this.code = code;
  }
}

function frame(type, payload = {}, identity = {}) {
  return {
    protocolVersion: 1,
    frameId: randomUUID(),
    type,
    sentAt: new Date().toISOString(),
    payload,
    ...identity,
  };
}

function encode(frameValue) {
  const body = Buffer.from(JSON.stringify(frameValue), "utf8");
  if (body.length > MAX_FRAME_BYTES) {
    throw new PackagedFusedWorkerError("RUNTIME_FRAME_TOO_LARGE", "runtime frame exceeds the size limit");
  }
  const prefix = Buffer.allocUnsafe(4);
  prefix.writeUInt32BE(body.length, 0);
  return Buffer.concat([prefix, body]);
}

function validateFrame(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PackagedFusedWorkerError("RUNTIME_FRAME_INVALID", "runtime frame must be an object");
  }
  if (value.protocolVersion !== 1 || typeof value.frameId !== "string" || typeof value.type !== "string") {
    throw new PackagedFusedWorkerError("RUNTIME_FRAME_INVALID", "runtime frame identity is invalid");
  }
  if (!value.payload || typeof value.payload !== "object" || Array.isArray(value.payload)) {
    throw new PackagedFusedWorkerError("RUNTIME_FRAME_INVALID", "runtime frame payload is invalid");
  }
  return value;
}

function nonceProof(nonce) {
  return createHash("sha256").update(nonce, "utf8").digest("hex");
}

function readManifest(resourcesPath) {
  const root = path.resolve(String(resourcesPath || ""));
  if (!root || !path.isAbsolute(root)) {
    throw new PackagedFusedWorkerError("PACKAGE_RESOURCES_ROOT_INVALID", "packaged Resources root is required");
  }
  const manifestPath = path.join(root, "agent-runtime", "fusion-content-manifest.json");
  let realManifestPath;
  try {
    realManifestPath = fs.realpathSync.native(manifestPath);
  } catch {
    throw new PackagedFusedWorkerError("PACKAGE_MANIFEST_MISSING", "packaged fused runtime manifest is missing");
  }
  if (!realManifestPath.startsWith(`${root}${path.sep}`)) {
    throw new PackagedFusedWorkerError("PACKAGE_RESOURCE_PATH_ESCAPE", "packaged fused runtime manifest escaped Resources");
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(realManifestPath, "utf8"));
  } catch (error) {
    throw new PackagedFusedWorkerError("PACKAGE_MANIFEST_INVALID", error.message);
  }
  if (!manifest || manifest.schema_version !== 1 || !Array.isArray(manifest.files)) {
    throw new PackagedFusedWorkerError("PACKAGE_MANIFEST_INVALID", "packaged fused runtime manifest is invalid");
  }
  return { root, manifest };
}

function resourceByRole(root, manifest, role, relativePath) {
  const entries = manifest.files.filter((entry) => {
    const pathValue = String(entry.path || "");
    return entry.role === role || pathValue === relativePath || pathValue === `Resources/${relativePath}`;
  });
  if (entries.length !== 1) {
    throw new PackagedFusedWorkerError(
      entries.length === 0 ? "PACKAGE_RESOURCE_MISSING" : "PACKAGE_RESOURCE_AMBIGUOUS",
      `packaged fused runtime resource is not uniquely bound: ${relativePath}`,
    );
  }
  return resolvePackageResource(entries[0], { resourcesPath: root });
}

function createRuntimeManifest(manifest) {
  const identity = manifest.kernel_build_identity;
  if (!identity || typeof identity !== "object" || !identity.build_id) {
    throw new PackagedFusedWorkerError("PACKAGE_MANIFEST_INVALID", "kernel build identity is missing");
  }
  return {
    schemaVersion: 1,
    manifestId: String(manifest.manifest_id || ""),
    buildIdentity: {
      schemaVersion: identity.schema_version,
      kernelApiVersion: identity.kernel_api_version,
      workerProtocolVersion: identity.worker_protocol_version,
      modelSchemaVersion: identity.model_schema_version,
      grantSchemaVersion: identity.grant_schema_version,
      checkpointSchemaVersion: identity.checkpoint_schema_version,
      eventSchemaVersion: identity.event_schema_version,
      buildId: identity.build_id,
      sourceSnapshotId: identity.source_snapshot_id,
      sourceManifestSha256: identity.source_manifest_sha256,
      echoBaselineSha: identity.echo_baseline_sha,
      runtimeFingerprint: identity.runtime_fingerprint,
    },
  };
}

class PackagedFusedWorkerBridge {
  constructor(duplex, { resourcesPath, nonce }) {
    if (!duplex || typeof duplex.on !== "function" || typeof duplex.write !== "function") {
      throw new PackagedFusedWorkerError("RUNTIME_HANDLE_INVALID", "backend runtime duplex is required");
    }
    if (!nonce) throw new PackagedFusedWorkerError("RUNTIME_HANDSHAKE_FAILED", "runtime nonce is required");
    this.duplex = duplex;
    this.nonce = nonce;
    this.decoder = Buffer.alloc(0);
    this.pendingHost = new Map();
    this.active = null;
    this.ready = false;
    this.closed = false;
    const { root, manifest } = readManifest(resourcesPath);
    this.manifest = createRuntimeManifest(manifest);
    this.workerResource = resourceByRole(root, manifest, "electron_worker_entry", "agent-runtime/worker.mjs");
    this.factoryResource = resourceByRole(root, manifest, "b13_worker_factory", "agent-runtime/worker/bridge/b13-worker-factory.mjs");
    this.depsResource = resourceByRole(root, manifest, "electron_host_deps", "agent-runtime/bridge/b13-host-kernel-deps.mjs");
    const channels = new MessageChannel();
    this.hostPort = channels.port1;
    this.worker = new Worker(this.workerResource.resolvedPath, {
      workerData: {
        manifest: this.manifest,
        factoryModule: pathToFileURL(this.factoryResource.resolvedPath).href,
        factoryExport: "createWorkerRuntime",
        factoryData: {
          schemaVersion: 1,
          depsModule: pathToFileURL(this.depsResource.resolvedPath).href,
        },
        hostPort: channels.port2,
      },
      transferList: [channels.port2],
    });
    this.worker.on("message", (value) => this.onWorkerFrame(value));
    this.worker.on("error", (error) => this.degrade(error.message));
    this.worker.on("exit", (code) => {
      if (!this.closed && code !== 0) this.degrade(`worker exited with code ${code}`);
    });
    this.hostPort.on("message", (value) => this.onHostRequest(value));
    this.hostPort.on("messageerror", () => this.degrade("worker host port failed"));
    duplex.on("data", (chunk) => this.onData(chunk));
    duplex.on("error", (error) => this.degrade(error.message));
  }

  onData(chunk) {
    if (this.closed) return;
    this.decoder = Buffer.concat([this.decoder, Buffer.from(chunk)]);
    while (this.decoder.length >= 4) {
      const size = this.decoder.readUInt32BE(0);
      if (size <= 0 || size > MAX_FRAME_BYTES) return this.degrade("runtime frame length is invalid");
      if (this.decoder.length < size + 4) return;
      const body = this.decoder.subarray(4, size + 4);
      this.decoder = this.decoder.subarray(size + 4);
      try {
        this.onBackendFrame(validateFrame(JSON.parse(body.toString("utf8"))));
      } catch (error) {
        this.degrade(error.message);
        return;
      }
    }
  }

  onBackendFrame(value) {
    if (value.type === "runtime.hello") {
      if (value.payload.nonceProof !== nonceProof(this.nonce)) return this.degrade("runtime nonce proof mismatch");
      this.ready = true;
      this.send(frame("runtime.ready", { protocolVersion: 1, buildId: this.manifest.buildIdentity.buildId }));
      return;
    }
    if (value.type === "runtime.host.response") {
      const response = value.payload.response;
      const pending = this.pendingHost.get(response?.requestId);
      if (!pending) return;
      this.pendingHost.delete(response.requestId);
      if (response.ok) pending.postMessage(response);
      else pending.postMessage(response);
      return;
    }
    if (!this.ready) return this.degrade("runtime command arrived before handshake");
    const taskId = String(value.taskId || "");
    const operationKey = String(value.operationKey || "");
    if (!taskId || !operationKey) return this.degrade("runtime command misses task identity");
    if (value.type === "task.submit") return void this.submit(taskId, operationKey, value.payload);
    if (value.type === "task.cancel") return void this.cancel(taskId, operationKey);
    if (value.type === "task.snapshot.request") return void this.snapshot(taskId, operationKey);
    this.degrade(`unsupported runtime frame ${value.type}`);
  }

  async submit(taskId, operationKey, payload) {
    try {
      if (this.active) throw new PackagedFusedWorkerError("PRODUCTION_TASK_ALREADY_ACTIVE", "a production task is already active");
      const open = payload.openInput || payload.open;
      const input = payload.turnInput || payload.input || payload.turn?.input;
      if (!open || !input) throw new PackagedFusedWorkerError("PRODUCTION_OPEN_INPUT_UNBOUND", "packaged production open/turn binding is missing");
      if (open.taskId !== taskId || open.operationKey !== operationKey || input.taskId !== taskId || input.operationKey !== operationKey) {
        throw new PackagedFusedWorkerError("PRODUCTION_SESSION_IDENTITY_MISMATCH", "production task identity mismatch");
      }
      this.active = { taskId, operationKey, requestId: randomUUID() };
      await this.requestWorker({ type: "open", requestId: `open-${this.active.requestId}`, taskId, operationKey, payload: { open }, buildIdentity: this.manifest.buildIdentity });
      this.send(frame("task.accepted", { taskId, operationKey }, { taskId, operationKey }));
      this.runTurn(taskId, operationKey, input);
    } catch (error) {
      this.active = null;
      this.send(frame("runtime.degraded", { code: error.code || "RUNTIME_COMMAND_FAILED", message: error.message }, { taskId, operationKey }));
    }
  }

  async cancel(taskId, operationKey) {
    if (this.active) this.worker.postMessage({ type: "cancel", requestId: `cancel-${this.active.requestId}`, taskId, operationKey, payload: { reason: "user" } });
    this.send(frame("task.cancelled", { cancelled: true }, { taskId, operationKey }));
  }

  runTurn(taskId, operationKey, input) {
    if (!this.active || this.active.taskId !== taskId || this.active.operationKey !== operationKey) {
      throw new PackagedFusedWorkerError("PRODUCTION_SESSION_IDENTITY_MISMATCH", "runTurn identity is not active");
    }
    this.worker.postMessage({
      type: "turn",
      requestId: `turn-${this.active.requestId}`,
      taskId,
      operationKey,
      payload: { turn: { input } },
    });
  }

  async snapshot(taskId, operationKey) {
    try {
      const result = await this.requestWorker({ type: "checkpoint", requestId: `checkpoint-${randomUUID()}`, taskId, operationKey, payload: { request: "checkpoint" } });
      this.send(frame("task.snapshot", result.payload, { taskId, operationKey }));
    } catch (error) {
      this.send(frame("runtime.degraded", { code: error.code || "RUNTIME_COMMAND_FAILED", message: error.message }, { taskId, operationKey }));
    }
  }

  requestWorker(value) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new PackagedFusedWorkerError("RUNTIME_WORKER_TIMEOUT", "packaged worker request timed out")), 10_000);
      this.worker.once("message", (response) => {
        clearTimeout(timer);
        if (response.type === "error") reject(new PackagedFusedWorkerError(response.payload.code || "RUNTIME_WORKER_REQUEST_FAILED", response.payload.message || "worker request failed"));
        else resolve(response);
      });
      this.worker.postMessage(value);
    });
  }

  onWorkerFrame(value) {
    if (value.type === "ready") return;
    if (value.type === "event") {
      this.send(frame("task.event", { event: value.payload }, { taskId: value.taskId, operationKey: value.operationKey }));
      return;
    }
    if (value.type === "turn_end") {
      this.active = null;
      return;
    }
  }

  onHostRequest(value) {
    if (!value || value.type !== "b13.host.request") return this.degrade("worker host request is invalid");
    const requestId = value.requestId;
    this.pendingHost.set(requestId, this.hostPort);
    this.send(frame("runtime.host.request", { request: value }, { taskId: value.taskId, operationKey: value.operationKey }));
  }

  send(value) {
    if (!this.closed) this.duplex.write(encode(value));
  }

  degrade(message) {
    if (this.closed) return;
    this.send(frame("runtime.degraded", { code: "RUNTIME_PROTOCOL_ERROR", message }));
    this.close();
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.hostPort.close();
    void this.worker.terminate();
    this.duplex.destroy?.();
  }
}

function startPackagedFusedWorkerBridge(options) {
  return new PackagedFusedWorkerBridge(options.duplex, options);
}

module.exports = { PackagedFusedWorkerError, PackagedFusedWorkerBridge, startPackagedFusedWorkerBridge };
