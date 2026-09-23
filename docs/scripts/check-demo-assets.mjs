import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const docs = fileURLToPath(new URL('../', import.meta.url));
const publicDir = join(docs, 'public');
const demoDir = join(publicDir, 'demo-screens');
const manifest = JSON.parse(readFileSync(join(docs, 'demo-screens.json'), 'utf8'));
const errors = [];

// Astro copies public/ even when Git ignores its contents.
for (const folder of ['screens', 'screenshots', 'timelapse', 'videos']) {
  if (existsSync(join(publicDir, folder))) {
    errors.push(`public/${folder}/ is reserved for private captures and must stay outside the website.`);
  }
}

if (manifest.kind !== 'generated-fictional-ui' || manifest.sourceImages?.length !== 0) {
  errors.push('The graph background must use fictional images generated without source screenshots.');
}

const allowed = new Map(manifest.files.map(({ file, sha256 }) => [file, sha256]));
if (!existsSync(demoDir)) {
  errors.push('public/demo-screens/ is missing.');
} else if (lstatSync(demoDir).isSymbolicLink()) {
  errors.push('public/demo-screens/ must not link to an external image directory.');
} else {
  for (const entry of readdirSync(demoDir, { withFileTypes: true })) {
    if (!entry.isFile() || !allowed.has(entry.name)) {
      errors.push(`Unexpected asset in public/demo-screens/: ${entry.name}`);
      continue;
    }
    const hash = createHash('sha256').update(readFileSync(join(demoDir, entry.name))).digest('hex');
    if (hash !== allowed.get(entry.name)) {
      errors.push(`${entry.name} changed. Review its provenance before updating demo-screens.json.`);
    }
  }
  for (const file of allowed.keys()) {
    if (!existsSync(join(demoDir, file))) errors.push(`Missing demo screen: ${file}`);
  }
}

if (errors.length) {
  console.error(errors.join('\n'));
  process.exitCode = 1;
} else {
  console.log(`Verified ${allowed.size} fictional graph screens; former capture folders are absent.`);
}
