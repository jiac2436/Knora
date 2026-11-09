import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = join(root, "dist");

async function copySources(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const source = join(directory, entry.name);
    if (entry.isDirectory()) {
      await copySources(source);
      continue;
    }
    const targetName = extname(entry.name) === ".ts" ? `${entry.name.slice(0, -3)}.js` : entry.name;
    const target = join(output, relative(join(root, "src"), dirname(source)), targetName);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, await readFile(source, "utf8"));
  }
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(join(root, "manifest.json"), join(output, "manifest.json"));
await cp(join(root, "static"), output, { recursive: true });
await copySources(join(root, "src"));
console.log(`Built extension at ${output}`);
