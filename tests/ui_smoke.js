// 渲染冒烟测试：用真实 state.json 跑一遍各渲染函数，检查是否产生 undefined / NaN / 空表
const fs = require('fs');

// 前端已从 index.html 拆出，直接读 app.js
let js = fs.readFileSync('web/app.js', 'utf8');
// 去掉末尾的 DOM 绑定与自动加载（那部分依赖真实浏览器）
js = js.slice(0, js.indexOf("$('#refresh').onclick"));

function fakeEl() {
  return {
    className: '', style: {}, innerHTML: '', textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [], appendChild(c) { this.children.push(c); }, onclick: null, dataset: {},
    querySelectorAll: () => [], scrollIntoView() {},
  };
}
global.document = {
  querySelector: () => fakeEl(),
  querySelectorAll: () => [],
  createElement: () => fakeEl(),
};
const scrollCalls = [];
global.window = { scrollY: 0, scrollTo(...a) { scrollCalls.push(a); },
                 location: { search: '', href: 'http://x/' } };
global.history = { replaceState() {} };
global.localStorage = { getItem: () => null, setItem() {} };
global.URL = class { constructor(u) { this.searchParams = { set() {} }; } };
global.location = { search: '' };
global.fetch = async () => ({ status: 200, json: async () => ({}) });

const api = {};
eval(js + '\napi.hero=hero;api.kpis=kpis;api.sectors=sectors;api.sectorTable=sectorTable;'
  + 'api.etfPanel=etfPanel;api.stockTable=stockTable;api.review=review;api.metrics=metrics;'
  + 'api.weightCard=weightCard;api.logs=logs;api.render=render;api.evolutionPanel=evolutionPanel;'
  + 'api.executionStrip=executionStrip;api.riskRows=riskRows;api.depthSegments=depthSegments;'
  + 'api.depthIntraday=depthIntraday;api.depthVol=depthVol;api.depthRegime=depthRegime;'
  + 'api.depthXsection=depthXsection;api.depthSelection=depthSelection;'
  + 'api.depthNav=depthNav;api.DEPTH_GROUPS=DEPTH_GROUPS;api.setBusy=setBusy;api.switchTab=switchTab;api.executionStrip=executionStrip;'
  + 'api.autoNext=autoNext;api.cycleAuto=cycleAuto;api.applyAuto=applyAuto;'
  + 'api.AUTO_STEPS=AUTO_STEPS;api.currentTab=currentTab;'
  + 'api.rescuePanel=rescuePanel;api.rescueBadge=rescueBadge;api.dataNotes=dataNotes;'
  + 'api.stockCalibNote=stockCalibNote;api.nameThemeNote=nameThemeNote;api.chanPanel=chanPanel;api.load=load;');

const S = JSON.parse(fs.readFileSync('data/state.json', 'utf8'));
// 导航项跟着 DEPTH_GROUPS 走，不写死名字——合并/重命名分节时不该改测试
const DEPTH_TAB_HINT = api.DEPTH_GROUPS.flatMap(([, items]) => items.map(([, t]) => t));
const pl = S.prediction;
let bad = 0;

function check(name, node, mustHave = []) {
  // 有些函数返回的是容器，内容在子节点里
  const h = (node.innerHTML || '') + ((node.children || []).map(c => c.innerHTML).join('') || '');
  const errs = [];
  if (!h || h.length < 20) errs.push('空内容');
  if (/undefined|NaN|\[object Object\]/.test(h)) errs.push('含 undefined/NaN');
  mustHave.forEach(k => { if (!h.includes(k)) errs.push('缺少 ' + k); });
  if (errs.length) { bad++; console.log(`✗ ${name}: ${errs.join('; ')}`); }
  else console.log(`✓ ${name}  (${h.length} 字符)`);
  return h;
}

check('hero', api.hero(pl, S.stats, S.depth), ['上涨概率', '预计涨跌', '90% 的概率落在']);
check('kpis', api.kpis(pl), ['开盘', '盘中', '全天']);
check('sectors', api.sectors(pl), ['比大盘强', '比大盘弱', '跌久了的板块']);
const t = check('sectorTable', api.sectorTable(pl), ['板块', '资金流入', '相关 ETF']);
check('etfPanel', api.etfPanel(pl), ['资金流入最多', '资金流出最多']);
check('stockTable', api.stockTable(pl.stocks), [pl.stocks[0].name, pl.stocks[0].sector]);
check('review', api.review(S), ['开盘', '全天']);
check('metrics', api.metrics(S.stats), ['开盘方向命中率']);
check('weightCard', api.weightCard(S), ['各因素的分量']);
check('evolutionPanel', api.evolutionPanel(S), ['开盘模型 · 敏感度', '区间命中率', '盘中层可用吗']);
check('logs', api.logs(S.errors), [S.errors[0] ? S.errors[0].day : '跳空']);

