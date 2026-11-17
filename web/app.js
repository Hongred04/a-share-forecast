/* A股预判 · 前端逻辑
   无框架、无构建、无外部依赖。结构：
     工具函数 → 状态与加载 → 页签与路由 → 各区块渲染函数 → 事件绑定
   数据全部来自 GET /api/state 的单个 JSON，字段契约见 tests/contract.py。*/

const $ = (s,r=document)=>r.querySelector(s);
const nf=(v,d=2)=>(v==null||isNaN(v))?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const sg=(v,d=2)=>(v==null||isNaN(v))?'—':(v>0?'+':'')+nf(v,d);
const pc=v=>(v==null||isNaN(v))?'—':(v*100).toFixed(1)+'%';
const cc=v=>(v==null||isNaN(v))?'':(v>0?'up':(v<0?'down':''));
let S=null, poll=null;
let AUTO_MIN=0, _autoTimer=null, _busy=false, _busyTimer=null, _lastLoad=0, _noteTimer=null;

/* 忙碌态：顶部细进度条 + 按钮禁用。
   注意不要假设 #loading 一定存在——首次渲染后它就被替换掉了，
   旧代码在这里直接写 #loading.textContent，刷新时会抛异常把轮询打断。 */
function setBusy(on, text){
  const p=$('#prog'); if(p) p.classList.toggle('on', !!on);
  ['#refresh','#rebuild'].forEach(id=>{
    const b=$(id); if(b){ b.disabled=!!on; b.style.opacity = on?'.55':''; }
  });
  const el=$('#loading'); if(el && text) el.textContent=text;
}

async function load(){
  try{
    const r=await fetch('/api/state');
    if(r.status===202){
      const j=await r.json();
      setBusy(true, j.error ? '计算失败：'+String(j.error).slice(0,240)
        : '正在拉取行情并计算…'+(j.log?'（'+j.log+'）':''));
      setTimeout(load,1800); return;
    }
    const keep = !!S;              // 已有内容 → 这是刷新，重绘时保留滚动位置与页签
    S = await r.json();
    clearTimeout(_busyTimer); _busyTimer=null;
    _lastLoad=Date.now();

    /* /api/state 只要内存里有数据就返回 200，即使服务端此刻正在后台重算。
       而服务启动时 warm_start 一定会重算一次（约 50 秒）。于是首次打开会
       立刻拿到**上一次的快照**并停在这里——用户看到的是旧数据，
       还以为已经刷新过了（自动刷新默认是关的，更不会自己变）。

       所以渲染前先确认一次构建状态，两种情况都要如实反映：
         · 正在构建 → 先把旧快照显示出来，然后轮询等新的，好了自动换；
         · 上次构建失败 → 旧快照照旧显示，但在提示条里挂出失败原因。
       状态查不到就按"什么都没发生"处理，不能因此不渲染。 */
    let st=null;
    try{ st = await (await fetch('/api/status')).json(); }catch(e){}
    if(st && st.error){
      S.meta = Object.assign({}, S.meta, { build_error: st.error });
    }else if(st && st.building){
      S.meta = Object.assign({}, S.meta, { building_now: true });
    }

    render(S, keep); stopPoll();

    if(st && st.building && !st.error){
      _busy=true;
      setBusy(true, '后台正在更新，完成后自动显示新数据…');
      // 同样要看门狗：构建异常时不能把 _busy 永久卡在 true
      clearTimeout(_busyTimer);
      _busyTimer=setTimeout(()=>{ _busy=false; _busyTimer=null; setBusy(false); }, 180000);
      startPoll();
    }else{
      _busy=false; setBusy(false);
    }
  }catch(e){
    setBusy(true, '未连接本地服务，请运行 python run.py serve');
    setTimeout(load,3000);
  }
}
function stopPoll(){ if(poll){clearInterval(poll);poll=null;} }
function startPoll(){ stopPoll(); poll=setInterval(async()=>{
  try{ const j=await (await fetch('/api/status')).json(); if(!j.building){ stopPoll(); load(); } }catch(e){}
},2000); }

/* ---------------- 自动刷新 ----------------
   按钮循环切换间隔，选择记在 localStorage。三条纪律：
     1. 后台标签页不刷——省数据源，也避免夜里空转；
     2. 构建中不叠加，且带 3 分钟看门狗，防止某次构建异常把定时器永久卡死；
     3. 重绘必须保留滚动位置与当前页签（见 render 的 keep 参数），
        否则每 N 分钟把人弹回顶部，等于不能用。 */
const AUTO_STEPS=[0,1,5,15,30];          // 分钟；0 = 关

function autoLabel(m){ return m ? `自动 ${m} 分` : '自动 关'; }
function autoNext(cur){                  // 纯函数，便于测试
  const i=AUTO_STEPS.indexOf(cur);
  return AUTO_STEPS[(i+1)%AUTO_STEPS.length];
}

function applyAuto(){
  if(_autoTimer){ clearInterval(_autoTimer); _autoTimer=null; }
  if(AUTO_MIN>0){
    _autoTimer=setInterval(()=>{
      if(document.hidden || _busy) return;
      refreshNow();
    }, AUTO_MIN*60000);
  }
  const b=$('#auto');
  if(b){
    b.textContent=autoLabel(AUTO_MIN);
    b.classList.toggle('on', AUTO_MIN>0);
    b.title = AUTO_MIN ? `已开启：每 ${AUTO_MIN} 分钟拉取最新行情（点击切换间隔）`
                       : '自动刷新已关闭（点击开启）';
  }
}

function cycleAuto(){
  AUTO_MIN=autoNext(AUTO_MIN);
  try{ localStorage.setItem('autoMin', String(AUTO_MIN)); }catch(e){}
  applyAuto();
  return AUTO_MIN;
}

async function refreshNow(){
  if(_busy) return false;
  _busy=true; setBusy(true);
  clearTimeout(_busyTimer);
  // 看门狗：构建卡住或请求失败时，别让 _busy 永久为 true
  _busyTimer=setTimeout(()=>{ _busy=false; _busyTimer=null; setBusy(false); }, 180000);
  try{
    const r = await fetch('/api/refresh',{method:'POST'});
    const j = await r.json().catch(()=>({}));
    /* 收盘后服务端会跳过重算（行情已定，再算一遍没有任何不同）。
       这不是错误，也不是"正在更新"——必须说清楚，否则按钮点下去一片安静，
       用户会以为坏了，然后反复点。 */
    if(j && j.skipped){
      _busy=false; clearTimeout(_busyTimer); _busyTimer=null; setBusy(false);
      flashNote(j.msg || '收盘后数据已定，无需重算');
      return false;
    }
    startPoll();
    return true;
  }catch(e){
    _busy=false; clearTimeout(_busyTimer); _busyTimer=null; setBusy(false);
    return false;
  }
}

/* 一次性提示：挂在 hero 提示条里，几秒后自动消失。
   没有独立的 toast 层，也不想为一句"不用重算"再引进一个组件。 */
function flashNote(txt){
  if(!S) return;
  S.meta = Object.assign({}, S.meta, { notice: String(txt) });
  render(S, true);
  clearTimeout(_noteTimer);
  _noteTimer=setTimeout(()=>{
    if(S && S.meta){ delete S.meta.notice; render(S, true); }
  }, 5000);
}

function currentTab(){
  const b=document.querySelector('.tab.active');
  return (b && b.dataset.tab) || 'pred';
}

