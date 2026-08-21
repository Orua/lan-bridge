import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

const [, , sourcePath, outputPath = sourcePath] = process.argv;
if (!sourcePath) {
  throw new Error("Usage: node tools/patch_main_epipe.mjs <app.asar> [output.asar]");
}

const original = readFileSync(sourcePath);
const headerJsonLength = original.readUInt32LE(12);
const header = JSON.parse(original.subarray(16, 16 + headerJsonLength).toString("utf8"));
const payloadStart = 8 + original.readUInt32LE(4);
const mainAsset = header.files["dist-electron"].files["main.js"];
const mainOffset = Number(mainAsset.offset);
const mainStart = payloadStart + mainOffset;
const mainEnd = mainStart + mainAsset.size;
let javascript = original.subarray(mainStart, mainEnd).toString("utf8");

const anchor = "const isDev = !electron_1.app.isPackaged;\n";
const protection =
  "for (const stream of [process.stdout, process.stderr]) {\n" +
  "    stream?.on?.('error', (err) => {\n" +
  "        if (err?.code !== 'EPIPE')\n" +
  "            throw err;\n" +
  "    });\n" +
  "}\n";
if (!javascript.includes(protection)) {
  const matches = javascript.split(anchor).length - 1;
  if (matches !== 1) {
    throw new Error(`Unable to patch EPIPE protection: found ${matches} anchors`);
  }
  javascript = javascript.replace(anchor, anchor + protection);
}

const replacement = Buffer.from(javascript, "utf8");
const delta = replacement.length - mainAsset.size;
mainAsset.size = replacement.length;
mainAsset.integrity.hash = createHash("sha256").update(replacement).digest("hex");

function shiftOffsets(node) {
  for (const file of Object.values(node.files ?? {})) {
    if (file.files) {
      shiftOffsets(file);
    } else if (file !== mainAsset && Number(file.offset) > mainOffset) {
      file.offset = String(Number(file.offset) + delta);
    }
  }
}
shiftOffsets(header);

const payload = Buffer.concat([
  original.subarray(payloadStart, mainStart),
  replacement,
  original.subarray(mainEnd),
]);
const json = Buffer.from(JSON.stringify(header), "utf8");
const innerPayloadSize = (4 + json.length + 1 + 3) & ~3;
const inner = Buffer.alloc(4 + innerPayloadSize);
inner.writeUInt32LE(innerPayloadSize, 0);
inner.writeUInt32LE(json.length, 4);
json.copy(inner, 8);
const outer = Buffer.alloc(8);
outer.writeUInt32LE(4, 0);
outer.writeUInt32LE(inner.length, 4);
const patched = Buffer.concat([outer, inner, payload]);

if (sourcePath === outputPath) {
  const backupPath = `${sourcePath}.before-epipe.bak`;
  if (!existsSync(backupPath)) {
    copyFileSync(sourcePath, backupPath);
  }
}
writeFileSync(outputPath, patched);
console.log(`Patched ${outputPath} (${delta >= 0 ? "+" : ""}${delta} bytes main delta)`);
