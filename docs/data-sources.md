# 数据源手册

> 全部为公开 HTTP 端点，**无需注册、无需 Token**。
> 本文每一条结论都来自实测，环境：A 股，2026-11。
> 这些都是非官方接口，随时可能变更——所以文末专门写了「怎么验证一个源还活着」。

---

## 一、实测总表

| # | 用途 | 端点 | 状态 | 备注 |
|---|---|---|---|---|
| 1 | 全市场个股快照 | `push2.eastmoney.com/api/qt/clist/get` | ✅ 主源 | 5000+ 只，100 条/页 |
| 2 | 板块列表 | 同上，`fs=m:90+t:2`（行业）/ `t:3`（概念） | ⚠️ 会整段失效 | 见坑 3 |
| 3 | 板块成分股 | 同上，`fs=b:BK0448` | ✅ | 与 #2 不同端点参数 |
| 4 | ETF 行情 | 同上，`fs=b:MK0021` | ✅ | 含主力净流入 |
| 5 | 日线 OHLCV | `push2his.eastmoney.com/api/qt/stock/kline/get` | ✅ | 1300 根，含成交额 |
| 6 | 涨停/跌停家数 | `push2ex.eastmoney.com/getTopicZTPool` / `DTPool` | ✅ | 不同子域，独立限流 |
| 7 | 两融历史 | `datacenter-web.eastmoney.com/api/data/v1/get` | ✅ | 320 日，6 小时缓存够 |
| 8 | ETF 季度份额 | `fundf10.eastmoney.com/FundArchivesDatas.aspx` | ✅ | 返回 JS 片段，非 JSON |
| 9 | 7×24 快讯 | `np-listapi.eastmoney.com/comm/web/getFastNewsList` | ✅ | 50 条标题 |
| 10 | 隔夜美股龙头 | `push2.eastmoney.com/api/qt/ulist.np/get` | ✅ | `secids=105.NVDA,…` |
| 11 | 全市场快照（备用） | `vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData` | ✅ 可用 | 5574 只 / 3.6s |
| 12 | 板块列表（备用） | `vip.stock.finance.sina.com.cn/q/view/newSinaHy.php` | ✅ 列表可用 | **无板块日线** |
| 13 | 日线（备用） | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | ✅ 可用 | **无成交额** |
| 14 | 网易日线 | `quotes.money.163.com/service/chddata.html` | ❌ 502 | 多次重试均失败 |
| 15 | 同花顺板块 | `q.10jqka.com.cn` / `d.10jqka.com.cn` | ⚠️ 需 cookie | 不适合做自动降级 |

---

## 二、逐源手册

### 1. 东财 clist：全市场 / 板块 / ETF

```
GET https://push2.eastmoney.com/api/qt/clist/get
```

| 参数 | 说明 |
|---|---|
| `fs` | 筛选条件，决定拉什么（见下表） |
| `pn` / `pz` | 页码 / 每页条数。**`pz` 上限 100** |
| `fltt=2` | **必须**。不加会返回字符串型数值，后续计算全错 |
| `invt=2` | 反序标记，配合 `po=1` |
| `fid=f3` | 排序字段 |
| `fields` | 逗号分隔，只要需要的，能显著减小响应 |

`fs` 取值：

| 用途 | fs |
|---|---|
| 沪深京 A 股 | `m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23` |
| 行业板块 | `m:90+t:2` |
| 概念板块 | `m:90+t:3` |
| 某板块成分股 | `b:BK0448` |
| ETF | `b:MK0021` |

返回结构：`{"data":{"total":5574,"diff":[{...}]}}`

字段（`fields` 里按需取，前缀 `f` 是东财内部编号）：

| 编号 | 含义 | 单位 |
|---|---|---|
| f2 / f3 | 现价 / 涨跌幅 | 元 / % |
| f5 / f6 | 成交量 / 成交额 | 手 / **元** |
| f8 | 换手率 | % |
| f9 / f10 | 市盈率 / 量比 | 倍 / 倍 |
| f12 / f14 | 代码 / 名称 | — |
| f20 / f21 | 总市值 / 流通市值 | **元** |
| f26 | 上市日期 | `YYYYMMDD` |
| f62 / f184 | 主力净流入 / 净占比 | **元** / % |
| f100 | 所属行业 | — |

最小可运行示例：

```bash
curl -s 'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6&fields=f2,f3,f12,f14,f20'
```

### 2. 东财 kline：日线

```
GET https://push2his.eastmoney.com/api/qt/stock/kline/get
    ?secid=1.000001&klt=101&fqt=1&lmt=1300&end=20500101
    &fields1=f1,f2,f3,f4,f5,f6
    &fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
```

- `secid`：`市场.代码`。`1` = 沪市，`0` = 深市。板块用 `90.BK0448`，美股用 `105.NVDA`
- `klt`：101 日线 / 102 周线 / 5 五分钟
- `fqt`：0 不复权 / 1 前复权 / 2 后复权
- `lmt`：根数上限，1300 够 5 年
- **`end`：必填**，一般填 `20500101`（等于"到最新"）