function render(S, keep){
  const prevTab = keep ? currentTab() : 'pred';
  const prevY   = keep ? (window.scrollY || 0) : 0;

  window.__meta = S.meta || {};
  window.__depth = S.depth || {};
  window.__spark = S.spark || {};
  const m=S.meta, st=S.stats||{}, pl=S.prediction, dp=S.depth||{};
  $('#phase').textContent = m.phase||'—';
  $('#dot').className = 'dot'+(m.phase==='盘中'||m.phase==='集合竞价'?' on':'');
  $('#meta').textContent = `v${m.version||0} · ${(m.generated_at||'').slice(5,16)}`;

  const app=$('#app'); app.innerHTML='';
  app.appendChild(tabBar());

  /* ---- 预判 ---- */
  const p1=document.createElement('div'); p1.id='tab-pred';
  p1.appendChild(hero(pl,st,dp));
  p1.appendChild(executionStrip(pl));
  p1.appendChild(rescueBadge(S.rescue||{}));
  p1.appendChild(kpis(pl));
  p1.appendChild(block('板块', sectors(pl), 'blk-sector', {label:'全部板块 →', tab:'sec'}));
  if((pl.stocks||[]).length) p1.appendChild(block('个股', stockTable(pl.stocks, pl.stock_calib, pl.name_themes), 'blk-stocks'));

  /* ---- 板块 ---- */
  const p2=document.createElement('div'); p2.id='tab-sec'; p2.className='hidden';
  p2.appendChild(block('全板块', sectorTable(pl)));
  p2.appendChild(block('ETF 资金', etfPanel(pl)));

  /* ---- 深度 ---- */
  const p3=document.createElement('div'); p3.id='tab-evo'; p3.className='hidden';
  if(dp.error){
    const err=document.createElement('div'); err.className='card pad';
    err.innerHTML=`<div class="k">深度层计算失败：${dp.error}</div>`;
    p3.appendChild(err);
  }else{
    p3.appendChild(depthNav());
    p3.appendChild(block('涨跌是怎么来的', stack(depthSegments(dp), depthIntraday(dp)), 'sec-1'));
    p3.appendChild(block('波动与极端行情', depthVol(dp), 'sec-2'));
    p3.appendChild(block('什么情况下更准', stack(depthRegime(dp), depthXsection(dp)), 'sec-3'));
    p3.appendChild(block('能力边界与自我修正',
      stack(metrics(st), depthSelection(dp), evolutionPanel(S), weightCard(S)), 'sec-4'));
    p3.appendChild(block('国家队护盘信号', rescuePanel(S.rescue||{}), 'sec-5'));
    p3.appendChild(block('上一次预判准不准', stack(review(S), logs(S.errors||[])), 'sec-6'));
    // 没有缠论数据就整节不出现——挂一块"数据不足"只会增加噪音
    if(pl.chan && pl.chan.bis) p3.appendChild(block('走势结构', chanPanel(pl), 'sec-7'));
  }

  app.appendChild(p1); app.appendChild(p2); app.appendChild(p3);
  bindTabs();

  if(keep){
    // 刷新重绘：回到原来那一页、原来的位置，不打断阅读
    switchTab(prevTab, false);
    window.scrollTo(0, prevY);
  }else{
    const want=(new URLSearchParams(location.search).get('tab')||'').toLowerCase();
    const w=({depth:'evo'})[want]||want;
    if(w && TAB_MAP[w]) switchTab(w, true);
    // 锚点要等渲染完才存在（内容是 fetch 回来才生成的），
    // 浏览器解析 URL 时这里还是空的，所以在这里补一次
    if(location.hash){
      const el=document.getElementById(location.hash.slice(1));
      if(el) el.scrollIntoView({block:'start'});
    }
  }
}

function tabBar(){
  const d=document.createElement('div'); d.className='tabbar';
  d.innerHTML=`<div class="tabs">
      <button class="tab active" data-tab="pred">明天</button>
      <button class="tab" data-tab="sec">板块</button>
      <button class="tab" data-tab="evo">依据</button>
    </div>`;
  return d;
}

/* 页签 -> 面板容器 id。重构时这行声明曾被一起删掉，导致 switchTab 抛
   ReferenceError：高亮会移动但面板不切换。现在由 tests/ui_smoke.js 兜住。 */
const TAB_MAP={pred:'tab-pred',sec:'tab-sec',evo:'tab-evo'};

function switchTab(key, scroll){
  const b=document.querySelector(`.tab[data-tab="${key}"]`);
  if(!b) return false;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  Object.entries(TAB_MAP).forEach(([k,id])=>$('#'+id).classList.toggle('hidden', key!==k));
  // 让页签可分享：写回 URL，但不产生历史记录
  try{
    const u=new URL(location.href);
    u.searchParams.set('tab', key==='evo'?'depth':key);
    history.replaceState(null,'',u);
  }catch(e){}
  if(scroll!==false) window.scrollTo({top:0,behavior:'smooth'});
  return true;
}
let _keysBound=false;
function bindTabs(){
  document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
  // 1/2/3 直接切页签，省一次点击。
  // 只绑一次：render() 每次刷新都会调用本函数，重复绑会导致按键触发 N 次。
  if(_keysBound) return;
  _keysBound=true;
  document.addEventListener('keydown', e=>{
    if(e.metaKey||e.ctrlKey||e.altKey||e.shiftKey) return;
    const tag=(e.target&&e.target.tagName||'').toLowerCase();
    if(tag==='input'||tag==='textarea'||tag==='select') return;
    const k={'1':'pred','2':'sec','3':'evo'}[e.key];
    if(k && switchTab(k)) e.preventDefault();
  });
}
function secLabel(t,id,more){
  const d=document.createElement('div'); d.className='sec';
  if(id) d.id=id;
  d.innerHTML=`<h2>${t}</h2>${more?`<button class="more">${more.label}</button>`:''}`;
  // fakeEl（冒烟测试）没有 querySelector，这里不能无条件调用
  if(more && d.querySelector) d.querySelector('.more').onclick=()=>switchTab(more.tab);
  return d;
}

/* 一节 = 标题 + 内容。统一包一层，正文里就不再出现"这一节在讲什么"的说明句 */
function block(title,node,id,more){
  const s=document.createElement('section'); s.className='blk';
  s.appendChild(secLabel(title,id,more));
  if(node) s.appendChild(node);
  return s;
}

function stack(){
  const d=document.createElement('div');
  for(let i=0;i<arguments.length;i++){ if(arguments[i]) d.appendChild(arguments[i]); }
  return d;
}

/* 深度页 6 节，按"依据 / 系统"两组分段。
   原来是 8 节：顶部导航 8 个按钮（挤成两行）+ 正文又是同样 8 个标题，
   同一批词要读两遍，这是"杂"的主要来源。
   合并原则是**同一类问题放一起**，不是为凑数硬砍：
     · 「不同行情的胜率」和「哪些指标管用」都是条件分布 → 一节
     · 「做不到什么」和「怎么自我修正」是一体两面（承认边界才需要纠错）→ 一节 */
const DEPTH_GROUPS=[
  ['依据',[['sec-1','涨跌从哪来'],['sec-2','波动与尾部'],['sec-3','什么情况更准']]],
  ['系统',[['sec-4','能力与修正'],['sec-5','护盘信号'],['sec-6','上次准不准'],['sec-7','走势结构']]],
];
const DEPTH_SECTIONS=DEPTH_GROUPS.flatMap(([,items])=>items);
function depthNav(){
  const d=document.createElement('div'); d.className='subnav';
  d.innerHTML=DEPTH_GROUPS.map(([g,items])=>`<div class="nvg"><span class="ng">${g}</span>
      ${items.map(([id,t])=>`<button data-go="${id}">${t}</button>`).join('')}</div>`).join('');
  if(d.querySelectorAll) d.querySelectorAll('[data-go]').forEach(b=>{
    b.onclick=()=>{ const el=document.getElementById(b.dataset.go);
      if(el) el.scrollIntoView({behavior:'smooth',block:'start'}); };
  });
  return d;
}

/* ---------------- 走势结构（缠论） ----------------
   这里最大的问题不是术语多，是**没有主语**：
   "最后一笔 向上 3574→3701"——什么笔？这两个数是什么？
   "最近中枢 4002~4175"——中枢是什么？跟我有什么关系？

   所以每个数字后面都要跟着"所以呢"：
   上方那个区间意味着解套盘，下方那个意味着支撑，这才是读者要的信息。
   术语只在括号里保留一份，给本来就懂的人对得上号。

   它不参与概率：回测里买点后 3 日确有 +1.88% 超额（n=14），
   但 5.3 年只有 24 个信号，这个量级估不出可信权重。 */
const CHAN_NAME={
  buy1:'跌不动了', buy2:'二次探底没破', buy3:'回调不破',
  sell1:'涨不动了', sell2:'二次冲高没破', sell3:'跌破后反抽不上',
  pzbuy:'在震荡区间底部', pzsell:'在震荡区间顶部'};

/* 中枢本身没有方向含义，含义由"现价在它哪一侧"决定 */
function zsWords(zs, price){
  if(!zs) return '';
  if(price < zs.ZD) return `${zs.ZD}~${zs.ZG} 是之前反复来回的区间，涨上去会遇到解套盘`;
  if(price > zs.ZG) return `${zs.ZD}~${zs.ZG} 是之前反复来回的区间，跌下来这里有支撑`;
  return `${zs.ZD}~${zs.ZG}，现在还没走出这个区间`;
}

