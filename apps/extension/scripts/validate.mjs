import { readFile, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const dist = join(root, "dist");
const manifest = JSON.parse(await readFile(join(dist, "manifest.json"), "utf8"));
const expectedPermissions = ["activeTab", "alarms", "nativeMessaging", "scripting", "storage"];
const actualPermissions = [...manifest.permissions].sort();
if (JSON.stringify(actualPermissions) !== JSON.stringify(expectedPermissions)) {
  throw new Error(`Unexpected permissions: ${actualPermissions.join(", ")}`);
}
if (manifest.host_permissions) throw new Error("Host permissions must remain optional");
if (JSON.stringify(manifest.optional_host_permissions) !== JSON.stringify(["http://*/*", "https://*/*"])) {
  throw new Error("Optional host permission declaration changed");
}

async function validateIcon(path, size) {
  const png = await readFile(join(dist, path));
  if (png.length < 24 || png.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a"
    || png.readUInt32BE(16) !== size || png.readUInt32BE(20) !== size) {
    throw new Error(`Invalid icon dimensions: ${path}`);
  }
}
for (const size of [16, 32, 48, 128]) {
  const path = `icons/icon-${size}.png`;
  if (manifest.icons?.[size] !== path) throw new Error(`Missing extension icon: ${size}`);
  await validateIcon(path, size);
}
for (const size of [16, 24, 32, 48]) {
  const path = `icons/toolbar-filled-${size}.png`;
  if (manifest.action.default_icon?.[size] !== path) throw new Error(`Missing toolbar glyph: ${size}`);
  await validateIcon(path, size);
}
const popup = await readFile(join(dist, "popup.html"), "utf8");
if (!popup.includes('href="help.html"') || /id="test-(submit|result)"/.test(popup)) {
  throw new Error("Popup must expose help and must not expose test submission");
}
await readFile(join(dist, "help.html"), "utf8");

for (const name of await readdir(dist)) {
  if (!name.endsWith(".js")) continue;
  const result = spawnSync(process.execPath, ["--check", join(dist, name)], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr);
}
console.log("Manifest and JavaScript validation passed");
