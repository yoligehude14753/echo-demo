import type {
  JsonObject,
  KernelBuildIdentity,
  OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";
import {
  createProductionWorkerRuntime,
  ProductionDependencyError,
  type ProductionKernelDependencies,
} from "./production-factory.ts";
import type { KernelWorkerRuntime } from "../worker/bridge.ts";

export const B13_FACTORY_DATA_SCHEMA = 1 as const;
export const B13_HOST_BINDING_UNBOUND = "B13_HOST_BINDING_UNBOUND" as const;

export type B13HostBindingProvenance = {
  model: "B05M:app.services.model_gateway.AgentModelGateway";
  tools: "B06P:app.agent_capabilities.CapabilityHostRegistry";
  persistence: "B11:app.runtime.b13_composition.B13SessionCheckpointPort";
  identity: "B10:EchoAgentKernel/OpenSessionInput";
};

export type B13KernelDepsFactoryInput = {
  open: OpenSessionInput;
  identity: KernelBuildIdentity;
};

export type B13KernelDepsFactoryModule = {
  createKernelDeps(
    input: B13KernelDepsFactoryInput,
  ): Promise<{
    deps: ProductionKernelDependencies;
    provenance: B13HostBindingProvenance;
  }>;
};

export type B13WorkerFactoryData = JsonObject & {
  schemaVersion: 1;
  depsModule: string;
};

export class B13HostBindingError extends Error {
  readonly code = B13_HOST_BINDING_UNBOUND;

  constructor(message: string = B13_HOST_BINDING_UNBOUND) {
    super(message);
    this.name = "B13HostBindingError";
  }
}

function requireFactoryData(value: JsonObject | undefined): B13WorkerFactoryData {
  if (!value || value.schemaVersion !== B13_FACTORY_DATA_SCHEMA || typeof value.depsModule !== "string" || !value.depsModule) {
    throw new B13HostBindingError();
  }
  return value as B13WorkerFactoryData;
}

function requireProvenance(value: unknown): asserts value is B13HostBindingProvenance {
  if (value === null || typeof value !== "object") throw new B13HostBindingError();
  const provenance = value as Record<string, unknown>;
  if (
    provenance.model !== "B05M:app.services.model_gateway.AgentModelGateway" ||
    provenance.tools !== "B06P:app.agent_capabilities.CapabilityHostRegistry" ||
    provenance.persistence !== "B11:app.runtime.b13_composition.B13SessionCheckpointPort" ||
    provenance.identity !== "B10:EchoAgentKernel/OpenSessionInput"
  ) {
    throw new B13HostBindingError("B13_HOST_BINDING_UNBOUND: provenance");
  }
}

/**
 * Worker-local production factory.  The host supplies a module URL, not
 * dependency objects or credentials.  That module must construct the concrete
 * B05M/B06P/B11 ports and prove the B10 identity binding; absent or partial
 * bindings fail closed before EchoAgentKernel.openSession.
 */
export async function createWorkerRuntime(input: {
  open: OpenSessionInput;
  identity: KernelBuildIdentity;
  factoryData?: JsonObject;
}): Promise<KernelWorkerRuntime> {
  const data = requireFactoryData(input.factoryData);
  const loaded = await import(data.depsModule) as Partial<B13KernelDepsFactoryModule>;
  if (typeof loaded.createKernelDeps !== "function") {
    throw new B13HostBindingError("B13_HOST_BINDING_UNBOUND: createKernelDeps");
  }
  const result = await loaded.createKernelDeps({ open: input.open, identity: input.identity });
  if (!result || typeof result !== "object" || !result.deps) {
    throw new B13HostBindingError("B13_HOST_BINDING_UNBOUND: deps");
  }
  requireProvenance(result.provenance);
  try {
    return await createProductionWorkerRuntime({
      open: input.open,
      identity: input.identity,
      deps: result.deps,
    });
  } catch (error) {
    if (error instanceof ProductionDependencyError) throw error;
    throw error;
  }
}
