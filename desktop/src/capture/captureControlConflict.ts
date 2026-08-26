export class CaptureControlConflictError extends Error {
  constructor() {
    super("收音选择已被其他设备更新");
    this.name = "CaptureControlConflictError";
  }
}
