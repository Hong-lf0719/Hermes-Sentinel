# scripts/ 预置脚本区

- 模型**可运行**（python/node 仅允许执行此目录脚本）
- 模型**不可写**（write_file 仅允许 data/ 与 outputs/）——堵死"写 evil.py 再执行"的提权链
- 需要分析时：先 `write_file` 把数据存到 data/，再 `python scripts/quick_stats.py data/xxx.csv [列]`

现有脚本：
- `quick_stats.py` — 读 data/ 下 CSV 输出行数/列名/数值列统计（JSON）
