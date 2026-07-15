import {
  EchoAgentKernel,
  type AgentTurnInput,
  type CancelReason,
  type KernelCheckpoint,
  type KernelDeps,
  type KernelEventEnvelope,
  type KernelSession,
  type KernelBuildIdentity,
  type OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";

export interface KernelWorkerRuntime {
  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope>;
  checkpoint(): Promise<KernelCheckpoint>;
  cancel(reason: CancelReason): Promise<void>;
  close(): Promise<void>;
}

export type KernelWorkerRuntimeFactoryInput = {
  open: OpenSessionInput;
  identity: KernelBuildIdentity;
};

export type KernelWorkerRuntimeFactory = (
  input: KernelWorkerRuntimeFactoryInput,
) => Promise<KernelWorkerRuntime>;

class KernelSessionRuntime implements KernelWorkerRuntime {
  private readonly session: KernelSession;

  constructor(session: KernelSession) {
    this.session = session;
  }

  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    return this.session.runTurn(input);
  }

  checkpoint(): Promise<KernelCheckpoint> {
    return this.session.checkpoint();
  }

  cancel(reason: CancelReason): Promise<void> {
    return this.session.cancel(reason);
  }

  close(): Promise<void> {
    return this.session.close();
  }
}

/**
 * Production wiring helper. Model/tool/session/context ports remain injected by
 * the embedding module; the worker owns only the kernel session lifetime.
 */
export function createKernelWorkerRuntime(
  kernel: EchoAgentKernel,
  input: OpenSessionInput,
  deps: KernelDeps,
): Promise<KernelWorkerRuntime> {
  return kernel.openSession(input, deps).then((session) => new KernelSessionRuntime(session));
}
