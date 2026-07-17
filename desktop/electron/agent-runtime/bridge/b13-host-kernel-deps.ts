import { randomUUID } from "node:crypto";
import type {
  AgentModelEvent,
  AgentModelRequest,
  CanonicalToolResult,
  EchoAgentEventSink,
  EchoAgentSessionPort,
  EchoAgentTelemetryPort,
  EchoClock,
  EchoContextPort,
  EchoIdFactory,
  EchoModelPort,
  EchoTool,
  EchoToolRegistry,
  JsonObject,
  KernelBuildIdentity,
  KernelCheckpoint,
  ModelContext,
  ModelRuntimeSnapshot,
  OpenSessionInput,
  ToolInvocationContext,
  ToolDescriptionContext,
  ToolValidation,
} from "../../../agent-kernel/core/index.ts";
import { B13HostClient, type B13HostPort } from "./b13-host-ipc.ts";
import type { ProductionKernelDependencies } from "./production-factory.ts";
import type { B13KernelDepsFactoryModule, B13KernelDepsFactoryInput, B13HostBindingProvenance } from "./b13-worker-factory.ts";

type RemoteToolDefinition = {
  name: string;
  description?: string;
  inputSchema: JsonObject;
  traits: { readOnly: boolean; destructive: boolean; concurrencySafe: boolean; capability: string };
};

function object(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`B13_HOST_SCHEMA_INVALID: ${label}`);
  return value as JsonObject;
}

function array(value: unknown, label: string): JsonObject[] {
  if (!Array.isArray(value) || !value.every((item) => item !== null && typeof item === "object" && !Array.isArray(item))) throw new Error(`B13_HOST_SCHEMA_INVALID: ${label}`);
  return value as JsonObject[];
}

function publicOpen(open: OpenSessionInput): JsonObject {
  const model = { ...open.model } as Partial<OpenSessionInput["model"]>;
  delete model.credentialHandle;
  return {
    taskId: open.taskId,
    operationKey: open.operationKey,
    model,
    grant: open.grant,
    limits: open.limits,
    ...(open.resume ? { resume: open.resume } : {}),
  } as unknown as JsonObject;
}

class RemoteModel implements EchoModelPort {
  private readonly client: B13HostClient;
  private readonly snapshotValue: ModelRuntimeSnapshot;

  constructor(client: B13HostClient, snapshotValue: ModelRuntimeSnapshot) {
    this.client = client;
    this.snapshotValue = snapshotValue;
  }

  snapshot(): ModelRuntimeSnapshot {
    return this.snapshotValue;
  }

  async countTokens(input: { request: AgentModelRequest }): Promise<{ inputTokens: number; estimated: boolean }> {
    const response = await this.client.call("model.count_tokens", { request: input.request } as unknown as JsonObject);
    const inputTokens = response.inputTokens;
    if (typeof inputTokens !== "number" || !Number.isFinite(inputTokens) || inputTokens < 0 || typeof response.estimated !== "boolean") throw new Error("B13_HOST_SCHEMA_INVALID: token count");
    return { inputTokens, estimated: response.estimated };
  }

  async *stream(request: AgentModelRequest, _signal: AbortSignal): AsyncIterable<AgentModelEvent> {
    const response = await this.client.call("model.stream", { request } as unknown as JsonObject);
    for (const raw of array(response.events, "model events")) {
      if (raw.schemaVersion !== 1 || typeof raw.type !== "string" || raw.requestId !== request.requestId) throw new Error("B13_HOST_SCHEMA_INVALID: model event");
      yield raw as unknown as AgentModelEvent;
    }
  }
}

class RemoteTool implements EchoTool {
  readonly name: string;
  readonly description?: string;
  readonly inputSchema: JsonObject;
  readonly traits: RemoteToolDefinition["traits"];

  private readonly client: B13HostClient;

  constructor(client: B13HostClient, definition: RemoteToolDefinition) {
    this.client = client;
    this.name = definition.name;
    this.description = definition.description;
    this.inputSchema = definition.inputSchema;
    this.traits = definition.traits;
  }

  async describe(_context: ToolDescriptionContext): Promise<string> {
    const response = await this.client.call("tool.describe", { toolName: this.name });
    return typeof response.description === "string" ? response.description : this.description ?? this.name;
  }

  async validate(input: unknown, context: ToolInvocationContext): Promise<ToolValidation> {
    const response = await this.client.call("tool.validate", {
      toolName: this.name,
      input: object(input, "tool input"),
      context: {
        taskId: context.taskId,
        operationKey: context.operationKey,
        grant: context.grant,
        requestId: context.requestId,
        toolUseId: context.toolUseId,
        context: context.context,
      },
    } as unknown as JsonObject);
    return response as unknown as ToolValidation;
  }

  async invoke(input: unknown, context: ToolInvocationContext): Promise<unknown> {
    const response = await this.client.call("tool.invoke", {
      toolName: this.name,
      input: object(input, "tool input"),
      context: {
        taskId: context.taskId,
        operationKey: context.operationKey,
        grant: context.grant,
        requestId: context.requestId,
        toolUseId: context.toolUseId,
        context: context.context,
      },
    } as unknown as JsonObject);
    return response;
  }

