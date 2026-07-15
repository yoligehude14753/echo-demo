import { runModel } from "./model.js";

export type KernelInput = {
  taskId: string;
  operationKey: string;
};

export function runKernel(input: KernelInput): string {
  return runModel(input.taskId + ":" + input.operationKey);
}
