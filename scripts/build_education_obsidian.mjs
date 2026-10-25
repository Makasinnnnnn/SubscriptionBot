import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = path.join(root, 'docs/upscale-channel/posts');
const output = path.join(root, 'docs/upscale-obsidian');
const manifest = JSON.parse(await fs.readFile(path.join(root, 'app/education/assets/manifest.json'), 'utf8'));
const sections = [
  ['01-start.txt', 'Начало работы и выбор счёта', ['01-demo-account']],
  ['02-signal.txt', 'Как читать торговый сигнал', []],
  ['03-long.txt', 'Как открыть LONG', ['02-search-btc','03-directions','16-market-amount','04-long-form','05-open-long']],
  ['04-short.txt', 'Как открыть SHORT', ['11-short-form','12-open-short']],
  ['05-risk.txt', 'Как рассчитать залог и риск', []],
  ['06-protection.txt', 'Как поставить Stop Loss и Take Profit', ['17-add-protection','06-protective-orders']],
  ['07-change.txt', 'Как изменить стоп или тейк', ['07-edit-stop','08-edit-take','18-remove-protection']],
  ['08-orders.txt', 'Limit: создание, изменение и отмена', ['13-limit-form','14-pending-limit','15-cancelled-limit']],
  ['09-close.txt', 'Как закрыть часть или всю позицию', ['09-close-half','10-close-rest','19-history']],
  ['10-rules.txt', 'Счета и ограничения', []],
  ['11-check.txt', 'Проверка перед подтверждением', []],
];
await fs.mkdir(path.join(output, 'images'), {recursive:true});
let markdown = `---\ntitle: "Upscale Trade — практическая методичка"\ndate: 2026-10-25\ntags:\n  - upscale\n  - обучение\n---\n\n# Upscale Trade — практическая методичка\n\nПошаговые инструкции для первого знакомства с мобильным терминалом. Каждый раздел можно читать отдельно; бот для работы с методичкой не нужен.\n\nРедакция: 25 октября 2026 года.\n\n> Все иллюстрации показывают настоящий интерфейс и учебные операции на демо-счёте. Значения относятся ко времени съёмки, отличаются между примерами и не являются текущими торговыми сигналами. Красные рамки выделяют нужные кнопки и поля.\n\n> Перед использованием реальных сигналов уточните их допустимость: Upscale запрещает систематическое копирование сделок другого трейдера Upscale, включая сигнальные сервисы. Самостоятельный ввод заявки не отменяет этого правила. До уточнения условий практикуйтесь на демо.\n\n## Содержание\n\n`;
sections.forEach(([,title],i)=>{markdown+=`${i+1}. [[#${i+1}. ${title}]]\n`;});
let imageNumber = 0;
for(const [index,[filename,title,images]] of sections.entries()) {
  const sourceText = (await fs.readFile(path.join(source,filename),'utf8')).replaceAll('\r\n','\n');
  const lines = sourceText.split('\n').slice(1).filter(line=>!/^#upscale_/.test(line));
  const body = lines.join('\n').trim().replace(/^Фото к уроку:.*$/m,'').trim();
  markdown += `\n---\n\n## ${index+1}. ${title}\n\n${body}\n`;
  if(images.length) markdown += '\n### Скриншоты к разделу\n';
  for(const name of images) {
    imageNumber++;
    const caption = manifest[name].caption;
    markdown += `\n**Рисунок ${imageNumber}. ${caption}**\n\n![${caption}](images/${name}.png)\n`;
    await fs.copyFile(path.join(root,'app/education/assets',name+'.png'),path.join(output,'images',name+'.png'));
  }
}
markdown += '\n---\n\n## Официальные источники\n\n- [Документация Upscale](https://docs.upscale.trade/ru)\n- [Рабочее пространство](https://docs.upscale.trade/ru/kak-rabotaet-upscale/rabochee-prostranstvo-upscale)\n- [Типы ордеров](https://docs.upscale.trade/ru/torgovye-mekhaniki/tipy-orderov)\n- [Позиции и кредитное плечо](https://docs.upscale.trade/ru/torgovye-mekhaniki/pozicii-i-kreditnoe-plecho)\n- [Правила счетов](https://docs.upscale.trade/ru/kak-prisoedinitsya-k-upscale/pravila-chellendzha-i-torgovli)\n- [Комиссии](https://docs.upscale.trade/ru/torgovye-mekhaniki/komissii)\n- [Фандинг](https://docs.upscale.trade/ru/torgovye-mekhaniki/fanding)\n';
await fs.writeFile(path.join(output,'Методичка Upscale.md'),markdown,'utf8');
console.log(`Obsidian manual: ${sections.length} sections, ${imageNumber} embedded screenshots.`);
