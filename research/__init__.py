"""研究报告包：把"为什么这么建模"变成可复现的脚本。

三个脚本共用同一份数据口径，但关注的层面不同：

    factors.py   信号检验。历次结论：隔夜外盘是唯一稳健信号，其余多为噪声。
    evolve.py    自进化对比。跳空模型已到天花板；日内层无边际；区间应更窄。
    deep.py      深度审计。三段分解、可交易性、状态依赖、截面因子、选择偏差。

口径约定（踩过坑，务必遵守）
    · 收益一律用 **百分数**（`core.depth.prepare` 的产出），换算 bp 时乘 100 只乘一次。
      曾经的 bug：`pred.actual_intra` 存的是百分数却按小数乘了 10000，得出 +388bp。
    · 盘中未收盘的那根 K 线必须剔除，否则等于把半天的收益当成全天。
    · 需要"下一日收益"时用 `shift(-1)` 后 `dropna`，注意这会剔掉最后一天；
      而按当日字段筛会剔掉第一天。两者混用会让不同脚本差 0.08bp。
    · 优先 `from core.depth import prepare`，不要自己再写一遍 gap/intra 的定义。

运行（在项目根目录）
    python -m research.factors
    python -m research.evolve
    python -m research.deep
    python run.py research deep      # 等价写法
"""