function chanPanel(pl){
  const c=document.createElement('div'); c.className='card pad';
  const k=(pl&&pl.chan)||{};
  if(!k.bis){
    c.innerHTML='<div class="faint" style="font-size:12.5px">日线数据不足，算不出走势结构</div>';
    return c;
  }
  const row=(t,v)=>`<div class="crow"><span class="ct">${t}</span><span class="cv">${v}</span></div>`;
  const bi=k.last_bi||{};
  const zs=k.last_zs;
  const rc=k.recent;
  const b=k.buy||{}, s=k.sell||{};
  // 点位取整：3574.27 对读者没有额外信息，3574 更好读
  const iP=v=>Math.round(Number(v));
  const now = iP(bi.dir==='up' ? bi.high : bi.low);
  const move = bi.dir==='up'
    ? `从 ${iP(bi.low)} 涨到 ${iP(bi.high)}`
    : `从 ${iP(bi.high)} 跌到 ${iP(bi.low)}`;
  const going = bi.confirmed
    ? `${move}，这一段已经走完`
    : `${move}，还在继续`;

  c.innerHTML=`
    ${row('这一段', going)}
    ${zs?row(now < zs.ZD ? '上方压力' : (now > zs.ZG ? '下方支撑' : '震荡区间'),
             zsWords({ZD:iP(zs.ZD), ZG:iP(zs.ZG)}, now)):''}
    ${rc?row('最近提醒', `<b>${CHAN_NAME[rc.kind]||rc.kind}</b> · ${rc.date.slice(5)} · ${iP(rc.price)}（${rc.age} 个交易日前）`):row('最近提醒','近两周没有出现新的买卖点')}
    ${b.n?row('过去准不准',
        `看涨信号出现 ${b.n} 次，3 天后平均${b.mean>=0?'涨':'跌'} ${Math.abs(b.mean)}%`
        +`（${Math.round(b.win*b.n)} 次上涨）`
        +(s.n?`<br>看跌信号出现 ${s.n} 次，3 天后平均${s.mean>=0?'涨':'跌'} ${Math.abs(s.mean)}%`:'')):''}
    <div class="cnote">这套方法把 K 线拆成一段段涨跌（也就是"笔"），
      再找"跌不动"和"涨不动"的位置（也就是"背驰"）。
      样本只有 ${b.n||0} 次，太少，<b>不参与涨跌概率</b>，只作参考。</div>`;
  return c;
}
/* 60 日收盘走势：主结论里唯一能一眼看出"现在在哪"的东西 */
function sparkline(sp, pl){
  const c = (sp && sp.close) || [];
  if(c.length < 10) return '';
  const W=900, H=64, pd=4;
  const mn=Math.min(...c), mx=Math.max(...c), rg=(mx-mn)||1;
  const x=i=>pd+i*(W-2*pd)/(c.length-1);
  const y=v=>H-pd-(v-mn)/rg*(H-2*pd);
  const d=c.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area=`${d} L${x(c.length-1).toFixed(1)},${H} L${x(0).toFixed(1)},${H} Z`;
  const chg=(c[c.length-1]/c[0]-1)*100;
  return `<div class="sparkwrap">
    <div class="spark-meta">
      <span>近 ${c.length} 个交易日</span>
      <span class="${cc(chg)}">${sg(chg)}%</span>
      <span class="faint">${nf(mn)} — ${nf(mx)}</span>
    </div>
    <svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <path class="area" d="${area}"/>
      <path class="line" d="${d}"/>
      <circle class="dot" cx="${x(c.length-1).toFixed(1)}" cy="${y(c[c.length-1]).toFixed(1)}" r="2.6"/>
    </svg>
  </div>`;
}

/* 方向词在界面上说人话："偏多/偏空/震荡"是行话，"看涨/看跌/横盘"一听就懂。
   后端仍用原值（契约、复盘、落库都按它），只在显示层映射。 */
const DIR={偏多:'看涨',偏空:'看跌',震荡:'横盘'};
const dirText=d=>DIR[d]||d||'—';

/* hero 顶部提示条：数据状况。
    两类事必须让用户看见，否则他会把"打折的结论"当成满血的：
      1. 模型自己说它降级了（隔夜外盘缺失 / 样本不足 / 区间覆盖率低）
      2. 数据已经陈旧（超过一个交易日没更新，或交易日历过期）
    设计上只做一行、不弹窗——它不是错误，是"这条结论的可信度附注"。 */
function dataNotes(pl, meta){
  const notes=[];
  const deg=(pl&&pl.degraded)||[];
  deg.forEach(d=>notes.push({t:'degraded', s:String(d)}));

  // 陈旧：预判是"下一个交易日"的，过了那天就该重算。
  // 用目标日与今天比，而不是拿 made_at 算小时数——周末本来就会放两天。
  const today=new Date(); today.setHours(0,0,0,0);
  if(pl && pl.target){
    const tgt=new Date(pl.target+'T00:00:00');
    if(!isNaN(tgt) && tgt < today){
      const days=Math.round((today-tgt)/86400000);
      notes.push({t:'stale', s:`这条预判是给 ${pl.target} 那天的，已经过去 ${days} 天了，点「刷新」重算`});
    }
  }
  // 日历过期：到了年底还没补明年的休市安排，target 会算错
  const cal=(meta&&meta.calendar)||{};
  if(cal.stale && cal.msg) notes.push({t:'cal', s:cal.msg});

  // 本次更新失败：页面上是上一次的完整快照，必须说出来。
  // 否则读者会把"旧的完整数据"当成"刚算出来的最新结果"。
  const be=(meta&&meta.build_error)||'';
  if(be){
    const first=String(be).split('\n')[0].slice(0,160);
    notes.push({t:'err', s:`本次更新没有成功，下面是上一次的完整快照 · ${first}`});
  }else if(meta&&meta.building_now){
    // 后台正在重算。setBusy 的那句"正在更新"写的是 #loading，
    // 而 #loading 在 #app 内、render 之后就没了——只靠它，用户看到的是
    // 一份旧数据加一片安静，完全不知道新数据正在路上。所以这里再挂一条。
    notes.push({t:'busy', s:'正在后台重新计算，完成后自动替换这份数据'});
  }
  // 一次性提示（例如"收盘后数据已定，没重算"），几秒后自动消失
  if(meta&&meta.notice) notes.push({t:'info', s:String(meta.notice)});

  if(!notes.length) return '';
  const cls=t=>t==='cal'?'cal':(t==='err'?'err':(t==='busy'?'busy':(t==='info'?'info':'')));
  return `<div class="dnote">${notes.map(n=>
    `<div class="dn ${cls(n.t)}"><i></i><span>${n.s}</span></div>`).join('')}</div>`;
}

function hero(pl,st,dp){
  const wd=['周日','周一','周二','周三','周四','周五','周六'][new Date(pl.target+'T00:00:00').getDay()];
  const dcls=pl.direction==='偏多'?'up':(pl.direction==='偏空'?'down':'');
  const lo=pl.exp_lo, hi=pl.exp_hi, span=Math.max(hi-lo,.3), X0=3, X1=97;
  const pos=v=>X0+(v-lo)/span*(X1-X0);
  const zero=Math.min(Math.max(pos(0),0),100);
  const bl=Math.min(Math.max(pos(lo),0),100);
  const bw=Math.max(Math.min(Math.max(pos(hi),0),100)-bl,1);
  const pt=Math.min(Math.max(pos(pl.exp_ret),0),100);

  const c=document.createElement('div'); c.className='card hero';
  c.innerHTML=`
    <div class="hero-top">
      <div class="hero-date"><b>${pl.target}</b> ${wd}<span class="sep">·</span>${pl.asof} ${pl.phase==='盘中'?'盘中':'收盘'}</div>
    </div>
    ${dataNotes(pl, window.__meta)}
    <div class="hero-grid">
      <div>
        <div class="hero-body">
          <div class="verdict ${dcls}">${dirText(pl.direction)}</div>
          <div class="prob"><span class="lab">上涨概率</span><span class="val">${pc(pl.prob_up)}</span></div>
        </div>
        <div class="hero-foot">
          <div class="hf">预计涨跌<b class="${cc(pl.exp_ret)}">${sg(pl.exp_ret)}%</b></div>
          <div class="hf">昨天收盘<b>${nf(pl.bench_close)}</b></div>
        </div>
      </div>
      <div>
        <div class="k" style="font-size:11.5px;margin-bottom:2px">90% 的概率落在</div>
        <div class="range">
          <div class="track"></div>
          <div class="band" style="left:${bl}%;width:${bw}%"></div>
          <div class="zero" style="left:${zero}%"></div>
          <div class="pt" style="left:${pt}%"></div>
        </div>
        <div class="range-l">
          <span style="left:0">${sg(pl.exp_lo)}%</span>
          <span style="left:${zero}%;transform:translateX(-50%)">0</span>
          <span style="right:0">${sg(pl.exp_hi)}%</span>
        </div>
        ${riskRows(pl,dp)}
      </div>
    </div>
    ${sparkline(window.__spark, pl)}`;
  return c;
}

