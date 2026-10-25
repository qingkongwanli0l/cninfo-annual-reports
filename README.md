# CNINFO Annual Reports

从[巨潮资讯网](https://www.cninfo.com.cn/)收集沪、深、北 A 股完整中文年度报告 PDF。支持按会计年度发现历史披露，或用 CSV 指定股票和年度，并提供版本归档、断点恢复和附件校验。

## 功能

- 按报告所属会计年度查询，覆盖延迟披露及以后发布的修订全文。
- 从历史公告发现公司，支持后来退市公司的历史 A 股披露及已核实的转让代码。
- 默认保留全部完整版本，包括原版、修订版、更新版及标记为已取消的全文；也可仅选择最新未取消版本。
- 串行低频访问，保存分页进度，遇到限流或验证要求停止。
- 以 SQLite 记录附件状态，校验 PDF 结构、页数和 SHA-256，重跑跳过已校验文件。

摘要、英文版、半年报、单独审计报告和更正说明不作为完整中文年报下载。仅凭标题无法判断的附件列入待核实清单。

## 安装

需要 Python 3.11 或更高版本，以及可访问巨潮查询域名和附件域名的网络。项目使用 Requests、pypdf 和 Python 标准库 SQLite。

Windows PowerShell：

```powershell
git clone https://github.com/qingkongwanli0l/cninfo-annual-reports.git
Set-Location cninfo-annual-reports
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux / macOS：

```bash
git clone https://github.com/qingkongwanli0l/cninfo-annual-reports.git
cd cninfo-annual-reports
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

下文命令以 PowerShell 为例。在 Linux / macOS 中，将 `.\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`。请在仓库根目录运行。

## 快速开始

先预览，再决定下载范围。**省略 `--limit` 和 `--pilot` 时，默认下载所选范围的全部附件。**

```powershell
# 发现并预览 2021 会计年度的历史年报
.\.venv\Scripts\python.exe -m cninfo_reports --year 2021 --dry-run

# 本次成功新增最多 100 份；重跑继续下一批
.\.venv\Scripts\python.exe -m cninfo_reports --year 2021 --limit 100

# 下载全部匹配版本，重跑跳过已校验文件
.\.venv\Scripts\python.exe -m cninfo_reports --year 2021

# 每家公司选择最新未取消全文
.\.venv\Scripts\python.exe -m cninfo_reports --year 2021 --versions latest
```

`2021` 指报告所属会计年度，通常在 2022 年披露。此后披露的 2021 年修订全文仍属于 2021 年。年度入口接受 2001 年起已经结束的会计年度，披露日期窗口以本轮查询的北京时间日期为上限。

首次发现一个年度时，需要查询、分页及补查元数据，可能耗时较长。`--dry-run` 会保存发现进度和导出结果，但不请求 PDF。仓库不附带预先生成的年度清单或数据库。

## 按股票 CSV 下载

CSV 使用 UTF-8 编码，必须包含 `stock_code`、`report_year` 两列。股票代码按六位字符串读取，保留前导零；重复的公司年度行只处理一次。

```csv
stock_code,report_year
000001,2023
600519,2024
```

仓库提供[示例清单](examples/targets.csv)：

```powershell
.\.venv\Scripts\python.exe -m cninfo_reports --input examples/targets.csv --dry-run
.\.venv\Scripts\python.exe -m cninfo_reports --input examples/targets.csv --limit 10
```

也可用 `--input` 指定自己的 CSV 路径，相对路径以启动目录为基准。年度入口直接使用发现的精确附件记录；CSV 入口按指定公司查询公告。年度发现导出的两列 `targets_YEAR.csv` 不包含附件 ID 和身份依据，不能代替完整附件清单。

## 命令行参数

| 参数 | 含义 |
| --- | --- |
| `--year YEAR` | 发现该会计年度的历史 A 股年报，与 `--input` 二选一。 |
| `--input PATH` | 读取指定股票和年度的 CSV。 |
| `--versions all` | 默认值，选择全部匹配的完整版本，包括已取消全文。 |
| `--versions latest` | 选择每家公司年度最新的未取消完整版本。 |
| `--dry-run` | 发现、选择并导出，不请求 PDF。 |
| `--limit N` | 限制本次成功新增下载数量，必须为正整数；省略时不设上限。 |
| `--pilot` | 样本下载，跨年份、跨重跑累计最多请求 10 个不同附件。 |
| `--refresh` | 重新扫描年度历史元数据，仅用于 `--year`。 |
| `--help` | 查看帮助。 |

`--limit` 只限制本次成功新增数量。已校验文件不计数，下载失败不占成功数量。

`--pilot` 的额度保存在状态库中，发起附件请求前即占用名额；失败也占额，重试同一附件不重复占额，已校验文件不占新额度。`--pilot --limit 3` 同时受本次成功 3 份和累计 10 个不同附件约束。达到额度后，其余附件保持待处理。请保留状态库以维持恢复和额度记录。

## 年度发现与版本规则

年度入口从历史公告发现公司，不将当前上市名单作为历史公司白名单。先按年度报告类别查询，再补查更正类别及其他披露时段；只有摘要而缺少全文时，按同公司及摘要披露日期不限类别补查。查询结果密集时按日期和市场拆分，并核对父窗口与子窗口的公告去重数量。

公司身份根据股票代码、`orgId`、公告市场和官方股票映射核对。退市转让代码仅在具有历史 A 股身份依据时使用；同一 `orgId` 不足以把 B 股或普通新三板证券认定为 A 股。历史代码冲突或标题不明时，保留待核实原因，已确认的其他附件仍可处理。

版本处理规则：

- 同一公告或相同附件 URL 的重复查询结果合并。
- 不同公告 ID、不同附件 URL 即使内容相同，也分别归档，并用 SHA-256 标记相同内容。
- `latest` 排除标题标记已取消的全文，按公告日期选择；同日优先明确的修订全文，仍不能确定唯一版本时列为待核实。
- 切换到 `latest` 只改变本次选择，已保存的其他版本继续保留。
- `withdrawn=no` 表示标题未见“已取消”标记，不保证该文件是现行版本。

“全量”指保存的截止日和查询范围内，巨潮可发现且通过识别的全部目标附件。分页完成不能证明全站绝对无遗漏；分类错误、历史搜索不可见或异常标题可能需要额外核实。请同时检查待核实项和查询日志。

## 清单复用、刷新与恢复

完整年度清单默认复用，未完成的查询自动续页。需要重新扫描时：

```powershell
# 重扫历史范围并预览
.\.venv\Scripts\python.exe -m cninfo_reports --year 2021 --refresh --dry-run

# 独立准备清单，不下载 PDF
.\.venv\Scripts\python.exe -m cninfo_reports.prepare --year 2021
```

刷新会扫描历史范围，以发现后补或重新分类的旧日期公告。已保存的公告、核实依据和下载状态保留；本次不再返回的历史版本不会自动移除。刷新中断后，重跑同一命令继续该轮分页。

若拆分窗口的父查询总数与子窗口去重数量不一致，清单标记为 `inconsistent`，停止年度下载。使用 `--refresh --dry-run` 重建查询进度。普通网络中断直接重跑续页即可。分页未完成时，不把部分结果作为完整年度清单使用。

下载中断后，重跑原命令。已完成文件按大小和 SHA-256 检查后跳过；缺失或损坏文件重新处理。同一次下载的重试中，有可靠版本标识时使用 `Range` / `If-Range` 续传；退出后的未完成分片会清理，下次从头请求该附件。

## 访问节奏与文件校验

查询、分页、下载和重试共用一个串行 HTTP 客户端。每次请求结束后间隔 5—7 秒；每成功新增 100 份、且仍有附件需要下载时，额外暂停 60 秒。连接超时 10 秒，读取超时 60 秒，瞬时故障最多尝试 3 次并逐步退避。

429 响应遵守 `Retry-After`；同批再次出现 429、收到 403 或验证页面时停止。本批三份附件失败也会停止并保存进度。运行锁阻止同一项目同时启动多个下载进程。请保持默认访问节奏，避免频繁刷新或并行运行多份副本。

PDF 先流式写入 `.pdf.part`，核对响应状态、长度、文件头、结束标识、可解析性和非零页数，通过后原子归档。每份附件完成后立即提交 SQLite；结果 CSV 在选择完成、每新增 100 份及退出时覆盖导出。

## 文件与输出

运行数据统一保存在仓库根目录的 `data/`，首次运行自动创建。该目录包含恢复所需状态，应按需备份。

```text
cninfo-annual-reports/
├── cninfo_reports/        程序源码
├── tests/                 离线测试
├── examples/targets.csv   CSV 示例
├── README.md
├── requirements.txt
├── LICENSE
├── LICENSE.cninfo-mcp
├── THIRD_PARTY_NOTICES.md
└── .gitignore
```

| 运行时位置 | 内容 |
| --- | --- |
| `data/pdf/代码/年度/` | 正式 PDF，文件名为 `代码_报告年_公告日期_公告ID.pdf`。 |
| `data/inventory_YEAR.sqlite3` | 年度公告、分页进度、补查及核实依据。 |
| `data/companies_YEAR.csv` | 公司、代表代码、历史代码、版本数量和最新日期。 |
| `data/reports_YEAR.csv` | 全部已确认全文候选、公告 ID、来源 URL、取消标记和年度依据。 |
| `data/review_YEAR.csv` | 未解决的标题、身份或摘要缺全文项；无待核实项时不保留空文件。 |
| `input/targets_YEAR.csv` | 年度发现导出的两列公司年度目标。 |
| `data/state.sqlite3` | 附件下载状态、文件路径、页数、大小、哈希及样本额度。 |
| `data/results.csv` | 本次选择的附件及下载结果，包含相同内容提示。 |
| `data/summary.csv` | 本次目标的完成、待处理、失败数量及原因。 |
| `data/stocks.json` | 单一股票身份缓存，有效期 7 天，刷新覆盖。 |
| `data/run.log` | 运行日志，单文件 5 MiB，保留 3 个轮转备份。 |
| `data/run.lock` | 进程锁文件，退出后释放锁。 |

`results.csv` 和 `summary.csv` 随本次目标覆盖导出；历史附件记录保存在 SQLite 中。年度入口使用 `latest` 时，`reports_YEAR.csv` 仍保存全部已确认候选。日志记录清单截止日 `through` 和完成状态。

启动、失败或退出时会清理受管理的下载分片、缓存和导出临时文件。正式 PDF、输入及数据库持续保留。磁盘需求取决于公司数量、版本数和文件大小，可先用 `--limit` 下载少量样本估算；初次发现和显式刷新也需要计入运行时间。

### 状态

| 状态 | 含义 |
| --- | --- |
| `complete` | 所选附件已通过文件校验。 |
| `pending` | 尚待下载，包括达到数量或样本额度后留下的附件。 |
| `missing` | 指定目标完整查询后未识别到合格全文，不证明报告不存在。 |
| `needs_review` | 公司身份、标题或最新版本无法确定，原因保留在清单中。 |
| `query_failed` / `not_queried` | 查询失败或尚未执行，不能作为缺失结论。 |
| `download_failed` | 请求或文件校验失败，错误原因保留。 |

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 下载目标完成，或预览正常完成；预览成功不表示 PDF 已下载。 |
| `1` | 查询、下载、身份或待核实问题，或运行中断。 |
| `2` | 仍有待处理附件或 `missing` 目标；命令参数错误也使用标准退出码 2。 |

## 常见问题

**为什么同一公司同一年有多个文件？**

默认 `all` 保存各次完整披露版本。只需要最新未取消全文时使用 `--versions latest`。同日版本不能唯一判断时会列为待核实。

**为什么预览没有立即结束？**

首次年度发现需要完整分页和补查，且每次请求有等待间隔。日志持续保存进度，中断后可重跑继续。

**提示身份或标题待核实，应该看哪里？**

年度发现查看 `review_YEAR.csv`，版本选择及目标状态查看 `summary.csv`。确认公司、年度和全文类型后再处理，不能仅凭标题最短、搜索排名或披露年份推断。

**收到 403、429 或验证码怎么办？**

保留数据库和已完成文件，按日志及服务器要求等待。程序不提供绕过访问限制的功能。

## 测试与维护

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试使用模拟 HTTP 响应和临时文件，覆盖版本筛选、历史身份、分页、刷新、下载额度、限流和 PDF 完整性，不依赖已有年度数据库或真实 PDF。

提交问题时请提供命令参数、Python 版本、退出码及必要日志片段，并移除本机路径、凭据和私人数据。修改查询或筛选规则时，请附能够复现问题的最小离线样本和对应测试。

## 许可与来源

项目代码采用 [MIT 许可证](LICENSE)。部分查询和下载逻辑改编自 `youhaozhao/cninfo-mcp`，来源、固定提交及上游许可见[第三方声明](THIRD_PARTY_NOTICES.md)和[上游 MIT 许可证](LICENSE.cninfo-mcp)。

下载的公告内容不属于本项目的软件许可范围，相关权利归原权利人所有。
