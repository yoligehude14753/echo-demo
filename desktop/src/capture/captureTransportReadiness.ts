export interface CaptureTransportReadinessPort {
  current(): boolean;
  subscribe(listener: (ready: boolean) => void): () => void;
}

export interface CaptureTransportCoordinatorPort {
  start(): void;
  pause(): void;
  kick(): void;
}

export interface CaptureTransportReadinessHandlers {
  onReady(): void;
  onNotReady(): void;
}

/**
 * 只缓存 readiness 的边沿用于幂等启停；不持有 active partition。
 * active ownership 唯一属于 upload pool，避免 readiness 的异步回调把旧
 * capture scope 重新提升为 active。
 */
export class CaptureTransportReadinessGate {
  private readonly port: CaptureTransportReadinessPort;
  private readonly coordinator: CaptureTransportCoordinatorPort;
  private readonly handlers: CaptureTransportReadinessHandlers;
  private ready: boolean;
  private running = false;
  private unsubscribe: (() => void) | null = null;

  constructor(
    port: CaptureTransportReadinessPort,
    coordinator: CaptureTransportCoordinatorPort,
    handlers: CaptureTransportReadinessHandlers,
  ) {
    this.port = port;
    this.coordinator = coordinator;
    this.handlers = handlers;
    this.ready = port.current();
  }

  start(): void {
    if (this.unsubscribe) return;
    this.unsubscribe = this.port.subscribe((ready) => this.update(ready));
    if (this.ready) {
      this.ensureStarted();
      this.handlers.onReady();
    }
  }

  current(): boolean {
    return this.ready;
  }

  kick(): void {
    if (!this.ready) return;
    this.ensureStarted();
    this.coordinator.kick();
  }

  dispose(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.pauseCoordinator();
  }

  private update(ready: boolean): void {
    if (ready === this.ready) return;
    this.ready = ready;
    if (!ready) {
      this.pauseCoordinator();
      this.handlers.onNotReady();
      return;
    }
    this.ensureStarted();
    this.handlers.onReady();
  }

  private ensureStarted(): void {
    if (!this.ready || this.running) return;
    this.coordinator.start();
    this.running = true;
  }

  private pauseCoordinator(): void {
    if (!this.running) return;
    this.coordinator.pause();
    this.running = false;
  }
}