/* hero 右栏：可预测的是风险，不是方向。
   横排三格而不是竖排三行——竖排每行 30px、共 90px，把 hero 撑到近一屏。 */
function riskRows(pl,dp){
  const d=(dp||window.__depth||{}), v=d.vol||{}, t=pl.tail||v.tail||{}, e=pl.execution||{};
  if(!v.forecast_vol_pct) return '';
  const cap = e.risk_cap!=null ? Math.round(e.risk_cap*100)+'%' : '—';
  return `<div class="hrisk">
    <div><span>波动大小</span><b>${nf(v.forecast_vol_pct,2)}%</b>
      <i>${v.regime||'—'}${v.percentile!=null?(v.percentile>0.66?'·偏高':(v.percentile<0.34?'·偏低':'·正常')):''}</i></div>
    <div><span>大跌风险</span><b class="down">${t.var95_bp!=null?nf(t.var95_bp/100,2)+'%':'—'}</b>
      <i>${t.var95_bp!=null?'5% 概率跌穿':'—'}</i></div>
    <div><span>最多投入</span><b>${cap}</b><i>其余留现金</i></div>
  </div>`;
}

/* 可执行性：这一行的信息量高于涨跌概率 */
function executionStrip(pl){
  const e=pl.execution||{};
  if(!e.headline) return document.createElement('div');
  const go=!!e.actionable;
  const d=document.createElement('div'); d.className='exec'+(go?' go':'');
  d.innerHTML=`<div class="vs">${e.headline}</div>
    <div class="bd">
      <span>${e.why||''}</span>
      ${e.execution_advice?`<span class="adv">${e.execution_advice}</span>`:''}
    </div>`;
  return d;
}

/* ---------------- 三分量 ---------------- */
/* 隔夜跳空 / 日内 / 全天 → 开盘 / 盘中 / 全天。后三个词不需要金融背景就能读。 */
function kpis(pl){
  const d=document.createElement('div'); d.className='kpis';
  const item=(t,v,cl,s)=>`<div class="kpi"><div class="t">${t}</div>
      <div class="v ${cl}">${v}</div><div class="s">${s}</div></div>`;
  d.innerHTML=
    item('开盘', sg(pl.e_gap)+'%', cc(pl.e_gap), `高开概率 ${pc(pl.p_gap_up)}`) +
    item('盘中', sg(pl.e_intra)+'%', intraCls(pl), intraNote(pl)) +
    item('全天', sg(pl.exp_ret)+'%', cc(pl.exp_ret), `上涨概率 ${pc(pl.prob_up)}`);
  return d;
}

function intraCls(pl){
  const ie=(window.__meta||{}).intra_edge||{};
  // 该层被判定无效时，数字降为灰色，避免读成"真实预测"
  return ie.active===false ? 'faint' : cc(pl.e_intra);
}

function intraNote(pl){
  const ie=(window.__meta||{}).intra_edge||{};
  if(ie.active===false) return '看不出方向';
  if(ie.active===true) return `上涨概率 ${pc(pl.p_intra_up)}`;
  return '还没验证';
}

/* ---------------- 自进化状态 ---------------- */
function evolutionPanel(S){
  const m=S.meta||{}, ie=m.intra_edge||{}, itv=m.interval||{}, cal=m.calib||{},
        pc2=m.prob_calib||{}, pl=S.prediction||{};
  const c=document.createElement('div'); c.className='card pad';
  const row=(k,v,cls)=>`<div class="er"><span>${k}</span><b class="${cls||''}">${v}</b></div>`;
  const gapB = cal.gap&&cal.gap.b!=null ? cal.gap.b : null;
  const gapS = cal.gap&&cal.gap.s!=null ? cal.gap.s : null;
  c.innerHTML=`
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 40px">
      <div>
        ${row('开盘模型 · 敏感度', gapB!=null? gapB.toFixed(3):'—')}
        ${row('开盘模型 · 误差', gapS!=null? gapS.toFixed(2)+'%':'—')}
        ${row('开盘样本', (cal.gap&&cal.gap.n? cal.gap.n:'—')+' 日')}
        ${row('概率收敛', pc2.lam!=null? pc2.lam.toFixed(2):'—')}
      </div>
      <div>
        ${row('区间幅度', itv.q!=null? '±'+itv.q.toFixed(2)+'%':'—')}
        ${row('区间命中率', itv.cov!=null? (itv.cov*100).toFixed(1)+'%（目标 90%）':'—')}
        ${row('盘中层优势', ie.edge!=null? (ie.edge>0?'+':'')+ie.edge.toFixed(4):'—',
              ie.active===false?'no':(ie.active===true?'ok':''))}
        ${row('盘中层可用吗', ie.active===true?'有效，参与计算':(ie.active===false?'没优势，只按常态':'样本太少'),
              ie.active===false?'no':(ie.active===true?'ok':''))}
      </div>
    </div>`;
  return c;
}

/* ================= 护盘共振 ================= */

/* 灯色不用红绿：本站红绿已经固定表示涨跌，再用它表示"吸筹/出货"会打架。
   改用语义（吸筹/出货/中性）+ 实心/空心圆点，颜色冲突消失。 */
function lgDot(state){
  if(state==='green') return '<i class="lg on"></i>';
  if(state==='red')   return '<i class="lg out"></i>';
  return '<i class="lg"></i>';
}
const LG_TXT={green:'吸筹',red:'出货',grey:'—'};

/* 尾盘 30 分钟：只有 32 个交易日，够看不够检验，单独一行、不进灯 */
function tailRow(t){
  if(!t) return '';
  return `<div class="k tailrow">
    尾盘 30 分钟 · ${t.date.slice(5)} <b class="${cc(t.pct)}">${sg(t.pct)}%</b>
    <span class="faint">近 ${t.n} 日均值 ${sg(t.mean)}% · 上涨 ${Math.round(t.win * 100)}%</span></div>`;
}

function rescuePanel(rp){
  const c=document.createElement('div'); c.className='card pad';
  if(!rp || rp.error){
    c.innerHTML=`<div class="k">护盘数据算不出来：${(rp||{}).error||'无数据'}</div>`;
    return c;
  }
  const vk=rp.verdict_key||'grey';
  const lights=(rp.lights||[]).map(x=>`
    <div class="litem ${x.state}">
      <div class="ltop"><i class="lg ${x.state==='green'?'on':(x.state==='red'?'out':'')}"></i>
        <b>${x.name}</b><span>${x.value}</span></div>
      <em>${x.note}</em>
    </div>`).join('');

  const rs=rp.rescue||{}, st=rs.stats||{};
  const conds=(rs.conds||[]).map(x=>`
    <span class="cond ${x.on?'on':'off'}">${x.name}<b>${x.detail}</b></span>`).join('');

  c.innerHTML=`
    <div class="lstrip">
      <div class="lsum ${vk}">
        <div class="lv">${rp.verdict}</div>
        <div class="ls"><span>资金面</span><span>在进 ${rp.green}</span><span>在出 ${rp.red}</span></div>
      </div>
      <div class="lrow">${lights}</div>
    </div>

    <div class="condrow">${conds}</div>
    ${tailRow(rp.tail30)}
    <div class="k" style="font-size:11.5px;margin-top:10px;line-height:1.8">
      五盏灯只是描述资金面，没有经过检验。救市三条历史上出现 ${st.n} 次，
      次日平均 ${sg(st.f1)}%，5 天内上涨概率 ${pc(st.win5)}。样本太少，不参与打分。
    </div>
    ${(rs.events||[]).length?`<div class="k" style="font-size:11px;margin-top:6px">
      历史上出现过：${rs.events.map(e=>`${e.date.slice(2)}`).join(' · ')}</div>`:''}`;
  return c;
}

/* 触发时在预判页顶一条，未触发不占位置 */
function rescueBadge(rp){
  const rs=(rp||{}).rescue||{};
  const d=document.createElement('div');
  if(!rs.triggered) return d;
  d.className='exec';
  d.innerHTML=`<div class="vs">国家队护盘</div>
    <div class="bd"><span>恐慌下跌、成交放大、大盘股护盘 同时出现</span>
      <span class="adv">历史上 ${rs.stats.n} 次，次日平均 ${sg(rs.stats.f1)}%</span></div>`;
  return d;
}

