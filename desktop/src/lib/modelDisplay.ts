/** Display the exact model id supplied by the runtime contract. */
export function modelDisplayName(runtimeName?: unknown): string {
  const name = String(runtimeName ?? "").trim();
  return name || "模型未确认";
}
