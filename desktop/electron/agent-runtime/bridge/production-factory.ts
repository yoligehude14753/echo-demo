import {
  EchoAgentKernel,
  type EchoAgentEventSink,
  type EchoAgentSessionPort,
  type EchoClock,
  type EchoContextPort,
  type EchoIdFactory,
  type EchoModelPort,
  type EchoAgentTelemetryPort,
  type EchoToolRegistry,
  type KernelDeps,
  type KernelBuildIdentity,
  type OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";
import { createKernelWorkerRuntime, type KernelWorkerRuntime } from "../worker/bridge.ts";

export const PRODUCTION_DEPENDENCIES_UNBOUND = "PRODUCTION_DEPENDENCIES_UNBOUND" as const;

export interface ProductionKernelDependencies extends KernelDeps {
  model: EchoModelPort;
  tools: EchoToolRegistry;
  session: EchoAgentSessionPort;
  events: EchoAgentEventSink;
  context: EchoContextPort;
  clock: EchoClock;
  ids: EchoIdFactory;
  telemetry: EchoAgentTelemetryPort;
}

export type ProductionWorkerRuntimeInput = {
  open: OpenSessionInput;
  identity: KernelBuildIdentity;
  deps: ProductionKernelDependencies;
};

export class ProductionDependencyError extends Error {
  readonly code = PRODUCTION_DEPENDENCIES_UNBOUND;

  constructor(message: string = PRODUCTION_DEPENDENCIES_UNBOUND) {
    super(message);
    this.name = "ProductionDependencyError";
  }
}

function requireDependencies(
  deps: ProductionKernelDependencies | undefined,
): ProductionKernelDependencies {
  if (!deps || typeof deps !== "object") {
    throw new ProductionDependencyError();
  }
  for (const field of ["model", "tools", "session", "events", "context", "clock", "ids", "telemetry"] as const) {
    if (!deps[field]) throw new ProductionDependencyError(`${PRODUCTION_DEPENDENCIES_UNBOUND}: ${field}`);
  }
  return deps;
}

export function createProductionWorkerRuntime(
  input: ProductionWorkerRuntimeInput,
): Promise<KernelWorkerRuntime> {
  const deps = requireDependencies(input.deps);
  const kernel = new EchoAgentKernel(input.identity);
  return createKernelWorkerRuntime(kernel, input.open, deps);
}

/**
 * WorkerManager's factory-module contract.  The worker-side composition must
 * explicitly supply deps; silently constructing a partial kernel is forbidden.
 */
export async function createWorkerRuntime(input: {
  open: OpenSessionInput;
  identity: KernelBuildIdentity;
  deps?: ProductionKernelDependencies;
}): Promise<KernelWorkerRuntime> {
  return createProductionWorkerRuntime({
    open: input.open,
    identity: input.identity,
    deps: requireDependencies(input.deps),
  });
}