/* ================= 深度页 ================= */

/* 01 三段分解 */
const SEGNAME={隔夜:'开盘',隔夜跳空:'开盘',日内:'盘中',全天:'全天'};
const segName=x=>SEGNAME[x]||x;

function depthSegments(dp){
  const seg=dp.segments||{}, rows=(seg.segments||[]);
  const c=document.createElement('div'); c.className='card pad';
  const body=rows.map(x=>`<tr>
      <td><div class="nm">${segName(x.label)}</div><div class="sub">${x.window}</div></td>
      <td class="r ${cc(x.mean_bp)}">${nf(x.mean_bp,1)}</td>
      <td class="r ${cc(x.recent_bp)}">${nf(x.recent_bp,1)}</td>
      <td class="r">${pc(x.win)}</td>
      <td class="r">${nf(x.sharpe,2)}</td>
      <td class="r ${cc(x.mean_bp - 5)}">${sg(x.mean_bp - 5, 1)}</td>
    </tr>`).join('');
  c.innerHTML=`
    <div class="tbl-wrap"><table class="dtbl segtbl">
      <thead><tr><th>时段</th><th class="r">日均</th><th class="r">近一年</th>
        <th class="r">上涨天数</th><th class="r" title="夏普比率：每承担一份波动换来多少收益">性价比</th>
        <th class="r" title="扣掉 5bp 双边手续费后还剩多少">扣费后</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    ${segCurve(seg.series||{})}
    <div class="k" style="font-size:11px;margin-top:6px">表里单位是基点（bp），1bp = 0.01%，100bp = 1%</div>`;
  return c;
}

function segCurve(sr){
  const a=sr.overnight||[], b=sr.intraday||[];
  if(a.length<10) return '';
  const W=900,H=100,pd=4, all=a.concat(b);
  const mx=Math.max.apply(null, all.map(Math.abs).concat([1]));
  const path=arr=>arr.map((v,i)=>`${i?'L':'M'}${(pd+i*(W-2*pd)/(arr.length-1)).toFixed(1)},${(H/2-v/mx*(H/2-pd)).toFixed(1)}`).join(' ');
  return `<svg class="curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" stroke="var(--line)"/>
      <path d="${path(a)}" fill="none" stroke="var(--fg)" stroke-width="1.25" opacity=".9"/>
      <path d="${path(b)}" fill="none" stroke="var(--muted)" stroke-width="1.25"
            stroke-dasharray="4 3" opacity=".95"/>
    </svg>
    <div class="lgd"><span>—— 开盘</span><span>┄ 盘中</span><span class="faint">60日累计 %</span></div>`;
}

/* 02 条件日内分布 */
function depthIntraday(dp){
  const ci=dp.intraday||{}, rows=ci.rows||[], cur=ci.current||{};
  const c=document.createElement('div'); c.className='card pad';
  const body=rows.map(x=>{
    const mean = x.mean_bp==null ? '—'
      : (x.reliable ? nf(x.mean_bp,1) : `${nf(x.mean_bp,1)}±${nf(x.se_bp,1)}`);
    const tag = x.significant ? '靠得住' : (x.n==null ? '' : (x.reliable ? '' : '天数太少'));
    return `<tr class="${x.label===cur.label?'cur':''}">
      <td class="nm">${x.label}</td>
      <td class="r">${x.n==null?'—':x.n}</td>
      <td class="r ${cc(x.median_bp)}">${x.median_bp==null?'—':nf(x.median_bp,1)}</td>
      <td class="r ${cc(x.mean_bp)}">${mean}</td>
      <td class="r ${cc(x.net_bp)}">${x.net_bp==null?'—':sg(x.net_bp,1)}</td>
      <td class="r ${x.significant?'':'faint'}">${tag}</td>
    </tr>`;
  }).join('');
  c.innerHTML=`
    <div class="tbl-wrap"><table class="dtbl condtbl">
      <thead><tr><th>开盘情况</th><th class="r">天数</th><th class="r">中位</th>
        <th class="r" title="均值 ± 日常波动幅度">均值±波动</th><th class="r">扣费后</th><th class="r"></th></tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="k" style="font-size:11px;margin-top:8px">盘中平均 ${nf(ci.base_bp,1)}bp</div>`;
  return c;
}

/* 03 波动与尾部 */
function depthVol(dp){
  const v=dp.vol||{}, t=v.tail||{};
  const c=document.createElement('div'); c.className='card pad';
  /* 模型名换成说法，术语留在 title 里：
     "QLIKE"对外行等于乱码，但它确实是评估波动预测的标准损失函数。
     做法是标题说人话（谁更准）、术语挂 title，懂的人仍有据可查。 */
  const MODEL_WORD={
    'EWMA(0.94)':'加权近期', 'EWMA':'加权近期',
    '无条件方差':'长期平均', 'MA22':'近22日均值', '朴素(昨日)':'直接用昨天',
  };
  const q=(v.qlike||[]).map((x,i)=>`<tr>
      <td class="nm" title="${x.model}">${MODEL_WORD[x.model]||x.model}</td>
      <td class="r ${i===0?'ok':''}">${nf(x.qlike,4)}</td></tr>`).join('');
  const row=(k,val,cl)=>`<tr><td class="nm">${k}</td>
      <td class="r ${cl||''}">${val==null?'—':nf(val/100,2)+'%'}</td></tr>`;
  c.innerHTML=`<div class="duo">
    <div>
      <div class="k" style="margin-bottom:2px">预测明天的波动，哪种算法更准
        <span class="faint" style="font-weight:400">· 数字越小越准</span></div>
      <table class="dtbl"><thead><tr><th>算法</th><th class="r" title="QLIKE 损失，越小越好">误差</th></tr></thead>
        <tbody>${q}</tbody></table>
    </div>
    <div>
      <div class="k" style="margin-bottom:2px">明天可能跌/涨到哪 <span class="faint" style="font-weight:400">· 尾巴上的极端情况</span></div>
      <table class="dtbl"><tbody>
        ${row('5% 最差', t.var95_bp, 'down')}
        ${row('1% 最差', t.var99_bp, 'down')}
        ${row('5% 平均最差', t.cvar95_bp, 'down')}
        ${row('95% 最好', t.upside95_bp, 'up')}
        <tr><td class="nm" title="偏度看涨跌是否对称，峰度看极端行情多不多">形状</td><td class="r">${nf(t.skew,2)}/${nf(t.kurt,1)}</td></tr>
      </tbody></table>
    </div>
  </div>`;
  return c;
}

/* 04 状态条件基准率 */
function depthRegime(dp){
  const r=dp.regime||{}, rows=r.rows||[];
  const c=document.createElement('div'); c.className='card pad';
  const body=rows.map(x=>`<tr class="${x.label===r.current?'cur':''}">
      <td class="nm">${x.label}</td>
      <td class="r">${x.n||'—'}</td>
      <td class="r">${x.up==null?'—':pc(x.up)}</td>
      <td class="r" style="color:var(--fg)">${x.prior==null?'—':pc(x.prior)}</td>
      <td class="r ${cc(x.edge)}">${x.edge==null?'—':sg(x.edge*100,1)}</td>
    </tr>`).join('');
  const total=rows.reduce((a,x)=>a+(x.n||0),0);
  c.innerHTML=`<div class="tbl-wrap"><table class="dtbl regtbl">
      <thead><tr><th>波动</th><th class="r">天数</th><th class="r">实际</th>
        <th class="r">平时水平</th><th class="r">差多少</th></tr></thead>
      <tbody>${body}
        <tr class="faint"><td class="nm">全部</td><td class="r">${total}</td>
          <td class="r">${pc(r.base_rate)}</td><td class="r">—</td><td class="r">—</td></tr>
      </tbody></table></div>`;
  return c;
}

