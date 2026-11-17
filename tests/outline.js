// 文本大纲：把各区块渲染成纯文本，用来检查"删掉说明之后还读不读得懂"。
// 视觉截图不可用时的替代验收手段，比截图更适合检查文案。
// 用法: node tests/outline.js [tab]
const fs = require('fs');

let js = fs.readFileSync('web/app.js', 'utf8');
js = js.slice(0, js.indexOf("$('#refresh').onclick"));

function fakeEl() {
  return {
    className: '', style: {}, innerHTML: '', textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [], appendChild(c) { this.children.push(c); }, onclick: null, dataset: {},
    querySelectorAll: () => [], scrollIntoView() {},
  };
}
global.document = { querySelector: () => fakeEl(), querySelectorAll: () => [], createElement: () => fakeEl() };
global.window = { scrollTo() {}, location: { search: '', href: 'http://x/' } };
global.location = { search: '' };
global.history = { replaceState() {} };
global.URL = class { constructor() { this.searchParams = { set() {} }; } };
global.fetch = async () => ({ status: 200, json: async () => ({}) });

const api = {};
eval(js + '\nObject.assign(api,{hero,executionStrip,kpis,sectors,stockTable,sectorTable,etfPanel,'
  + 'depthSegments,depthIntraday,depthVol,depthRegime,depthXsection,depthSelection,'
  + 'metrics,review,logs,evolutionPanel,weightCard,depthNav,block,stack});');

const S = JSON.parse(fs.readFileSync('data/state.json', 'utf8'));
const pl = S.prediction, dp = S.depth, st = S.stats;

function text(node) {
  if (!node) return '';
  const raw = (node.innerHTML || '') + (node.children || []).map(text).join('\n');
  return raw
    .replace(/<\/(tr|div|table|thead|tbody|section)>/g, '\n')
    .replace(/<\/(td|th)>/g, ' │ ')
    .replace(/<br\s*\/?>/g, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&amp;/g, '&')
    .split('\n')
    .map(l => l.replace(/[ \t]+/g, ' ').replace(/(\s*│\s*)+$/, '').trim())
    .filter(l => l.length > 0)
    .join('\n');
}

function section(title, node) {
  console.log('\n' + '─'.repeat(4) + ' ' + title + ' ' + '─'.repeat(Math.max(0, 44 - title.length)));
  console.log(text(node));
}

const which = process.argv[2] || 'pred';

if (which === 'pred') {
  section('顶栏', { innerHTML: `A股预判 │ ${S.meta.phase} │ v${S.meta.version} │ 刷新 重算`, children: [] });
  section('主结论', api.hero(pl, st, dp));
  section('可执行性', api.executionStrip(pl));
  section('三分量', api.kpis(pl));
  section('板块', api.sectors(pl));
  section('个股', api.stockTable(pl.stocks));
}

if (which === 'sec') {
  section('全板块', api.sectorTable(pl));
  section('ETF', api.etfPanel(pl));
}

if (which === 'depth') {
  section('锚点', api.depthNav());
  section('收益结构', api.stack(api.depthSegments(dp), api.depthIntraday(dp)));
  section('波动与尾部', api.depthVol(dp));
  section('状态基准', api.depthRegime(dp));
  section('截面因子', api.depthXsection(dp));
  section('能力边界', api.stack(api.metrics(st), api.depthSelection(dp)));
  section('自进化', api.stack(api.evolutionPanel(S), api.weightCard(S)));
  section('复盘', api.stack(api.review(S), api.logs(S.errors || [])));
}

// 统计：正文里还剩多少"说明性"字数（用于判断是否真删干净了）
const all = text({ innerHTML: '', children: [
  api.hero(pl, st, dp), api.executionStrip(pl), api.kpis(pl), api.sectors(pl),
  api.stockTable(pl.stocks), api.depthSegments(dp), api.depthIntraday(dp), api.depthVol(dp),
  api.depthRegime(dp), api.depthXsection(dp), api.depthSelection(dp),
  api.metrics(st), api.review(S), api.logs(S.errors || []),
]});
const total = all.replace(/\s/g, '').length;
console.log('\n' + '='.repeat(50));
console.log(`全页可见字符数（含数字标点）: ${total}`);
