import { makeRuntimeFrame, validateRuntimeFrame, type RuntimeFrame } from "./envelope.ts";

export interface RuntimeMessagePort {
  postMessage(value: unknown): void;
  on(event: "message", listener: (value: unknown) => void): this;
  on(event: "messageerror", listener: (error: unknown) => void): this;
  close(): void;
}

export type RuntimeFrameListener = (frame: RuntimeFrame) => void;
export type RuntimePortErrorListener = (error: unknown) => void;

export class MessagePortChannel {
  private readonly port: RuntimeMessagePort;
  private closed = false;
  private readonly onMessage: (value: unknown) => void;
  private readonly onMessageError: (error: unknown) => void;

  constructor(
    port: RuntimeMessagePort,
    onFrame: RuntimeFrameListener,
    onError: RuntimePortErrorListener,
  ) {
    this.port = port;
    this.onMessage = (value) => {
      try {
        onFrame(validateRuntimeFrame(value));
      } catch (error) {
        onError(error);
      }
    };
    this.onMessageError = onError;
    port.on("message", this.onMessage);
    port.on("messageerror", this.onMessageError);
  }

  send(input: Omit<RuntimeFrame, "schemaVersion">): RuntimeFrame {
    if (this.closed) throw new Error("runtime message port is closed");
    const frame = makeRuntimeFrame(input);
    this.port.postMessage(frame);
    return frame;
  }

  sendFrame(frame: RuntimeFrame): RuntimeFrame {
    if (this.closed) throw new Error("runtime message port is closed");
    const validated = validateRuntimeFrame(frame);
    this.port.postMessage(validated);
    return validated;
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.port.close();
  }
}
