// Editable vector annotations: original screenshots are embedded unchanged.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../app/education');
const manifest = JSON.parse(await fs.readFile(path.join(root, 'assets/manifest.json'), 'utf8'));
const escape = value => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;');
// Optional renderer path keeps a developer-only asset tool out of bot dependencies.
const sharp = process.argv[2] ? (await import(pathToFileURL(process.argv[2]).href)).default : null;
for (const [name, item] of Object.entries(manifest)) {
  const bytes = await fs.readFile(path.join(root, 'originals', name + '.png'));
  // Browser capture bytes may use JPEG encoding despite the requested .png filename.
  // Captured mobile viewport was inspected at 633 × 751 pixels.
  const width = 633, height = 751;
  const overlay = item.boxes.map(([x, y, w, h], index) =>
    `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="none" stroke="#ff2929" stroke-width="4"/>` +
    `<path d="M ${Math.max(4,x-18)} ${y+12} L ${x} ${y+12} l -7 -5 m 7 5 l -7 5" fill="none" stroke="#ff2929" stroke-width="3"/>`
  ).join('');
  const mime = bytes[0] === 0x89 ? 'image/png' : 'image/jpeg';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img"><title>${escape(item.caption)}</title><image width="${width}" height="${height}" href="data:${mime};base64,${bytes.toString('base64')}"/>${overlay}</svg>`;
  await fs.writeFile(path.join(root, 'assets', name + '.svg'), svg);
  if (sharp) await sharp(Buffer.from(svg)).png().toFile(path.join(root, 'assets', name + '.png'));
}
console.log(`${Object.keys(manifest).length} vector annotations created; originals unchanged.`);