/* 05 截面因子 */
function depthXsection(dp){
  const xs=dp.xsection||{}, rows=xs.rows||[];
  const c=document.createElement('div'); c.className='card pad';
  const body=rows.map(x=>`<tr>
      <td><div class="nm" title="${x.factor}">${String(x.factor||'').replace(/\([^)]*\)/g,'').trim()}</div></td>
      <td class="r ${cc(x.ic)}">${sg(x.ic,4)}</td>
      <td class="r">${sg(x.t,2)}</td>
      <td class="r ${x.survives?'ok':'faint'}">${x.survives?'✓':'—'}</td>
      <td class="r ${cc(x.spread_bp)}">${sg(x.spread_bp,1)}</td>
      <td class="r ${cc(x.net_bp)}">${sg(x.net_bp,1)}</td>
      <td class="r">${sg(x.sharpe,2)}</td>
    </tr>`).join('');
  c.innerHTML=`<div class="tbl-wrap"><table class="dtbl xstbl">
      <thead><tr><th>因子</th>
        <th class="r" title="信息系数 IC：因子排序与实际收益排序的相关系数">排序准度</th>
        <th class="r" title="统计显著性，绝对值越大越不像巧合">可信度</th>
        <th class="r" title="t 绝对值超过 2.58 才算过统计门槛">过关</th>
        <th class="r" title="最好一组减最差一组的收益差">最好减最差</th>
        <th class="r">扣费后</th>
        <th class="r">性价比</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    <div class="k" style="font-size:11px;margin-top:8px">${xs.pit_warning||''}</div>`;
  return c;
}

/* 06 选择偏差门槛 */
function depthSelection(dp){
  const sb=dp.selection_bias||{};
  const c=document.createElement('div'); c.className='card pad';
  c.innerHTML=`<div class="duo">
      <div>
        <div class="k" style="font-size:11.5px">随便乱猜，最好也能到</div>
        <div style="font-size:30px;font-weight:620;letter-spacing:-1px;margin-top:4px">${nf(sb.threshold,2)}</div>
      </div>
      <div class="k" style="font-size:11.5px;line-height:1.8">
        ${sb.n_trials||'—'} 种试法 · ${sb.years||'—'} 年<br>低于这条线不算本事</div>
    </div>`;
  return c;
}

/* ---------------- 板块 ----------------
   三类并列：比大盘强 / 比大盘弱 / 跌久了。
   列头写"板块 今天 明天 概率"——四个词各对一列，不用"名称·今日"这种自造词。
   "相对强势"改成"比大盘强"：它排的就是横截面名次，大盘跌时第一名也在跌。 */
function sectorCol(list, label, tone){
  const head=`<div class="srow shead"><div></div><div class="n">板块</div>
      <div class="p">今天</div><div class="p">明天</div><div></div><div class="p">概率</div></div>`;
  const rows=(list||[]).map((x,i)=>`
      <div class="srow">
        <div class="i">${i+1}</div>
        <div class="n"><b>${x.name}</b>${x.pot?'<i class="cls pot">跌久</i>':''}</div>
        <div class="p ${cc(x.r1)}">${x.r1==null?'—':sg(x.r1)+'%'}</div>
        <div class="p ${cc(x.exp_ret)}">${sg(x.exp_ret)}%</div>
        <div class="b"><i style="width:${Math.max(x.prob*100,3)}%;background:var(--${tone})"></i></div>
        <div class="p">${pc(x.prob)}</div>
      </div>`).join('');
  const empty=`<div class="srow"><div class="i"></div><div class="n faint">今天没有</div></div>`;
  return `<div><div class="k gname">${label}</div>${head}${rows||empty}</div>`;
}

/* 60 日迷你走势：给"跌久"一个一眼可见的形状，不占额外一列 */
function miniSpark(arr){
  if(!arr||arr.length<10) return '';
  const W=180,H=22,pd=2;
  const mn=Math.min.apply(null,arr), mx=Math.max.apply(null,arr), rg=(mx-mn)||1;
  const pts=arr.map((v,i)=>`${(pd+i*(W-2*pd)/(arr.length-1)).toFixed(1)},`+
    `${(H-pd-(v-mn)/rg*(H-2*pd)).toFixed(1)}`).join(' ');
  return `<svg class="msp" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.1"
        vector-effect="non-scaling-stroke"/></svg>`;
}

/* 阶段是给人看的词：已启动 / 刚转强 / 横盘中。
   后端存的是"转强/筑底"，显示层换掉，别让用户去猜"筑底"是什么意思。 */
const STAGE={已启动:'已启动',转强:'刚转强',筑底:'还横着'};

function sectorPot(list){
  if(!list||!list.length)
    return `<div class="potwrap"><div class="pothead"><span class="k">跌久了的板块</span>
      <span class="faint">今天没有符合的</span></div></div>`;
  const head=`<div class="srow shead pot"><div></div><div class="n">板块</div>
      <div class="p">状态</div><div class="p" title="离最近一年的最高点还差多少">距最高</div><div class="p">一年位置</div>
      <div class="p">没创新低</div><div class="p">近5天</div><div class="p">潜力分</div></div>`;
  const SC={已启动:'up',转强:'',筑底:'faint'};
  const rows=list.map((x,i)=>`
      <div class="srow pot">
        <div class="i">${i+1}</div>
        <div class="n"><b>${x.name}</b><i>${x.r120!=null?'半年 '+sg(x.r120,0)+'%':''}</i>
          ${miniSpark(x.spark)}</div>
        <div class="p ${SC[x.stage]||''}">${STAGE[x.stage]||'—'}</div>
        <div class="p ${cc(x.dd250)}">${x.dd250==null?'—':sg(x.dd250,1)+'%'}</div>
        <div class="p">${x.pos250==null?'—':Math.round(x.pos250*100)+'%'}</div>
        <div class="p">${x.low_age==null?'—':(x.low_age<1?'今天':Math.round(x.low_age)+'天')}</div>
        <div class="p ${cc(x.r5)}">${sg(x.r5)}%</div>
        <div class="p">${x.pot_score==null?'—':nf(x.pot_score,0)}</div>
      </div>`).join('');
  return `<div class="potwrap">
      <div class="pothead"><span class="k">跌久了的板块</span>
        <span class="faint">跌得久、位置低 · 只是筛选，不是涨跌预测</span></div>
      ${head}${rows}</div>`;
}

function sectors(pl){
  const c=document.createElement('div'); c.className='card pad';
  c.innerHTML=`<div class="cols">
      ${sectorCol(pl.sectors_up,'比大盘强','up')}
      ${sectorCol(pl.sectors_down,'比大盘弱','down')}</div>
    ${sectorPot(pl.sectors_potential)}`;
  return c;
}

/* ---------------- 全板块预测表 ----------------
   一套数据、一个视图，用类别筛选切换，而不是复制三张表。
   默认「全部」按明日概率排；选「潜伏」时按潜伏分排——两种排序各自回答一个问题。 */
let SEC_FILTER='all';
function sectorTable(pl){
  const all=pl.sectors_all||[];
  const upSet=new Set((pl.sectors_up||[]).map(x=>x.code));
  const downSet=new Set((pl.sectors_down||[]).map(x=>x.code));
  const clsOf=x=>upSet.has(x.code)?'up':(downSet.has(x.code)?'down':(x.pot?'pot':''));
  const MARK={up:'强',down:'弱',pot:'跌久'};
  const cnt={all:all.length,up:0,down:0,pot:0};
  all.forEach(x=>{const k=clsOf(x); if(k) cnt[k]++;});

  const c=document.createElement('div'); c.className='card';
  const rowHTML=(filter)=>{
    let list=all.slice();
    if(filter!=='all') list=list.filter(x=>clsOf(x)===filter);
    if(filter==='pot') list.sort((a,b)=>(b.pot_score||0)-(a.pot_score||0));
    else list.sort((a,b)=>b.prob-a.prob);
    const html=list.map(x=>{
      const k=clsOf(x), e=x.etf;
      const etfTxt = e
        ? `<span title="${e.name} · 净${sg(e.net_yi,1)}亿" style="cursor:help">
             <span class="tag">${e.code}</span>
             <span class="${cc(e.pct)}" style="margin-left:5px">${sg(e.pct)}%</span></span>`
        : '<span class="faint">—</span>';
      return `<tr>
        <td><div class="co"><b>${x.name}${k?`<i class="cls ${k}">${MARK[k]}${k==='pot'&&x.pot_score!=null?' '+nf(x.pot_score,0):''}</i>`:''}</b>
          <span>${x.code}</span></div></td>
        <td class="r"><span style="color:var(--fg)">${pc(x.prob)}</span></td>
        <td class="r"><span class="${cc(x.exp_ret)}">${sg(x.exp_ret)}%</span></td>
        <td class="r"><span class="${cc(x.r1)}">${sg(x.r1)}%</span></td>
        <td class="r"><span class="${cc(x.r20)}">${x.r20==null?'—':sg(x.r20)+'%'}</span></td>
        <td class="r"><span class="${cc(x.dd250)}">${x.dd250==null?'—':sg(x.dd250,1)+'%'}</span></td>
        <td class="r"><span class="${cc(x.net_yi)}">${x.net_yi==null?'—':sg(x.net_yi,1)+'亿'}</span></td>
        <td><div class="reason" style="max-width:150px">${etfTxt}</div></td>
      </tr>`;}).join('');
    return html || `<tr><td colspan="8" class="faint" style="padding:18px 20px">这一类今天没有板块</td></tr>`;
  };

  c.innerHTML=`<div class="filters">
      ${[['all','全部'],['up','强于大盘'],['pot','跌久了'],['down','弱于大盘']].map(([k,t])=>
        `<button data-f="${k}" class="${SEC_FILTER===k?'on':''}">${t}<b>${cnt[k]}</b></button>`).join('')}
    </div>
    <div class="tbl-wrap"><table class="sectbl">
      <thead><tr><th>板块</th><th class="r">上涨概率</th><th class="r">预计涨跌</th>
        <th class="r">今天</th><th class="r">近20天</th><th class="r" title="离最近一年的最高点还差多少">距最高</th>
        <th class="r">资金流入</th><th>相关 ETF</th></tr></thead>
      <tbody>${rowHTML(SEC_FILTER)}</tbody></table></div>`;

  c.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{
    SEC_FILTER=b.dataset.f;
    c.querySelectorAll('[data-f]').forEach(x=>x.classList.toggle('on',x.dataset.f===SEC_FILTER));
    const tb=c.querySelector('tbody');
    if(tb) tb.innerHTML=rowHTML(SEC_FILTER);
  });
  return c;
}