⚠️ **不带 `end` 会返回 `{"rc":102,"data":null}`**——状态码 200、HTTP 完全正常，
只是没有数据。这是踩过的第一个"伪成功"，字段看着都对，就是空。
调试时如果 `data` 是 null，先检查这个。

返回 `data.klines` 是**字符串数组**，每行逗号分隔：
`日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率`

成交量单位是**手**，成交额是**元**。

⚠️ 五分钟线一次约 1500 根 ≈ 32 个交易日，**不支持 end 参数翻页**，想取更早只能换周期。

### 3. 东财 push2ex：涨跌停

```
GET https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=5&sort=fbt:asc&date=20261113
```

`ut` 是固定常量，不是密钥。返回 `data.tc` 为当日涨停家数。

### 4. 东财 datacenter：两融

```
GET https://datacenter-web.eastmoney.com/api/data/v1/get
    ?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize=320&sortColumns=DIM_DATE&sortTypes=-1
```

返回 `result.data` 数组，字段：`RZYE`（融资余额）、`RZYEZB`（占流通市值 %）、`RZJME`（净买入）、`RQYE`（融券余额）。

**这是与 push2 完全不同的子域**，限流独立——push2 被限时它通常仍可用。

### 5. 东财 fundf10：ETF 季度份额

```
GET https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code=510300
```

⚠️ 返回的是 JS 片段：`var gmbd_apidata={content:"<table>…"}`，**不是 JSON**。
JSON 解析器会直接报错，只能取 `text` 再用正则抠 `<td>`。

### 6. 东财快讯

```
GET https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_news_col&column=344&order=1&needInteractData=0&pageindex=1&pagesize=50
```

失败可退到 `getNewsByColumns`。只取标题做情感打分，够用。

### 7. 东财 ulist：外盘

```
GET https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=105.NVDA,105.AAPL&fields=f2,f3,f12,f14
```

美股市场号是 `105`。

### 8. 新浪：全市场快照（备用）

```
GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData
    ?page=1&num=100&sort=symbol&asc=1&node=hs_a
```

**请求头必须带 `Referer: https://finance.sina.com.cn`**，否则大概率被拒。

字段：

| 字段 | 含义 | 单位 |
|---|---|---|
| `code` / `name` | 代码 / 名称 | — |
| `trade` | 现价 | 元 |
| `changepercent` | 涨跌幅 | % |
| `volume` / `amount` | 成交量 / 成交额 | 股 / **元** |
| `mktcap` / `nmc` | 总市值 / 流通市值 | **万元** ⚠️ |
| `turnoverratio` | 换手率 | % |
| `per` / `pb` | 市盈率 / 市净率 | 倍 |

三个必须处理的差异：

1. **市值单位是万元**（东财是元）。不乘 10000，市值因子会小四个数量级。
2. **`num` 上限 100**，5500 只需翻约 56 页。
3. 没有量比、主力净流入、上市日期、行业。

### 9. 新浪：板块列表与成分

```
# 行业板块（返回 JS 对象，非 JSON）
GET https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php

# 概念板块
GET https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class

# 某板块成分股
GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData
    ?page=1&num=100&sort=symbol&asc=1&node=new_jrhy
```

板块列表返回形如 `var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,…"}`，
仍是 JS 赋值语句，要正则截取 `{...}` 再解析。

⚠️ **新浪板块代码（`new_xxx`）与东财（`BKxxxx`）是两套体系，不能混用**。

### 10. 新浪：K 线

```
GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
    ?symbol=sh000001&scale=240&ma=no&datalen=1023
```

- `scale=240` 是日线
- 支持个股（`sh600000`）与指数（`sh000001`）
- **不支持板块代码**：`new_blhy` / `gn_hwqc` / `bk_881101` 一律返回 `null`

这一条决定了板块没法做备用——见「三、降级策略」。

### 11. 腾讯：日线（备用）

```
GET https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,1300,qfq
```

`param` 格式：`代码,周期,起,止,数量,复权`。

返回 `data.<代码>.day` 为数组：`[日期, 开, 收, 高, 低, 成交量]`。

⚠️ **不含成交额**。成交量类因子会因此不可用——宁可少一个信号，也不要拿成交量乘一个假均价去编成交额。

> 注意区分：`qt.gtimg.cn` 是腾讯的**实时**行情，`web.ifzq.gtimg.cn` 才是历史 K 线。

### 12. 网易：日线（不可用）

```
GET https://quotes.money.163.com/service/chddata.html?code=0000001&start=20260101&end=20261114&fields=TCLOSE;HIGH;LOW;TOPEN;VOTURNOVER;VATURNOVER
```

实测持续 **502 Bad Gateway**，多次重试、换参数均失败。历史上这个接口以稳定著称，
如果你要用，先自己验证一遍。

### 13. 同花顺：板块（需 cookie）

`q.10jqka.com.cn` 与 `d.10jqka.com.cn` 都需要 `hexin-v` cookie，该值由页面 JS 生成。
不适合做自动降级通道。