  toModelResult(output: unknown): CanonicalToolResult {
    const value = object(output, "tool output");
    const result = typeof value.result === "string" ? value.result : JSON.stringify(value.value ?? "") ?? "";
    return {
      content: result,
      isError: value.isError === true,
      ...(value.receipt && typeof value.receipt === "object" && !Array.isArray(value.receipt) ? { receipt: value.receipt as JsonObject } : {}),
    };
  }
}

class RemoteTools implements EchoToolRegistry {
  private readonly tools: Map<string, RemoteTool>;

  constructor(client: B13HostClient, definitions: RemoteToolDefinition[]) {
    this.tools = new Map(definitions.map((definition) => [definition.name, new RemoteTool(client, definition)]));
  }

  list(): readonly EchoTool[] {
    return [...this.tools.values()];
  }

  resolve(name: string): EchoTool | undefined {
    return this.tools.get(name);
  }
}

class RemoteSession implements EchoAgentSessionPort {
  private readonly client: B13HostClient;

  constructor(client: B13HostClient) {
    this.client = client;
  }

  async startup(kernelIdentity: KernelBuildIdentity): Promise<KernelBuildIdentity> {
    const response = await this.client.call("session.startup", { kernelIdentity } as unknown as JsonObject);
    if (JSON.stringify(response.kernelIdentity) !== JSON.stringify(kernelIdentity)) throw new Error("B13_HOST_IDENTITY_MISMATCH: startup");
    return kernelIdentity;
  }

  async currentDurableEventSeq(): Promise<number> {
    const response = await this.client.call("session.current_durable_event_seq", {});
    if (typeof response.durableEventSeq !== "number" || response.durableEventSeq < 0) throw new Error("B13_HOST_SCHEMA_INVALID: durable event sequence");
    return response.durableEventSeq;
  }

  async saveCheckpoint(checkpoint: KernelCheckpoint): Promise<void> {
    await this.client.call("session.save_checkpoint", { checkpoint } as unknown as JsonObject);
  }

  async close(): Promise<void> {
    await this.client.call("session.close", {});
  }
}

class RemoteEvents implements EchoAgentEventSink {
  private readonly client: B13HostClient;

  constructor(client: B13HostClient) {
    this.client = client;
  }

  async publish(event: import("../../../agent-kernel/core/index.ts").KernelEventEnvelope): Promise<void> {
    await this.client.call("events.publish", { event } as unknown as JsonObject);
  }

  async audit(entry: import("../../../agent-kernel/core/index.ts").KernelAuditEntry): Promise<void> {
    await this.client.call("events.audit", { entry } as unknown as JsonObject);
  }
}

class RemoteContext implements EchoContextPort {
  private readonly tools: RemoteTools;

  constructor(tools: RemoteTools) {
    this.tools = tools;
  }

  async buildModelContext(input: import("../../../agent-kernel/core/index.ts").AgentTurnInput, history: readonly import("../../../agent-kernel/core/index.ts").CanonicalMessage[]): Promise<ModelContext> {
    return {
      system: [{ type: "text", text: input.systemPrompt }],
      messages: [...history],
      tools: this.tools.list().map((tool) => ({ name: tool.name, description: (tool as RemoteTool).description, inputSchema: tool.inputSchema })),
      toolChoice: "auto",
    };
  }
}

class RemoteTelemetry implements EchoAgentTelemetryPort {
  private readonly client: B13HostClient;

  constructor(client: B13HostClient) {
    this.client = client;
  }

  async record(name: string, attributes: JsonObject): Promise<void> {
    await this.client.call("telemetry.record", { name, attributes });
  }
}

class RemoteIds implements EchoIdFactory {
  private sequence = 0;
  next(kind: "request" | "event" | "turn" | "message" | "checkpoint" | "cancel"): string {
    this.sequence += 1;
    return `${kind}-b13-${this.sequence}-${randomUUID()}`;
  }
}

const clock: EchoClock = { now: () => new Date().toISOString() };

export async function createKernelDeps(input: B13KernelDepsFactoryInput & { hostPort?: B13HostPort }): Promise<{
  deps: ProductionKernelDependencies;
  provenance: B13HostBindingProvenance;
}> {
  if (!input.hostPort) throw new Error("B13_HOST_IPC_UNBOUND");
  const client = new B13HostClient(input.hostPort, input.open.taskId, input.open.operationKey);
  const bound = await client.call("session.bind", {
    ...publicOpen(input.open),
    kernelBuildIdentity: input.identity,
  } as unknown as JsonObject);
  const definitions = array(bound.tools, "tool definitions") as unknown as RemoteToolDefinition[];
  const tools = new RemoteTools(client, definitions);
  return {
    deps: {
      model: new RemoteModel(client, input.open.model),
      tools,
      session: new RemoteSession(client),
      events: new RemoteEvents(client),
      context: new RemoteContext(tools),
      clock,
      ids: new RemoteIds(),
      telemetry: new RemoteTelemetry(client),
    },
    provenance: {
      model: "B05M:app.services.model_gateway.AgentModelGateway",
      tools: "B06P:app.agent_capabilities.CapabilityHostRegistry",
      persistence: "B11:app.runtime.b13_composition.B13SessionCheckpointPort",
      identity: "B10:EchoAgentKernel/OpenSessionInput",
    },
  };
}

export default { createKernelDeps } satisfies B13KernelDepsFactoryModule;
