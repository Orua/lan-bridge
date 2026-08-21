import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

const [, , sourcePath, outputPath = sourcePath] = process.argv;
if (!sourcePath) {
  throw new Error("Usage: node tools/patch_packaged_ui.mjs <app.asar> [output.asar]");
}

const original = readFileSync(sourcePath);
const headerJsonLength = original.readUInt32LE(12);
const header = JSON.parse(original.subarray(16, 16 + headerJsonLength).toString("utf8"));
const payloadStart = 8 + original.readUInt32LE(4);
const asset = header.files.dist.files.assets.files["index-BpSqrgS_.js"];
const assetOffset = Number(asset.offset);
const assetStart = payloadStart + assetOffset;
const assetEnd = assetStart + asset.size;
let javascript = original.subarray(assetStart, assetEnd).toString("utf8");

const stateBefore =
  'close_to_tray:!0,audit_log_path:""})';
const stateAfter =
  'close_to_tray:!0,audit_log_path:"",codex_official_proxy_url:""})';
const webSearchSection =
  'c.jsxs("section",{className:"settings-section",children:[c.jsx("h3",{children:n==="zh"?"联网搜索服务":"Web Search Provider"})';
const proxySection =
  'c.jsxs("section",{className:"settings-section",children:[c.jsx("h3",{children:n==="zh"?"Codex 官方联网代理":"Codex Official Network Proxy"}),c.jsx("p",{className:"field-hint",children:n==="zh"?"仅在切换到 OpenAI 官方版时使用；不是 Bridge API 的监听或转发地址。":"Used only when switching Codex to the official OpenAI API; this is not the Bridge API endpoint."}),c.jsx("div",{className:"form-grid",children:c.jsxs("div",{className:"form-group full-width",children:[c.jsx("label",{children:n==="zh"?"VPN / HTTP 代理 URL":"VPN / HTTP Proxy URL"}),c.jsx("input",{value:s.codex_official_proxy_url||"",onChange:g=>a({...s,codex_official_proxy_url:g.target.value}),placeholder:"http://127.0.0.1:7890"})]})})]}),';

for (const [before, after, label] of [
  [stateBefore, stateAfter, "settings state"],
  [webSearchSection, proxySection + webSearchSection, "proxy section"],
]) {
  const occurrences = javascript.split(before).length - 1;
  if (occurrences === 1) {
    javascript = javascript.replace(before, after);
  } else if (!javascript.includes(after)) {
    throw new Error(`Unable to patch ${label}: found ${occurrences} matches`);
  }
}

const appearanceStart =
  'c.jsxs("section",{className:"settings-section",children:[c.jsx("h3",{children:l("settings.appearance")})';
const serverStart =
  'c.jsxs("section",{className:"settings-section",children:[c.jsx("h3",{children:l("settings.server")})';
const saveActions =
  'c.jsxs("div",{className:"btn-row",style:{marginTop:24},children:[c.jsx("button",{className:"btn btn-primary",onClick:Et';
const appearanceIndex = javascript.indexOf(appearanceStart);
const serverIndex = javascript.indexOf(serverStart, appearanceIndex);
const saveIndex = javascript.indexOf(saveActions, serverIndex);
if (appearanceIndex < 0 || serverIndex < 0 || saveIndex < 0) {
  throw new Error("Unable to locate settings sections for appearance repositioning");
}
const appearanceBlock = javascript.slice(appearanceIndex, serverIndex);
javascript =
  javascript.slice(0, appearanceIndex) +
  javascript.slice(serverIndex, saveIndex) +
  appearanceBlock +
  javascript.slice(saveIndex);

const replacement = Buffer.from(javascript, "utf8");
const delta = replacement.length - asset.size;
asset.size = replacement.length;
asset.integrity.hash = createHash("sha256").update(replacement).digest("hex");

function shiftOffsets(node) {
  for (const file of Object.values(node.files ?? {})) {
    if (file.files) {
      shiftOffsets(file);
    } else if (file !== asset && Number(file.offset) > assetOffset) {
      file.offset = String(Number(file.offset) + delta);
    }
  }
}
shiftOffsets(header);

const payload = Buffer.concat([
  original.subarray(payloadStart, assetStart),
  replacement,
  original.subarray(assetEnd),
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
  const backupPath = `${sourcePath}.before-proxy-ui.bak`;
  if (!existsSync(backupPath)) {
    copyFileSync(sourcePath, backupPath);
  }
}
writeFileSync(outputPath, patched);
console.log(`Patched ${outputPath} (${delta >= 0 ? "+" : ""}${delta} bytes asset delta)`);
