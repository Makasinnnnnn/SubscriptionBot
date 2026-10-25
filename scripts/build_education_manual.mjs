import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const edu = path.join(root, 'app/education');
const guide = await fs.readFile(path.join(edu, 'guide.md'), 'utf8');
const manifest = JSON.parse(await fs.readFile(path.join(edu, 'assets/manifest.json'), 'utf8'));
const groups = {
  intro:['01-demo-account'], open:['02-search-btc','03-directions','16-market-amount','04-long-form','05-open-long','11-short-form','12-open-short'],
  protect:['17-add-protection'], change:['06-protective-orders','07-edit-stop','08-edit-take'],
  orders:['13-limit-form','14-pending-limit','15-cancelled-limit','18-remove-protection'],
  close:['09-close-half','10-close-rest','19-history']
};
const esc = text => text.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const format = text => esc(text).replace(/https:\/\/[^\s]+/g, url => `<a href="${url}">${url}</a>`);
const sections = guide.split('\n## ').slice(1);
let body = '<h1>Upscale Trade</h1><p class="lead">Практическая методичка для пользователей Telegram-бота</p><p>25 октября 2026 · Мобильный интерфейс · Практика на демо-счёте</p><nav>';
for(const section of sections){const heading=section.split('\n')[0]; const [key,title]=heading.split(' — ');body+=`<a href="#${key}">${esc(title)}</a>`;}
body+='</nav>';
for(const section of sections){
  const [heading,...lines]=section.split('\n'); const [key,title]=heading.split(' — ');
  body+=`<section id="${key}"><h2>${esc(title)}</h2>`;
  for(const page of lines.join('\n').trim().split('\n---\n')){
    const [label,...rest]=page.trim().split('\n');
    body+=`<article><h3>${esc(label)}</h3>`;
    for(const paragraph of rest.join('\n').trim().split('\n\n')){
      if(paragraph.includes(' | ')){
        const rows=paragraph.split('\n'); body+='<table>';
        rows.forEach((row,index)=>{body+='<tr>'+row.split(' | ').map(cell=>`<${index?'td':'th'}>${format(cell)}</${index?'td':'th'}>`).join('')+'</tr>';});body+='</table>';
      }else body+=`<p>${format(paragraph).replaceAll('\n','<br>')}</p>`;
    }
    body+='</article>';
  }
  for(const name of groups[key]||[]){
    const image=await fs.readFile(path.join(edu,'assets',name+'.png'));
    body+=`<figure><img src="data:image/png;base64,${image.toString('base64')}" alt="${esc(manifest[name].caption)}"><figcaption>${esc(manifest[name].caption)}</figcaption></figure>`;
  }
  body+='</section>';
}
const html=`<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Upscale Trade — практическая методичка</title><style>body{font:17px/1.6 system-ui,sans-serif;color:#18232e;background:#f4f6f8;max-width:850px;margin:32px auto;padding:0 20px}h1{font-size:48px;margin-bottom:0}h2{font-size:28px;border-top:3px solid #ed922c;padding-top:24px}h3{font-size:21px}section{scroll-margin-top:20px}article{background:white;padding:16px 24px;margin:16px 0;border-radius:12px}.lead{font-size:23px}nav{display:grid;gap:8px;background:white;padding:24px;border-radius:12px}a{color:#205b91;overflow-wrap:anywhere}figure{margin:24px auto;max-width:633px}img{display:block;width:100%;height:auto;border-radius:8px}figcaption{font-size:15px;color:#3f4c59;margin-top:10px}table{border-collapse:collapse;font-size:14px;width:100%;display:block;overflow:auto}td,th{border:1px solid #d4dbe0;padding:7px;text-align:left}@media print{body{background:white;margin:0}nav{display:none}article{padding:0}figure{max-width:400px;break-inside:avoid}h2,h3{break-after:avoid}}</style>${body}</html>`;
await fs.writeFile(path.join(root,'docs/upscale-manual.html'),html);
console.log(`Standalone manual: ${sections.length} lessons, ${Object.keys(manifest).length} screenshots.`);
