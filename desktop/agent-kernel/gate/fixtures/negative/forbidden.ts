import { readFile } from "node:fs";

export function forbidden(): unknown {
  return readFile;
}