// ---- 深度层 ----
const dp = S.depth || {};
if (dp.error) {
  console.log('  ✗ depth 计算失败:', dp.error); bad++;
} else {
  check('executionStrip', api.executionStrip(pl), [pl.execution ? pl.execution.headline.slice(0, 6) : '执行']);
  check('riskRows', api.hero(pl, S.stats, S.depth), ['波动大小', '最多投入', '大跌风险']);
  check('depthSegments', api.depthSegments(dp), ['开盘', '盘中', '全天', '近一年', '扣费后']);
  check('depthIntraday', api.depthIntraday(dp), ['中位', '平开', '盘中平均']);
  check('depthVol', api.depthVol(dp), ['更准', '平均最差', '形状']);
  check('depthRegime', api.depthRegime(dp), ['平时水平', '全部']);
  check('depthXsection', api.depthXsection(dp), ['可信度', '最好减最差']);
  check('depthSelection', api.depthSelection(dp), ['随便乱猜']);

  // 数值一致性：页面显示必须与后端数字同源
  const seg = (dp.segments || {}).segments || [];
  const ov = seg.find(x => x.key === 'overnight');
  if (ov && Math.abs(ov.mean_bp) < 0.01) { console.log('  ✗ 隔夜段均值为 0，疑似单位错'); bad++; }
  const vol = dp.vol || {};
  if (!(vol.forecast_vol_pct > 0 && vol.forecast_vol_pct < 20)) {
    console.log('  ✗ 波动预测超出合理区间:', vol.forecast_vol_pct); bad++;
  }
  const tail = vol.tail || {};
  if (tail.var95_bp != null && tail.var95_bp > 0) { console.log('  ✗ VaR95 应为负'); bad++; }
  if (tail.cvar95_bp != null && tail.cvar95_bp > tail.var95_bp) {
    console.log('  ✗ CVaR 应比 VaR 更负'); bad++;
  }
  const xs = (dp.xsection || {}).rows || [];
  if (xs.length && Math.abs(xs[0].spread_bp) > 200) {
    console.log('  ✗ 截面价差量级异常:', xs[0].spread_bp, 'bp'); bad++;
  }
  check('depthNav', api.depthNav(), DEPTH_TAB_HINT);
  check('rescuePanel', api.rescuePanel(S.rescue), ['指数位置', 'ETF 资金', '救市三条', '杠杆资金']);
  const rs = S.rescue || {};
  if ((rs.lights || []).length !== 5) { bad++; console.log('  ✗ 护盘灯应为 5 盏'); }
  if (typeof api.rescueBadge(rs) !== 'object') { bad++; console.log('  ✗ rescueBadge 未返回节点'); }
  const rowsI = (dp.intraday || {}).rows || [];
  if (rowsI.some(r => r.n > 0 && Math.abs(r.mean_bp || 0) > 300)) {
    console.log('  ✗ 条件日内均值量级异常'); bad++;
  }
}

// 表格行数校验
const rows = (t.match(/<tr>/g) || []).length;
console.log(`板块表行数: ${rows - 1} / 数据 ${pl.sectors_all.length}`);
if (rows - 1 !== pl.sectors_all.length) { bad++; console.log('✗ 行数与数据不一致'); }

// ETF 覆盖
const cov = pl.sectors_all.filter(x => x.etf).length;
console.log(`有 ETF 映射的板块: ${cov} / ${pl.sectors_all.length}`);
if (cov === 0) {
  // ETF 拉不到是数据源的问题，不是渲染的问题——测试不该因为"这一天源抖了"就判红，
  // 否则会和 kline(40) 那次一样：红灯与代码质量无关，最后没人再信它。
  // 该断言的是**界面有没有如实承认**这件事。
  const warned = (pl.degraded || []).some(d => String(d).includes('ETF'));
  if (!warned) { bad++; console.log('✗ ETF 全部缺失，但 degraded 里没有告警（会被误读成"今天没资金异动"）'); }
  else console.log('✓ ETF 全缺，但界面已告警（数据源问题，非渲染缺陷）');
}