---

## 三、降级策略

备用源要**按端点设计，不是按数据商设计**。实测过一次：

> `push2.eastmoney.com/api/qt/clist/get` 的 `m:` 类查询整段返回空，
> 而同一时刻 `b:` 类（成分股、ETF）、`push2his`（日线）、`datacenter`（两融）、
> `push2ex`（涨停池）全部正常。

所以给整条链路配一个"备用数据商"是无效设计——真正需要备份的只有会一起挂的那几个端点。

| 能力 | 主源 | 备用 | 覆盖情况 |
|---|---|---|---|
| 全市场快照 | 东财 clist | 新浪 | 有，缺量比/主力净流入/行业 |
| 日线 | 东财 kline | 腾讯 | 有，缺成交额 |
| 板块列表 | 东财 clist `m:90` | 无 | 新浪有列表但无板块日线 |
| 板块日线 | 东财 kline `90.BKxxxx` | 无 | 同上 |
| ETF | 东财 clist `b:MK0021` | 无 | 新浪有列表但无主力净流入，而排序依赖它 |
| 两融 / 涨停 / 新闻 / 外盘 | 各自子域 | 不需要 | 子域独立，通常不同时挂 |

**板块为什么不硬做备份**：拉成分股等权合成一条"板块日线"技术上可行，但那是另一条指数，
与官方板块指数口径不同，历史序列会断在切换那天。为了"今天能跑"污染历史，不划算。

---

## 四、坑清单

按踩到的顺序排列，前三条是最容易骗过自己的。

### 1. HTTP 200 + 空 body 的"伪成功"

东财被限流时**不返回 4xx**，而是 `{"data":null}`。如果代码只检查状态码，
会把"没拿到"当成"今天没有数据"——两个含义完全相反。

同类的还有：kline 少一个 `end` 参数，返回 `{"rc":102,"data":null}`；
板块列表在限流时返回空 `diff`。**全部是 200 + 正常结构 + 空内容**。

判据要用**"应该非空"**：全市场正常 5000+，拿到 0 或 100 就是失败，不是"今天股票少"。

### 2. 单位不一致

| 数据 | 东财 | 新浪 |
|---|---|---|
| 市值 | 元 | **万元** |
| 成交量 | 手 | **股** |
| 成交额 | 元 | 元 |

不换算不会报错，只会静默算错。稳健标准化后排序可能还对，但所有绝对数值全错——
这是最难发现的一类 bug。

### 3. `m:` 类与 `b:` 类的可用性不同步

东财 `clist` 的 `m:0+t:6`（全市场）、`m:90+t:2`（板块）会在同一时间一起失效，
而 `b:BKxxxx`（成分股）、`b:MK0021`（ETF）仍然正常。

所以"东财挂了"这个判断过粗，要精确到端点。

### 4. NaN 混进 JSON

部分接口会返回 `NaN`（非标准 JSON 字面量），浏览器 `JSON.parse` 直接抛
`Unexpected token 'N'`，整页数据作废。序列化前要把 NaN/Inf 转成 `null`。

### 5. 反复探测会加剧限流

同一个端点连续请求十几次，可能从"部分可用"变成"整段不可用"。
验证时每次至少间隔几秒，别把排查动作本身变成故障源。

### 6. 板块代码不通用

东财 `BK0448`、新浪 `new_blhy`、同花顺 `881101` 是三套独立编码。
跨源混用会静默返回空——不会报错。

### 7. 成交量 vs 成交额

日线备用源（腾讯）只有成交量、没有成交额。用 `成交量 × 收盘价` 估算成交额看着合理，
但复权价与原价差得远，估出来的量能因子会系统性偏移。缺就说缺。

---

## 五、怎么验证一个源还活着

不要相信文档，也不要相信"别人说能用"。按这个顺序做：

1. **打最小请求**：只取 3 条、只留必要字段，先确认通不通。
2. **看 body 而不是状态码**：200 + `{"data":null}` 等于失败。
3. **断言"应该非空"**：全市场少于 1000 只、板块少于 20 个，一律判失败。
4. **核对单位**：拿一个你知道市值的股票（比如工商银行 ≈ 2 万亿）验证量级，
   差 10000 倍就是万元/元的坑。
5. **对两个源做交叉验证**：同一天同一只股票的成交量，两个源差十倍，
   基本可以断定是复权处理不一致，不是源坏了。
6. **注意限流节奏**：每次请求间隔 2 秒以上，别连续打。

本仓库的 `core/datahub.py` 里，`_get_list()` 就是第 3 条的落地：
空 `diff` 会重试，因为"空"在行情接口里通常意味着限流而不是真的没有数据。

---

## 六、合规与频率

- 这些是**网页前端调用的接口**，不是官方 API。没有 SLA，没有文档，随时改。
- 个人研究、低频拉取通常没问题；**不要高频轮询、不要并发过高**（本项目全市场快照
  并发 6 页，单次构建总请求约 70 次）。
- 不要用于商业分发或二次售卖数据。
- 数据仅供研究，不构成投资建议。