/* ---------------- ETF 动向 ---------------- */
function etfPanel(pl){
  const d=pl.etf_panel||{inflow:[],outflow:[]};
  const c=document.createElement('div'); c.className='card pad';
  const col=(list,label,color)=>`
    <div>
      <div class="k" style="margin-bottom:4px">${label}</div>
      ${(list||[]).map(r=>`<div class="srow" style="grid-template-columns:1fr 62px 66px">
        <div class="n"><b>${r.name}</b><i>${r.code}</i></div>
        <div class="p ${cc(r.net_yi)}">${sg(r.net_yi,1)}亿</div>
        <div class="p ${cc(r.pct)}">${sg(r.pct)}%</div>
      </div>`).join('')||'<div class="faint" style="font-size:12px">暂无</div>'}
    </div>`;
  c.innerHTML=`<div class="cols">${col(d.inflow,'资金流入最多','up')}${col(d.outflow,'资金流出最多','down')}</div>`;
  return c;
}

/* ---------------- 个股 ----------------
   两列 9 行压成 5 行：表格形态（代码独占一行 + 六列）在主页要吃掉 360px，
   而这里要回答的只是"哪几只、多大概率、为什么"。完整代码进 title。 */
function stockTable(list, sc, themes){
  const c=document.createElement('div'); c.className='card pad';
  const items=(list||[]).map(s=>`
    <div class="si" title="${s.code} · ${(s.reasons||[]).join('；')}">
      <b>${s.name}</b>
      <span class="r"><em>${s.sector}</em>${(s.reasons||[])[0]||''}</span>
      <span class="p"><em>概率</em>${pc(s.prob)}</span>
      <span class="t ${cc(s.pct)}">${sg(s.pct)}%</span>
    </div>`).join('');
  c.innerHTML=`<div class="stk">${items}</div>${nameThemeNote(themes)}${stockCalibNote(sc)}`;
  return c;
}

/* 名称炒作提示。只给 lift 最高的两个，多了就是噪音。
   必须写清"不参与预测"：这类行情没有基本面锚，看到名字就买是情绪接力。 */
function nameThemeNote(themes){
  const t=(themes||[]).filter(x=>x&&x.label).slice(0,2);
  if(!t.length) return '';
  const txt=t.map(x=>`「${x.label}」${x.n}只`).join('、');
  return `<div class="scnote"><i class="w"></i><span>今日强势股里有<b>名称共性</b>：${txt}。
    这是按名字抱团，不是行业逻辑，<b>未纳入概率计算</b>。</span></div>`;
}

/* 个股概率的成色。三种状态必须分开说，因为它们的可信度差一个量级：
   未校准 = 纯公式原值；弱信号 = 校准过但承认分不开；可用 = 有实测支撑。 */
function stockCalibNote(sc){
  if(!sc || typeof sc!=='object') return '';
  const n=Number(sc.n_seen||sc.n||0), need=Number(sc.min_n||40);
  if(!sc.ready){
    return `<div class="scnote"><i class="w"></i><span>个股概率<b>还没校准</b>：可用样本 ${n}/${need}。
      现在显示的是打分公式原值，不含任何历史成绩。</span></div>`;
  }
  const hit=sc.hit_rate!=null?pc(sc.hit_rate):'—';
  const bs=sc.brier!=null?nf(sc.brier,4):'—';
  const bs0=sc.brier_raw!=null?nf(sc.brier_raw,4):'—';
  if(sc.weak){
    return `<div class="scnote"><i class="w"></i><span>个股概率<b>区分度不足</b>：
      校准后这几只几乎同分（${n} 个样本，实测命中 ${hit}）。
      说明这个打分模型目前对次日涨跌没有稳定预测力，名单只能当参考。</span></div>`;
  }
  return `<div class="scnote"><i class="g"></i><span>概率已按 ${n} 个实测样本校准：
    命中 ${hit}，误差 ${bs}（未校准时 ${bs0}，越小越准）。</span></div>`;
}

/* ---------------- 复盘 ---------------- */
function review(S){
  const le=S.last_eval;
  const c=document.createElement('div'); c.className='card pad';
  if(!le){ c.innerHTML='<div class="faint" style="font-size:12.5px">还没有可以复盘的预判</div>'; return c; }
  const box=(t,pred,act,ok,extra)=>`
    <div class="cmp">
      <div class="t"><span>${t}</span><span class="${ok?'ok':'no'}">${ok?'✓':'✗'}</span></div>
      <div class="v">${sg(pred)}% <s>→</s> <span class="${cc(act)}">${sg(act)}%</span></div>
      <div class="s">${extra||''}</div>
    </div>`;
  const dirOk = Math.sign(le.e_intra)===Math.sign(le.actual_intra);
  c.innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:10px;flex-wrap:wrap">
      <div class="k">${le.target} · 当时判断${dirText(le.direction)}${le.source==='replay'?'（历史回放）':'（实盘）'}</div>
      <div class="k" style="font-size:11.5px">预测偏差 ${nf(le.brier,3)}</div>
    </div>
    <div class="grid3">
      ${box('开盘',le.e_gap,le.actual_gap,le.gap_hit,`高开概率 ${pc(le.p_gap_up)}`)}
      ${box('盘中',le.e_intra,le.actual_intra,dirOk,`差 ${sg(le.actual_intra-le.e_intra)}%`)}
      ${box('全天',le.exp_ret,le.actual_pct,le.hit,`当时说上涨概率 ${pc(le.prob_up)}`)}
    </div>`;
  return c;
}

function metrics(st){
  const wrap=document.createElement('div');
  const d=document.createElement('div'); d.className='grid4';
  const it=(t,v,s)=>`<div class="stat"><div class="t">${t}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  d.innerHTML=
    it('开盘方向命中率',pc(st.gap_hit_rate),`${st.n||0} 天`) +
    it('全天涨跌命中率',pc(st.hit_rate),`基准 ${pc(st.base_rate)}`) +
    it('预测偏差',nf(st.brier,4),'0.25 相当于瞎猜') +
    it('板块排序准确度',nf(st.avg_ic,4),`方向正确 ${pc(st.ic_positive_rate)}`);
  wrap.appendChild(d);
  return wrap;
}

