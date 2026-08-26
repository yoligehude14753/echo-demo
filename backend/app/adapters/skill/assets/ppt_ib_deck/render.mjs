// docxtemplater PPTX render: 读模板 + JSON → 输出 .pptx
// 用法: node render.mjs <template.pptx> <data.json> <out.pptx>

import fs from "node:fs";
import path from "node:path";
import PizZip from "pizzip";
import Docxtemplater from "docxtemplater";

const [, , tplPath, jsonPath, outPath] = process.argv;
if (!tplPath || !jsonPath || !outPath) {
  console.error("usage: node render.mjs <template.pptx> <data.json> <out.pptx>");
  process.exit(1);
}

const buf = fs.readFileSync(path.resolve(tplPath));
const zip = new PizZip(buf);
const data = JSON.parse(fs.readFileSync(path.resolve(jsonPath), "utf-8"));

const doc = new Docxtemplater(zip, {
  paragraphLoop: true,
  linebreaks: true,
  delimiters: { start: "{", end: "}" },
});

doc.render(data);

const out = doc.getZip().generate({ type: "nodebuffer", compression: "DEFLATE" });
fs.writeFileSync(path.resolve(outPath), out);
console.log(`OK written: ${outPath} (${out.length} bytes)`);
