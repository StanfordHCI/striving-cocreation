import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const docs = fileURLToPath(new URL('../', import.meta.url));
const built = join(docs, 'dist');
const output = join(docs, 'dist-site');
const routes = ['index.html'];
const assets = await readdir(join(built, '_astro'));
const files = new Set(routes);
const queue = [...routes];

// Include only compiled assets reachable from the paper page.
while (queue.length) {
  const file = queue.shift();
  if (!/\.(html|css|js)$/.test(file)) continue;
  const content = await readFile(join(built, file), 'utf8');
  for (const asset of assets) {
    const path = `_astro/${asset}`;
    if (content.includes(asset) && !files.has(path)) {
      files.add(path);
      queue.push(path);
    }
  }
}

const demo = JSON.parse(await readFile(join(docs, 'demo-screens.json'), 'utf8'));
for (const { file } of demo.files) files.add(`demo-screens/${file}`);
for (const file of ['architecture', 'editing', 'striving-quality', 'striving-representativeness', 'editing-results']) {
  files.add(`figures/uist2026/${file}.png`);
}
for (const file of ['media/tempo-preview.mp4', 'media/tempo-preview-poster.jpg', 'licenses/DM-Sans-OFL.txt']) files.add(file);

await rm(output, { recursive: true, force: true });
for (const file of files) {
  await mkdir(dirname(join(output, file)), { recursive: true });
  await cp(join(built, file), join(output, file));
}
await writeFile(join(output, '.nojekyll'), '');
console.log(`Packaged the paper website and approved media in dist-site/.`);
