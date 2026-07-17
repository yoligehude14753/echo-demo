export { MessagePortChannel } from "./message-port/channel.ts";
export {
  MAX_RUNTIME_FRAME_BYTES,
  WORKER_PROTOCOL_VERSION,
  RuntimeProtocolError,
  makeRuntimeFrame,
  runtimeFrameByteLength,
  validateRuntimeFrame,
} from "./message-port/envelope.ts";
export type { RuntimeFrame, RuntimeFrameType } from "./message-port/envelope.ts";
export { createKernelWorkerRuntime } from "./worker/bridge.ts";
export type { KernelWorkerRuntime, KernelWorkerRuntimeFactory, KernelWorkerRuntimeFactoryInput } from "./worker/bridge.ts";
export {
  RUNTIME_MANIFEST_SCHEMA_VERSION,
  assertRuntimeManifestMatches,
  assertWorkerBuildIdentity,
  createRuntimeManifest,
  newRuntimeEventId,
  validateRuntimeManifest,
} from "./worker/identity.ts";
export type { RuntimeContractVersions, RuntimeManifest } from "./worker/identity.ts";
export { WorkerManager, WorkerRuntimeError, WorkerRuntimeSession } from "./pool/worker-manager.ts";
export type { WorkerManagerOptions, WorkerManagerState } from "./pool/worker-manager.ts";
export { PooledWorkerRuntimeSession, WorkerPool } from "./pool/worker-pool.ts";
export type { WorkerPoolOptions } from "./pool/worker-pool.ts";
export {
  decodeRuntimeFrame,
  encodeRuntimeFrame,
  FramedRuntimeError,
  makeRuntimeMessage,
  nonceProof,
  RuntimeFrameDecoder,
} from "./bridge/framed-runtime.ts";
export type { FramedRuntimeMessage, RuntimeDuplex } from "./bridge/framed-runtime.ts";
export { EmbeddedRuntimePortServer } from "./bridge/embedded-runtime-server.ts";
export type { EmbeddedRuntimeCommandHandler } from "./bridge/embedded-runtime-server.ts";
export {
  createProductionEmbeddedRuntimeCommandHandler,
  createProductionEmbeddedRuntimePort,
} from "./bridge/production-composition.ts";
export type {
  ProductionCompositionOptions,
  ProductionRuntimeEvent,
} from "./bridge/production-composition.ts";
export {
  createProductionWorkerRuntime,
  createWorkerRuntime,
  ProductionDependencyError,
  PRODUCTION_DEPENDENCIES_UNBOUND,
} from "./bridge/production-factory.ts";
export {
  B13_FACTORY_DATA_SCHEMA,
  B13_HOST_BINDING_UNBOUND,
  B13HostBindingError,
  createWorkerRuntime as createB13WorkerRuntime,
} from "./bridge/b13-worker-factory.ts";
export type {
  B13HostBindingProvenance,
  B13KernelDepsFactoryInput,
  B13KernelDepsFactoryModule,
  B13WorkerFactoryData,
} from "./bridge/b13-worker-factory.ts";
export {
  B13HostClient,
  B13_HOST_PROTOCOL_VERSION,
  B13_HOST_REQUEST_TYPE,
  B13_HOST_RESPONSE_TYPE,
  validateB13HostRequest,
  validateB13HostResponse,
} from "./bridge/b13-host-ipc.ts";
export type {
  B13HostMethod,
  B13HostPort,
  B13HostRequest,
  B13HostRequestHandler,
  B13HostResponse,
} from "./bridge/b13-host-ipc.ts";
export type {
  ProductionKernelDependencies,
  ProductionWorkerRuntimeInput,
} from "./bridge/production-factory.ts";