// ---------------------------------------------------------------------------
// 页签集成测试
//
// 之前的测试全部是"把单个渲染函数调一遍"，从未真正执行 render()，也从未点过页签。
// 结果一次重构把 `const TAB_MAP` 的声明删掉后测试仍然全绿，而线上点击无反应：
// switchTab 抛 ReferenceError，高亮移了、面板没切。这里补上真实路径。
// ---------------------------------------------------------------------------
function makeNode(tag) {
  const cls = new Set();
  return {
    tagName: (tag || 'div').toUpperCase(), id: '', className: '', style: {},
    innerHTML: '', textContent: '', children: [], dataset: {}, onclick: null,
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll: () => [],
    scrollIntoView() {},
    classList: {
      add: c => cls.add(c),
      remove: c => cls.delete(c),
      toggle: (c, on) => { if (on === undefined) { cls.has(c) ? cls.delete(c) : cls.add(c); } else if (on) cls.add(c); else cls.delete(c); },
      contains: c => cls.has(c),
    },
    _cls: cls,
  };
}

const nodes = new Map();
const sel = s => { if (!nodes.has(s)) nodes.set(s, makeNode('div')); return nodes.get(s); };
const TABS = [
  Object.assign(makeNode('button'), { dataset: { tab: 'pred' } }),
  Object.assign(makeNode('button'), { dataset: { tab: 'sec' } }),
  Object.assign(makeNode('button'), { dataset: { tab: 'evo' } }),
];

// querySelector 必须和真实 DOM 一样：找不到就返回 null，且「页签选择器」要指向
// 同一个元素——否则 switchTab 给 b 加的 active 会加到一个新造的对象上，
// 测试反而测不到真实行为。
function query(sel2) {
  const m = /^\.tab\[data-tab="(.+)"\]$/.exec(sel2);
  if (m) return TABS.find(b => b.dataset.tab === m[1]) || null;
  if (sel2.startsWith('#')) return sel(sel2);
  return null;
}

global.document = {
  querySelector: query,
  querySelectorAll: s => (s === '.tab' ? TABS : []),
  createElement: t => makeNode(t),
  addEventListener() {},
};