function weightCard(S){
  const c=document.createElement('div'); c.className='card pad'; c.style.marginTop='12px';
  const ws=(S.weights||[]).filter(w=>w.name!=='__all__');
  const mx=Math.max(...ws.map(w=>Math.abs(w.weight)),.1);
  const rows=ws.map(w=>{
    const p=Math.abs(w.weight)/mx*49, pos=w.weight>=0;
    return `<div class="wrow"><div class="n">${LABEL[w.name]||w.name}</div>
      <div class="wt"><div class="m"></div>
        <i style="${pos?`left:50%;width:${p}%`:`right:50%;width:${p}%`};background:${pos?'var(--fg)':'var(--dim)'}"></i></div>
      <div class="v">${sg(w.weight,3)}</div></div>`;
  }).join('');
  c.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
      <div class="k">各因素的分量（正=看涨，负=看跌）</div></div>
    <div class="wgrid">${rows}</div>
    ${weightSeries(S)}`;
  return c;
}

function weightSeries(S){
  const ws=S.weight_series||{};
  const keys=Object.keys(ws).filter(k=>ws[k].points.length>8);
  if(!keys.length) return '';
  keys.sort((a,b)=>Math.abs(ws[b].points[ws[b].points.length-1][1])-Math.abs(ws[a].points[ws[a].points.length-1][1]));
  const k=keys[0], pts=ws[k].points, vals=pts.map(p=>p[1]);
  const step=Math.max(1,Math.ceil(pts.length/160));
  const ds=vals.filter((_,i)=>i%step===0||i===vals.length-1);
  const mx=Math.max(...ds.map(Math.abs),.1), W=900,H=76,pd=5;
  const d=ds.map((v,i)=>`${i?'L':'M'}${pd+i*(W-2*pd)/(ds.length-1)},${H/2-v/mx*(H/2-pd)}`).join(' ');
  const lo=Math.min(...vals), hi=Math.max(...vals);
  return `<div style="margin-top:16px;border-top:1px solid var(--line-soft);padding-top:12px">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
      <span class="k" style="font-size:11px">${LABEL[k]||k} 分量变化</span>
      <span class="k" style="font-size:11px">当前 ${sg(vals[vals.length-1],3)} · 区间 ${sg(lo,2)} ~ ${sg(hi,2)}</span>
    </div>
    <svg class="svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:72px">
      <line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" class="wzero"/>
      <path d="${d}" fill="none" class="wline" stroke-width="1.3"/></svg>
    </div>`;
}

function logs(errs){
  const c=document.createElement('div'); c.className='card pad';
  if(!errs.length){ c.innerHTML='<div class="faint" style="font-size:12.5px">还没有纠错记录</div>'; return c; }
  /* 只显示最近 2 条。纠错记录是技术流水（"板块IC+0.64 · 权重 板块扩散-0.006"），
     一天一条、每条都长这样，铺满 4 条会把这一节的结论压到看不见。
     2 条足够说明"系统确实在按结果调权重"，剩下的用一行计数交代。 */
  const show=errs.slice(0,2);
  c.innerHTML=show.map(l=>`
    <div class="log">
      <div class="h"><span>${l.day} → ${(l.target||'').slice(5)}</span><span>·</span><span>${l.kind}</span></div>
      <div class="d" title="${(l.diagnosis||'').replace(/"/g,'')}">${l.diagnosis||''}</div>
      ${l.action?`<div class="a">↳ ${l.action}</div>`:''}
    </div>`).join('')
    + (errs.length>2
        ? `<div class="faint" style="font-size:11.5px;margin-top:9px">共 ${errs.length} 条纠错记录，这里只显示最近 2 条</div>`
        : '');
  return c;
}

const LABEL={trend:'涨跌趋势',meanrev:'涨多了会回',overseas:'隔夜外盘',breadth:'多少指数在涨',
  internals:'多少股票在涨',capital:'主力资金',volume:'成交量',volregime:'波动大小',
  sentiment:'新闻好坏',rotation:'板块轮动'};

$('#refresh').onclick=async e=>{
  const b=e.currentTarget; b.textContent='更新中';
  await refreshNow();
  setTimeout(()=>{b.textContent='刷新';},1500);
};
$('#rebuild').onclick=async e=>{
  if(!confirm('完整重算将重建 800 个交易日的历史回放（约 1-2 分钟），用于重新训练权重。继续？')) return;
  const b=e.currentTarget; b.textContent='重算中'; setBusy(true);
  _busy=true;
  await fetch('/api/refresh?full=true',{method:'POST'}); startPoll();
  setTimeout(()=>{b.textContent='重算';},1500);
};

(function(){
  const root=document.documentElement, btn=$('#theme');
  const meta=document.querySelector('meta[name="theme-color"]');
  const paint=()=>{
    const light = root.getAttribute('data-theme')==='light';
    btn.title = light ? '切换到暗色' : '切换到亮色';
    if(meta) meta.setAttribute('content', light ? '#fafafa' : '#09090b');
  };
  btn.onclick=()=>{
    const next = root.getAttribute('data-theme')==='light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try{ localStorage.setItem('theme', next); }catch(e){}
    paint();
  };
  paint();
})();

/* ---------------- 数据源监控 ----------------
   只显示中文名称与状态，不显示端点与参数：这是"数据还通不通"的仪表，
   不是接口文档。11 个源分属不同子域，限流与故障各自独立，所以分开报。 */
let _srcData=null, _srcAt=0;

function srcDot(ok){ return `<i class="sdot ${ok?'ok':'bad'}"></i>`; }

function paintSrc(){
  const btn=$('#src'), p=$('#srcpanel');
  if(!btn) return;
  if(!_srcData){ btn.textContent='数据源'; return; }
  const {ok,total}=_srcData;
  btn.innerHTML=`${srcDot(ok===total)}<span class="srclabel">数据源 </span>${ok}/${total}`;
  btn.classList.toggle('warn', ok<total);
  if(!p || p.classList.contains('hidden')) return;
  const groups=[];
  (_srcData.items||[]).forEach(x=>{
    let g=groups.find(v=>v[0]===x.group);
    if(!g){ g=[x.group,[]]; groups.push(g); }
    g[1].push(x);
  });
  p.innerHTML=`
    <div class="sp-head"><b>数据源</b><span>${_srcData.at||''} 检测</span>
      <button id="srcre">重新检测</button></div>
    ${(_srcData.fallback&&_srcData.fallback.kline)?'<div class="sp-warn">日线已在用备用源，成交额缺失</div>':''}
    ${(_srcData.fallback&&_srcData.fallback.stock_spot)?'<div class="sp-warn">全市场快照已切到新浪：量比、主力净流入、行业缺失</div>':''}
    ${groups.map(([g,list])=>`
      <div class="sp-g">${g}</div>
      ${list.map(x=>`<div class="sp-row">${srcDot(x.ok)}
        <span class="sp-n">${x.name}</span>
        <span class="sp-t ${x.ok?'':'bad'}">${x.ok?x.ms+' ms':'不通'}</span></div>`).join('')}`).join('')}`;
  const re=$('#srcre');
  if(re) re.onclick=e=>{ e.stopPropagation(); loadSrc(true); };
}

async function loadSrc(force){
  try{
    const r=await fetch('/api/sources'+(force?'?force=true':''));
    _srcData=await r.json(); _srcAt=Date.now(); paintSrc();
  }catch(e){ /* 面板打不开不影响主流程 */ }
}

(function initSrc(){
  const btn=$('#src'), p=$('#srcpanel');
  if(!btn||!p) return;
  btn.onclick=e=>{
    e.stopPropagation();
    const show=p.classList.contains('hidden');
    p.classList.toggle('hidden', !show);
    btn.setAttribute('aria-expanded', show?'true':'false');
    if(show){
      paintSrc();
      if(Date.now()-_srcAt>60000) loadSrc(false);
    }
  };
  p.onclick=e=>e.stopPropagation();
  document.addEventListener('click', ()=>{
    if(!p.classList.contains('hidden')){
      p.classList.add('hidden'); btn.setAttribute('aria-expanded','false');
    }
  });
  document.addEventListener('keydown', e=>{
    if(e.key==='Escape' && !p.classList.contains('hidden')){
      p.classList.add('hidden'); btn.setAttribute('aria-expanded','false');
    }
  });
  setTimeout(()=>loadSrc(false), 1200);   // 首屏渲染完再探，不抢行情请求
})();

/* ---------------- 自动刷新：恢复上次选择 ---------------- */
(function(){
  try{
    const v=parseInt(localStorage.getItem('autoMin')||'0',10);
    AUTO_MIN=AUTO_STEPS.includes(v)?v:0;
  }catch(e){ AUTO_MIN=0; }
  const btn=$('#auto');
  if(btn) btn.onclick=()=>cycleAuto();
  applyAuto();

  // 回到前台时若已超过一个周期，补一次；否则要等到下个整周期才有新数据
  document.addEventListener('visibilitychange', ()=>{
    if(document.hidden || AUTO_MIN<=0 || _busy) return;
    if(Date.now()-_lastLoad >= AUTO_MIN*60000) refreshNow();
  });
})();

load();