let renderOk = true;
try { api.render(S); } catch (e) {
  renderOk = false; bad++;
  console.log(`✗ render() 抛异常: ${e.name}: ${e.message}`);
}
if (renderOk) {
  console.log('✓ render()  完整执行');

  const bound = TABS.filter(b => typeof b.onclick === 'function').length;
  if (bound !== 3) { bad++; console.log(`✗ 只绑定了 ${bound}/3 个页签的点击`); }
  else console.log('✓ 三个页签都绑定了 onclick');

  // 点击"板块"，断言面板显隐真的切了
  try {
    TABS[1].onclick();
    const predHidden = sel('#tab-pred')._cls.has('hidden');
    const secHidden = sel('#tab-sec')._cls.has('hidden');
    const evoHidden = sel('#tab-evo')._cls.has('hidden');
    if (!predHidden || secHidden || !evoHidden) {
      bad++;
      console.log(`✗ 点"板块"后面板状态错: pred hidden=${predHidden} sec hidden=${secHidden} evo hidden=${evoHidden}`);
    } else {
      console.log('✓ 点击页签后面板正确切换');
    }
    if (!TABS[1]._cls.has('active') || TABS[0]._cls.has('active')) {
      bad++; console.log('✗ 页签高亮态未更新');
    }
  } catch (e) {
    bad++;
    console.log(`✗ 点击页签抛异常: ${e.name}: ${e.message}`);
  }

  // 非法页签应安全返回 false，而不是抛错
  try {
    if (api.switchTab('nope') !== false) { bad++; console.log('✗ switchTab 对未知页签应返回 false'); }
  } catch (e) {
    bad++; console.log(`✗ switchTab('nope') 抛异常: ${e.name}: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// 自动刷新
// ---------------------------------------------------------------------------
const seq = [];
let cur = 0;
for (let i = 0; i < 6; i++) { cur = api.autoNext(cur); seq.push(cur); }
if (seq.join(',') !== '1,5,15,30,0,1') {
  bad++; console.log('✗ 自动刷新间隔序列错:', seq.join(','));
} else {
  console.log('✓ 自动刷新间隔循环 1→5→15→30→关');
}

// 定时器会拖住 node 进程，测试期间换成空实现
const realSet = global.setInterval, realClear = global.clearInterval;
global.setInterval = () => 0;
global.clearInterval = () => {};
try {
  const m = api.cycleAuto();          // 0 -> 1
  if (m !== 1) { bad++; console.log('✗ cycleAuto 应返回 1，实际', m); }
  api.cycleAuto(); api.cycleAuto(); api.cycleAuto(); api.cycleAuto();  // 回到 0
  if (!api.AUTO_STEPS.includes(m)) { bad++; console.log('✗ 循环值不在候选集内'); }
  console.log('✓ cycleAuto 可循环且不越界');
} finally {
  global.setInterval = realSet;
  global.clearInterval = realClear;
}

// ---------------------------------------------------------------------------
// 刷新重绘不能打断阅读：页签不跳、滚动位置保留
// ---------------------------------------------------------------------------
scrollCalls.length = 0;
api.switchTab('sec');                       // 默认应滚到顶部
if (!scrollCalls.some(a => a.length === 1 && a[0] && a[0].top === 0)) {
  bad++; console.log('✗ 切换页签未滚到顶部');
} else {
  console.log('✓ 切换页签滚到顶部');
}

scrollCalls.length = 0;
api.switchTab('sec', false);                // keep 模式不滚
if (scrollCalls.length) {
  bad++; console.log('✗ switchTab(key,false) 仍在滚动');
} else {
  console.log('✓ switchTab(key,false) 不滚动');
}

global.window.scrollY = 340;
scrollCalls.length = 0;
api.render(S, true);                        // 模拟自动刷新重绘
if (!scrollCalls.some(a => a[0] === 0 && a[1] === 340)) {
  bad++; console.log('✗ 刷新重绘未恢复滚动位置', JSON.stringify(scrollCalls));
} else {
  console.log('✓ 刷新重绘保留滚动位置');
}

// ---------------------------------------------------------------------------
// 数据源面板
//
// 这段代码在原切片点（#refresh 绑定）之后，所以以前的冒烟测试从来没跑到它。
// 面板的价值全在"数字对不对"：按钮上的 x/y 必须等于实际通过条数，任一条不通
// 必须变红，备用源缺成交额必须挂告警——这些都不能靠肉眼看。
// ---------------------------------------------------------------------------
{
  const raw = fs.readFileSync('web/app.js', 'utf8');
  const a = raw.indexOf('let _srcData');
  const b = raw.indexOf('(function initSrc()');
  const srcSeg = (a > 0 && b > a) ? raw.slice(a, b) : '';
  if (!srcSeg) {
    bad++; console.log('✗ 找不到数据源面板代码段');
  } else {
    const sapi = {};
    try {
      // `$` 原本定义在上一次 eval 的作用域里，这里要重新给它一份
      eval('const $ = (s,r=document)=>r.querySelector(s);\n' + srcSeg
        + '\nsapi.paintSrc=paintSrc;sapi.srcDot=srcDot;'
        + 'sapi.setData=d=>{_srcData=d;};sapi.loadSrc=loadSrc;');
    } catch (e) {
      bad++; console.log(`✗ 数据源段语法/求值失败: ${e.name}: ${e.message}`);
    }
    const btn = sel('#src'), panel = sel('#srcpanel');

    // 1) 没有数据时不乱写，退回朴素文案
    btn.textContent = ''; btn.innerHTML = '';
    sapi.paintSrc();
    if (btn.textContent !== '数据源') { bad++; console.log('✗ 无数据时按钮应显示「数据源」，实际:', btn.textContent); }
    else console.log('✓ 无数据时按钮退回「数据源」');

    // 2) 全通
    const mk = (name, group, ok = true) => ({ name, group, ok, ms: ok ? 220 : 0, note: '' });
    const all = [
      mk('日线行情', '行情'), mk('全市场快照', '行情'), mk('板块与成分', '行情'),
      mk('ETF 行情', '行情'), mk('涨跌停家数', '行情'), mk('隔夜外盘', '行情'),
      mk('7×24 快讯', '情绪'), mk('两融余额', '资金'), mk('ETF 份额', '资金'),
      mk('尾盘分钟线', '资金'), mk('备用源（腾讯）', '备用'),
    ];
    sapi.setData({ at: '08:53:25', items: all, ok: 11, total: 11, degraded: [], fallback: { kline: false } });
    panel.classList.remove('hidden');
    sapi.paintSrc();

    if (!btn.innerHTML.includes('11/11')) { bad++; console.log('✗ 按钮未显示 11/11'); }
    else console.log('✓ 全通时按钮显示 11/11');
    if (btn.classList.contains('warn')) { bad++; console.log('✗ 全通时按钮不应带 warn'); }
    if (!btn.innerHTML.includes('sdot ok')) { bad++; console.log('✗ 全通时指示灯应为绿'); }

    const rows = (panel.innerHTML.match(/class="sp-row"/g) || []).length;
    if (rows !== 11) { bad++; console.log(`✗ 面板应列 11 条，实际 ${rows}`); }
    else console.log('✓ 面板列出 11 条数据源');
    const groups = (panel.innerHTML.match(/class="sp-g"/g) || []).length;
    if (groups !== 4) { bad++; console.log(`✗ 应分 4 组，实际 ${groups}`); }
    else console.log('✓ 面板按 4 组归类');
    if (panel.innerHTML.includes('sp-warn')) { bad++; console.log('✗ 无降级时不应出现告警条'); }
    if (/undefined|NaN|\[object Object\]|eastmoney|push2|http/.test(panel.innerHTML)) {
      bad++; console.log('✗ 面板泄露了端点或异常值');
    } else {
      console.log('✓ 面板只给中文名与耗时，未泄露端点');
    }

    // 3) 有一条不通：按钮变红、数字跟着降
    const deg = all.map(x => x.name === 'ETF 份额' ? mk('ETF 份额', '资金', false) : x);
    sapi.setData({ at: '08:54', items: deg, ok: 10, total: 11, degraded: ['ETF 份额'], fallback: { kline: false } });
    sapi.paintSrc();
    if (!btn.classList.contains('warn')) { bad++; console.log('✗ 有源不通时按钮应变红'); }
    else console.log('✓ 有源不通时按钮变红');
    if (!btn.innerHTML.includes('10/11')) { bad++; console.log('✗ 按钮未显示 10/11'); }
    if (!panel.innerHTML.includes('不通')) { bad++; console.log('✗ 不通的源应显示「不通」'); }
    else console.log('✓ 不通的源标为「不通」');

    // 4) 日线落到备用源：必须挂告警，否则用户以为成交额还在
    sapi.setData({ at: '08:55', items: all, ok: 11, total: 11, degraded: [], fallback: { kline: true } });
    sapi.paintSrc();
    if (!panel.innerHTML.includes('成交额缺失')) { bad++; console.log('✗ 备用源生效时缺少告警'); }
    else console.log('✓ 日线用备用源时提示成交额缺失');

    // 5) 面板关闭时只更新按钮，不重建内容（避免无谓重排）
    panel.classList.add('hidden');
    const before = panel.innerHTML;
    sapi.paintSrc();
    if (panel.innerHTML !== before) { bad++; console.log('✗ 面板关闭时不应重绘内容'); }
    else console.log('✓ 面板关闭时只更新按钮');
  }
}

// ---------------------------------------------------------------------------
// 数据状况提示（降级 / 陈旧 / 日历过期）
//
// 这三条对应三种"结论其实打折了"的情况。以前后端算了 degraded 但前端根本不读，
// 模型自认在瞎猜时用户看到的仍是干净的 54%。这里逐条卡住。
// ---------------------------------------------------------------------------
{
  const mkPl = over => Object.assign({}, pl, over);

  // 1) 一切正常 → 不出现提示条（不能平白多一行噪音）
  let h = api.dataNotes(mkPl({ degraded: [], target: '2099-01-01' }), {});
  if (h) { bad++; console.log('✗ 无异常时不应出现提示条，实际:', h.slice(0, 80)); }
  else console.log('✓ 无异常时不显示提示条');

  // 2) 后端降级原因要原样透出
  h = api.dataNotes(mkPl({ degraded: ['隔夜外盘缺失，跳空按均值处理'], target: '2099-01-01' }), {});
  if (!h.includes('隔夜外盘缺失')) { bad++; console.log('✗ 降级原因未显示'); }
  else console.log('✓ 降级原因显示到界面');

  // 3) 多条降级都要显示，不能只取第一条
  h = api.dataNotes(mkPl({
    degraded: ['隔夜外盘缺失，跳空按均值处理', '学习样本不足，权重接近先验'],
    target: '2099-01-01' }), {});
  if (!(h.includes('隔夜外盘缺失') && h.includes('学习样本不足'))) {
    bad++; console.log('✗ 多条降级原因未全部显示');
  } else console.log('✓ 多条降级原因全部显示');

  // 4) 目标日已过 → 必须提示陈旧
  h = api.dataNotes(mkPl({ degraded: [], target: '2020-01-02' }), {});
  if (!h.includes('已经过去')) { bad++; console.log('✗ 目标日过期未提示'); }
  else console.log('✓ 目标日过期时提示陈旧');

  // 5) 目标日就是今天 → 不算过期（当天早上看是正常场景）
  const today = new Date(); const ymd = today.toISOString().slice(0, 10);
  h = api.dataNotes(mkPl({ degraded: [], target: ymd }), {});
  if (h.includes('已经过去')) { bad++; console.log('✗ 目标日为今天不应判过期'); }
  else console.log('✓ 目标日为今天时不判过期');

  // 6) 交易日历过期要提示，且用醒目样式
  h = api.dataNotes(mkPl({ degraded: [], target: '2099-01-01' }),
                    { calendar: { stale: true, msg: '交易日历只写到了 2027 年底' } });
  if (!h.includes('交易日历')) { bad++; console.log('✗ 日历过期未提示'); }
  else if (!h.includes('dn cal')) { bad++; console.log('✗ 日历过期应使用告警样式'); }
  else console.log('✓ 交易日历过期时提示');

  // 7) 字段缺失不能炸（老快照 / 首次加载）
  try {
    const h1 = api.dataNotes({}, {});
    const h2 = api.dataNotes(null, null);
    if (h1 || h2) { bad++; console.log('✗ 字段缺失时应返回空串'); }
    else console.log('✓ 字段缺失时安全返回空');
  } catch (e) {
    bad++; console.log(`✗ 字段缺失时抛异常: ${e.name}: ${e.message}`);
  }

  // 8) hero 里必须真的挂上提示条（不能只是有个函数没人调）
  const hh = api.hero(mkPl({ degraded: ['隔夜外盘缺失，跳空按均值处理'], target: '2099-01-01' }), {}, {});
  if (!hh.innerHTML.includes('dnote')) { bad++; console.log('✗ hero 未渲染提示条'); }
  else console.log('✓ hero 中确实渲染了提示条');

  // 8b) 构建失败：必须说出来"这是上一次的快照"，且用醒目样式
  h = api.dataNotes(mkPl({ degraded: [], target: ymd }),
                    { build_error: 'RuntimeError: 板块数据只拿到 0 个\nTraceback (most recent call last):' });
  if (!h.includes('上一次的完整快照')) {
    bad++; console.log('✗ 构建失败时应说明当前显示的是旧快照');
  } else if (!h.includes('dn err')) {
    bad++; console.log('✗ 构建失败应使用醒目样式（dn err）');
  } else if (h.includes('Traceback')) {
    bad++; console.log('✗ 提示条不应把 traceback 整段倒给用户');
  } else console.log('✓ 构建失败时提示当前为旧快照且样式醒目');

  // ---------------- 个股概率校准状态 ----------------
  // 三种状态必须能区分：未校准 / 区分度不足 / 已校准。
  // 这三句话的可信度差一个量级，混成一句就是误导。
  // 9) 样本不足时显示"还没校准"，并给出进度 n/need
  h = api.stockCalibNote({ ready: false, n_seen: 18, min_n: 40 });
  if (!h.includes('还没校准')) { bad++; console.log('✗ 样本不足时应标"还没校准"'); }
  else if (!h.includes('18/40')) { bad++; console.log('✗ 样本不足时应显示进度 18/40'); }
  else if (!h.includes('公式原值')) { bad++; console.log('✗ 未校准时应说明是公式原值'); }
  else console.log('✓ 样本不足时标"还没校准"并显示进度');

  // 10) 已达到样本量但区分度不足，必须明说"没有稳定预测力"
  h = api.stockCalibNote({ ready: true, weak: true, n: 120, n_seen: 120, min_n: 40,
                           hit_rate: 0.48, brier: 0.2433, brier_raw: 0.3111 });
  if (!h.includes('区分度不足')) { bad++; console.log('✗ weak 时应标"区分度不足"'); }
  else if (!h.includes('稳定预测力')) { bad++; console.log('✗ 区分度不足时应说明预测力问题'); }
  else if (h.includes('还没校准')) { bad++; console.log('✗ weak 状态不应与"未校准"混为一谈'); }
  else console.log('✓ 区分度不足时给出明确结论');

  // 11) 正常校准态：给命中率与两个 Brier，读者能自己核对是否真变好
  h = api.stockCalibNote({ ready: true, weak: false, n: 200, n_seen: 200, min_n: 40,
                           hit_rate: 0.53, brier: 0.2412, brier_raw: 0.2528 });
  if (!h.includes('已按 200 个实测样本校准')) { bad++; console.log('✗ 正常态未说明样本数'); }
  else if (!h.includes('53')) { bad++; console.log('✗ 正常态未显示命中率'); }
  else if (!h.includes('0.2412') || !h.includes('0.2528')) {
    bad++; console.log('✗ 正常态应同时给出校准前后误差');
  } else console.log('✓ 正常校准态显示命中率与前后误差');

  // 12) 缺字段/老快照不能炸
  try {
    const a1 = api.stockCalibNote(null);
    const a2 = api.stockCalibNote({});
    if (a1) { bad++; console.log('✗ sc 为空时应返回空串'); }
    else if (!a2.includes('还没校准')) { bad++; console.log('✗ 空对象应退化为"未校准"'); }
    else console.log('✓ 个股校准提示对缺失字段安全');
  } catch (e) {
    bad++; console.log(`✗ 个股校准提示抛异常: ${e.name}: ${e.message}`);
  }

  // 13) stockTable 必须真的把说明渲染出来（不能只有函数没人调）
  const st = api.stockTable(
    [{ code: '600000', name: '测试股', sector: '测试板块', prob: 0.66, pct: 3.2, reasons: ['比板块多涨 2.1%'] }],
    { ready: false, n_seen: 5, min_n: 40 });
  if (!st.innerHTML.includes('scnote')) { bad++; console.log('✗ stockTable 未渲染校准说明'); }
  else if (!st.innerHTML.includes('测试股')) { bad++; console.log('✗ stockTable 丢了股票行'); }
  else console.log('✓ stockTable 渲染了校准说明与股票行');

  // ---------------- 名称炒作提示 ----------------
  // 14) 有主题时给出字与只数
  h = api.nameThemeNote([{ label: '洋', n: 5, lift: 6.9, names: ['东方海洋', '百洋股份'] }]);
  if (!h.includes('「洋」') || !h.includes('5只')) {
    bad++; console.log('✗ 名称主题未显示标签与只数');
  } else console.log('✓ 名称主题显示字与只数');

  // 15) 必须明确"未纳入概率计算"，否则读者会以为系统在推荐这些票
  if (!h.includes('未纳入概率计算')) {
    bad++; console.log('✗ 名称炒作提示必须声明不参与预测');
  } else if (!h.includes('不是行业逻辑')) {
    bad++; console.log('✗ 名称炒作提示应说明它不是行业逻辑');
  } else console.log('✓ 名称炒作提示声明了不参与预测');

  // 16) 最多显示两个，避免堆成噪音
  h = api.nameThemeNote([
    { label: '洋', n: 5 }, { label: '芯', n: 7 }, { label: '杰', n: 4 }, { label: '超', n: 3 }]);
  if ((h.match(/「/g) || []).length > 2) { bad++; console.log('✗ 名称主题应最多显示两个'); }
  else console.log('✓ 名称主题最多显示两个');

  // 17) 空输入/缺字段不炸，也不输出空壳
  try {
    const n1 = api.nameThemeNote(null);
    const n2 = api.nameThemeNote([]);
    const n3 = api.nameThemeNote([{}]);
    if (n1 || n2 || n3) { bad++; console.log('✗ 无主题时应返回空串'); }
    else console.log('✓ 无名称主题时安全返回空');
  } catch (e) {
    bad++; console.log(`✗ 名称主题提示抛异常: ${e.name}: ${e.message}`);
  }

  // 18) stockTable 要把主题提示渲染出来
  const st2 = api.stockTable(
    [{ code: '600000', name: '测试股', sector: '测试板块', prob: 0.66, pct: 3.2, reasons: [] }],
    { ready: false, n_seen: 5, min_n: 40 },
    [{ label: '洋', n: 5 }]);
  if (!st2.innerHTML.includes('名称共性')) { bad++; console.log('✗ stockTable 未渲染名称主题'); }
  else console.log('✓ stockTable 渲染了名称主题提示');

  // ---------------- 走势结构（缠论） ----------------
  // 19) 正常结构要显示"这一段涨到哪""上方什么位置有压力""最近提醒""过去准不准"
  const chanOk = api.chanPanel({ chan: {
    bis: 76, zhongshus: 12,
    last_bi: { dir: 'down', from: '2026-10-09', to: '2026-11-06', high: 3701.59, low: 3574.27, confirmed: true },
    last_zs: { ZG: 4175.35, ZD: 4002.78, from: '2026-01-14', confirmed: true },
    signals_total: 24,
    buy: { n: 14, mean: 1.88, win: 0.929 },
    sell: { n: 10, mean: -0.88, win: 0.2 },
    recent: { kind: 'sell3', date: '2026-11-06', price: 3701.59, reason: '跌破中枢', age: 5, is_buy: false },
  } });
  const ch = chanOk.innerHTML || '';
  // 术语只能出现在**解释句**里（"…（也就是"背驰"）"），不能当主文案。
  // 所以检查范围排除 cnote 那段说明文字。
  const chBody = ch.split('cnote')[0];
  const jargon = ['中枢', '三卖', '一买', '背驰', '笔段'].filter(w => chBody.includes(w));
  if (jargon.length) {
    bad++; console.log(`✗ 走势结构面板把术语当主文案：${jargon.join('、')}`);
  } else console.log('✓ 走势结构面板用日常语言描述，术语只留在解释句里');

  if (!ch.includes('从') || (!ch.includes('涨到') && !ch.includes('跌到'))) {
    bad++; console.log('✗ 走势结构没说明这一段从哪到哪（读者不知道那些数字是什么）');
  } else console.log('✓ 走势结构说明了两段区间与方向');

  if (!ch.includes('解套') && !ch.includes('支撑')) {
    bad++; console.log('✗ 区间没有给出"所以呢"（压力/支撑的含义）');
  } else console.log('✓ 区间给了解套盘/支撑的含义');

  // 20) 必须明说"不参与概率"——否则读者会把 93% 胜率当成模型结论
  if (!ch.includes('不参与涨跌概率')) {
    bad++; console.log('✗ 走势结构面板必须声明不参与概率');
  } else console.log('✓ 走势结构面板声明不参与概率');

  // 21) 样本量必须露出来，不能只给胜率
  if (!ch.includes('14 次')) {
    bad++; console.log('✗ 走势结构面板未标出样本量');
  } else console.log('✓ 走势结构面板标出样本量');

  // 22) 没有数据时不能炸，也不能显示 undefined
  try {
    const e1 = api.chanPanel({});
    const e2 = api.chanPanel({ chan: {} });
    const e3 = api.chanPanel(null);
    const bad3 = [e1, e2, e3].some(n => /undefined|NaN/.test(n.innerHTML || ''));
    if (bad3) { bad++; console.log('✗ 缠论面板在缺数据时输出 undefined/NaN'); }
    else console.log('✓ 缠论面板缺数据时安全降级');
  } catch (e) {
    bad++; console.log(`✗ 缠论面板缺数据时抛异常: ${e.name}: ${e.message}`);
  }

  // ---------------- 首次打开必须自动等到构建完成 ----------------
  // /api/state 只要内存里有数据就返回 200，即使服务端正在后台重算。
  // 服务启动时 warm_start 一定会重算一次（约 50 秒），于是首次打开会立刻
  // 拿到上一次的快照。若前端就此停住，用户看到的是旧数据且不会自己变
  // （自动刷新默认关闭）——这正是"首次打开不会自动刷新"的成因。
  (async () => {
    let polled = false;
    const realSetInterval = global.setInterval;
    global.setInterval = (fn, ms) => { polled = true; return 1; };

    const run = async (building, error) => {
      polled = false;
      global.fetch = async (url) => {
        const u = String(url);
        if (u.includes('/api/state')) return { status: 200, json: async () => S };
        if (u.includes('/api/status')) return { status: 200, json: async () => ({ building, error }) };
        return { status: 200, json: async () => ({}) };
      };
      await api.load();
      return polled;
    };

    try {
      const p1 = await run(true, null);
      if (!p1) { bad++; console.log('✗ 后台构建中仍应继续轮询（否则停在旧快照）'); }
      else console.log('✓ 后台构建中会继续轮询等待新数据');

      const p2 = await run(false, null);
      if (p2) { bad++; console.log('✗ 构建已完成时不应继续轮询（白耗请求）'); }
      else console.log('✓ 构建已完成时不再轮询');

      // 构建失败：不能静默拿旧快照充数，要挂出失败原因
      const p3 = await run(false, 'RuntimeError: 板块数据只拿到 0 个');
      if (p3) { bad++; console.log('✗ 构建失败时不应继续轮询'); }
      else if (!(S.meta && S.meta.build_error)) {
        bad++; console.log('✗ 构建失败未写入 meta.build_error（界面将无从提示）');
      } else console.log('✓ 构建失败会把原因带进界面');
    } catch (e) {
      bad++; console.log(`✗ load() 首次打开逻辑抛异常: ${e.name}: ${e.message}`);
    } finally {
      global.setInterval = realSetInterval;
      console.log(bad ? `\n${bad} 项未通过` : '\n全部通过');
      process.exit(bad ? 1 : 0);
    }
  })();
}
