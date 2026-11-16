# 给 AI 的调用说明（agent_crawl 封装层）

> 这份文档是写给 **AI 助手 / 脚本** 看的：只讲"怎么调、返回什么、失败了怎么办、不许干什么"。
> 人看的版本在 `使用说明.md`，两边不会冲突；本文所有字段、默认值、上限都对着
> `agent_crawl.py` 源码核过一遍，不是凭印象写的。

---

## 0. 先记住三条

1. **一律加 `--json`。** 加了这个参数，stdout 只吐一段固定结构 JSON，人看的进度全部改道到 stderr。
   不加 `--json` 也能跑，但 stdout 里混着汉字报告，你得自己从文字里抠数字——容易抠错。
2. **判结果只看 JSON 里的 `ok` / `status` / `reason_kind`**，不要靠退出码单独判断（下面 §9 有对照表）。
3. **不要为了"看一眼"去跑 `login`。** 那条命令会真的弹出一个浏览器窗口等人扫码，
   是有副作用的动作。查登录状态用 `run`（预检会告诉你）或 `status`。

命令统一这样写（在项目根目录执行，相对路径别改）：

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py <子命令> [参数]
```

六个子命令：`run` / `batch` / `login` / `status` / `defaults` / `export`。
另有四个（长任务与查版本）：`estimate`（只估算不真跑）/ `cancel`（喊停）/ `run --detach`（后台跑）/ `version`（查装的是哪一版）——见 §1.5 与 §1.6。

---

## 1. 六条命令速查

| 子命令 | 干什么 | 有副作用吗 | 支持 `--json` |
|---|---|---|---|
| `run` | 跑一个采集任务（核心入口） | 有：启动浏览器、写 `tasks\`、改 `state.json` | 是 |
| `batch` | 按 JSON 文件顺序跑一串任务 | 有：同上，且整队共用一把锁 | 是 |
| `resume` | 断点续跑：接着旧任务按关键词补抓 + 按 ID 去重合并 | 有：启动浏览器、写**新** `tasks\` 目录；旧目录不动 | 是 |
| `status` | 看环境 / 锁 / 进度 / 历史任务 | 无（只读磁盘和进程表） | 是 |
| `export` | 把已采任务导成交付用 Excel | 只多写一个 xlsx 文件，不动原始数据 | 是 |
| `login` | 触发扫码登录 | 有：弹浏览器窗口，要人扫码 | 是（加 `--json` 时**不弹窗**，只回报状态） |
| `defaults` | 查看 / 修改默认参数 | 有：改 `state.json` | 是 |

## 1.6 `version` —— 查装的是哪一版

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py version --json
```

无副作用。返回 `version` / `note`（这版加了什么）/ `python` / `home` / `mcp_server_exists`。
用户问"我装的是最新版吗"、或者你怀疑行为与文档不符时，先调它，别靠文件日期猜。

同平台的 `status --json` 里也带 `version` 字段。

## 1.5 三个长任务命令（`estimate` / `cancel` / `run --detach`）

**什么时候才用**：任务要跑几分钟以上（几百条、或带大量评论）。几十条的小任务直接前台 `run` 更简单。

### `estimate` —— 只估算，不真跑

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py estimate -p <平台> -k "<词>" -n <条数> --comments --max-comments <N> --json
```

- 参数和 `run` **完全一样**（同一个 `add_run_flags`，只少 `--detach`），但**不启动浏览器、不发网络请求**，只读 `state.json` 历史 + 超时公式。
- 有副作用吗：**无**。可以随便调、反复调。
- 返回字段：

| 字段 | 含义 |
|---|---|
| `ok` / `status` | `status` 恒为 `预估完成`；参数不合格时是 `参数校验未通过`（此时只有 `ok`/`status`/`reason`） |
| `eta_seconds` / `eta_text` | 预计耗时（秒 / 人话）。例：`约 3分11秒` |
| `confidence` / `confidence_label` | `高` / `中` / `低`；`confidence_label` 是给用户看的（`中把握`） |
| `basis` | **依据是什么**，必须原样转述：有历史写「按本机 N 次同平台实测（平均每条 X 秒）反推」，没有写「本机没有该平台的实测记录，只能按保守经验粗估，不保证」 |
| `sec_per_item` | 计算用的每条内容平均秒数 |
| `history_expect` | 按历史达成率：你要 Y 条，实际大概率只给 X 条。非空时要转告用户 |
| `cautions` | 字符串数组，**每一条都必须转给用户**（没登录 / 要量超上限 / 现在有锁 / 估算超过超时上限 / 评论是大头 / 允许重试会拖长） |
| `timeout_min` | 这次真跑会用到的超时上限（分钟） |
| `note` | 固定免责声明：预估仅供参考，不保证 |

- **不许把 `eta_seconds` 说成"一定就是这个时间"**，必须带上 `confidence_label` 和 `basis`。

### `run --detach` —— 后台跑

在原 `run` 命令后加 `--detach`（其他参数不变，`--json` 照旧要加）：

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p <平台> -k "<词>" -n <条数> --name <任务名> --json --detach
```

- 立刻返回，不再阻塞。后台进程有自己的隐藏控制台 + 独立进程组，**关掉调用方终端也不影响它跑完**。
- 返回里 `status` 三种可能：
  - `后台已启动` → 正常，接着用 `status --task` 查进度
  - `已完成（任务短，后台很快就跑完了）` → 任务太快，已经跑完了，返回里带 `contents` 和 `result_status`
  - `启动即退出` → 没跑起来（`ok=false` 或 `reason` 有日志路径），别再等，去读 `log`
- 其他字段：`pid`（后台进程号）、`locked`（是否已拿到运行锁）、`log`（屏幕输出文件）、`how_to_check` / `how_to_cancel`（两条命令，可直接给用户）
- `batch --detach` 同样支持（整条队列甩后台），但返回文案不同：队列本身不进 `history`，所以判定是「进程不在了 = 队列跑完了」，`status` 会写「已完成（整条队列跑完了）」，且 `how_to_check` 是 `status`（**不要带 `--task`**）。

### `cancel` —— 喊停

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py cancel --wait <秒> --json
```

- 机制：**写纸条不硬杀**。`agent_crawler\cancel.request` 写一张含 `pid` 的纸条，干活进程每 2 秒查一次，收到就走优雅收尾：发停止信号 → 关浏览器 → 写报告 → 摘运行锁。**已落盘数据全部保留**。
- `--wait`：递完纸条最多等几秒确认是否真停（默认 30，`0`=递完立刻返回）。
- 返回 `status`：
  - `已停止` → 确认停了
  - `已喊停，正在收尾` → 纸条生效但还在关浏览器，稍后用 `status` 再看
  - `无任务在跑` → 本来就没任务；**若牌子残留（进程已死），会顺手清掉牌子并返回 `ok=true`、`lock_cleaned=true`**
- 喊停后的任务在历史里：`status` = `已取消`（一条没拿到）或 `部分完成`（已拿到数据），`reason` 写明「已取消（人为喊停…）」。
- **不要连发多次 `cancel`**，一次就够；纸条是同一个文件。

### 相关行为说明（三处容易踩）

1. **过期纸条自动作废**：每次 `run` 开跑前会检查 `cancel.request`，指向的进程已不存在就撕掉，所以上次被强杀留下的纸条不会误伤这次。
2. **`status --json` 多了 `cancel_pending`**：`true` = 纸条已递出但任务还没收到（正常会在 2 秒内收到）。
3. **喊停不等于失败**：`reason_kind` 归到 `other`，`advice` 是「开跑前就被拦下或你手动停的；按上面的提示处理，不用查网络和登录」。**不许把它当网络故障去重试。**

---

## 2. `run` —— 跑一个任务

### 2.1 参数全表（源码 `add_run_flags`，一个不多一个不少）

| 参数 | 含义 | 默认 | 取值范围 / 备注 |
|---|---|---|---|
| `-p` / `--platform` | 平台 | **必填** | `bili` `zhihu` `wb` `dy` `tieba` `ks`，支持中文别名。**`xhs`（小红书）已不推荐**：反爬最狠、强验证码人工也常过不去，本项目不保证能跑通，见下方说明 |
| `-k` / `--keywords` | 关键词，英文逗号分隔 | **必填** | 最多 10 个；不能是空串 |
| `-n` / `--count` | **每个关键词**要多少条内容 | 记忆值（初始 20） | 1 ~ `count_cap`（初始 300） |
| `-f` / `--format` | 落盘格式 | 记忆值（初始 `jsonl`） | `json` `jsonl` `csv` `excel`（`xlsx`/`表格` = excel） |
| `-t` / `--type` | 任务类型 | 记忆值（初始 `search`） | `search` `detail` `creator`（`搜索`/`详情`/`创作者`/`博主`） |
| `--specified-id` | `detail` 类型必填：内容 ID | 空 | 逗号分隔，多个 |
| `--creator-id` | `creator` 类型必填：创作者 ID | 空 | 逗号分隔，多个 |
| `--name` | 任务名 | 第一个关键词 | 会做文件名清洗（非法字符换 `_`，截 40 字）；目录名 = `任务名_平台_日期`，同日同名第二条自动加 `_r2` |
| `--comments` / `--no-comments` | 抓不抓评论 | 记忆值（初始 **开**） | 两个开关互斥 |
| `--max-comments` | 每条内容抓多少条评论 | 记忆值（初始 10） | 1 ~ 20000 |
| `--media` / `--no-media` | 下不下媒体文件 | 记忆值（初始 **关**） | — |
| `--headless` / `--no-headless` | 无头模式 | 记忆值（初始 **可见窗口**） | 可见窗口反爬更稳，**默认别改** |
| `--auto-import` / `--no-auto-import` | 预检发现没登录时，是否自动导入 Edge 扩展导出的票再试一次 | 开 | 一般不用动 |
| `--retries` | 基础设施类错误自动重试次数 | 记忆值（初始 1） | 0 ~ 3 |
| `--wait-lock` | 别人在跑时最多排队等几分钟 | 0（直接退出，不抢） | 数字，单位分钟 |
| `--timeout-min` | 超时分钟数 | 自动（见 §2.3） | 显式给就尊重你的值（只受 1~180 保护） |
| `--passthrough` | 原样透传给 `main.py` 的额外参数 | 空 | **高级逃生口，AI 不要用** |
| `--start` | 从第几页开始抓（续跑用） | 不传 = 第 1 页 | **别自己瞎填**：原码语义是"把抓取窗口整体往后平移"，不是"少抓"。交给 `resume` 子命令算 |
| `--probe` / `--no-probe` | 开跑前先探一次票（用该平台的泛词取 1 条真实数据） | **自动** | 三态：都不写=自动（dy/ks/xhs + 总量≥100 条才开）；`--probe`=一定探；`--no-probe`=一定不探。探不通就直接判 `预检失败`，不往下跑 |
| `--detach` | 甩到后台跑：立刻返回任务信息，任务自己接着跑，关终端也不影响 | 关 | **只有 `run` 有这个参数**（`estimate` 没有）。几百条以上的长任务用它；几十条的小任务别用（同步跑更简单） |
| `--json` | stdout 只输出一段 JSON | 关 | AI 必须开 |

平台别名表：`xhs/小红书/红书/xiaohongshu`、`dy/抖音/douyin`、`ks/快手/kuaishou`、
`bili/B站/b站/bilibili/哔哩哔哩`、`wb/微博/weibo`、`tieba/贴吧/百度贴吧`、`zhihu/知乎`。

**平台取舍建议**（省你踩坑的时间）：

| 平台 | 建议 | 原因 |
|---|---|---|
| `bili` `zhihu` `wb` `tieba` | ✅ 首选，能稳定出数据 | 本机实测跑通过 |
| `dy` `ks` | ⚠ 可试，票好不好用只有真跑才知道 | 接口要签名，本机验不了票；快手票搬得进但平台不认 |
| `xhs` | ❌ **不推荐，不保证** | 反爬最狠，强验证码人工也常过不去；调用它时 `estimate` / `run` 会返回 `platform_note` 字段给出明确警告 |

代码里仍然保留 `xhs`（软弃用，不删）。什么时候真跑通了随时能再用，只是不承诺。

### 2.2 参数怎么被校验（`validate_params`）

不通过就**在开跑前**停下，状态记 `预检失败`，不会产生任务目录：

- 平台认不出 → 报错，列出支持的七个。
- 关键词超过 10 个 → 报错。
- `count` < 1 或 > `count_cap`（300）→ 报错。**注意 `count` 是"每个关键词"的条数**，
  给 3 个词 `-n 20` 就是要 60 条。
- `type=detail` 没给 `--specified-id`、或 `type=creator` 没给 `--creator-id` → 报错。
- `--max-comments` 超出 1~20000 → 报错。
- `--retries` 超出 0~3 → 报错。

**请求条数基数 `requested`**：`search` = `count × 关键词个数`；`detail` = ID 个数；`creator` = 创作者 ID 个数。
后面 JSON 里的 `contents` 就是拿来跟 `requested` 对账的。

### 2.3 超时怎么算的（自动值）

```
超时分钟 = 10 + ceil(count/10)                     # 基数
         + ceil(requested × max_comments / 60)      # 只有开了评论才加
然后钳到 [3, 180]
```

显式给 `--timeout-min` 就不套这个公式，只做 `≥1` 和 `≤180` 的保护。
（实测校准：B站 30 条 × 每条 10 评论 ≈ 199 条评论，评论阶段约 8 分钟。宁可给宽，不要假超时。）

### 2.4 跑一次要多久（真实数据，不是估的）

- 知乎实测：**1126 条评论，4 分 28 秒**（不含启动浏览器那 14 秒）。
- 按这个外推：1000 条评论 ≈ 4 分钟，5000 条 ≈ 20 分钟，10000 条 ≈ 40 分钟。
- 但"一万条评论"不是一条命令能给出来的：单次 `count` 上限 300，评论和内容都得靠**多条任务或多个关键词**叠出来。
- 原码 `config/base_config.py` 里还有 `CRAWLER_MAX_NOTES_COUNT = 24`、
  `CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 60` 这类上限，封装层**不动它们**。
  实测也不是严格按它截断（知乎 18 条内容拿到了 1126 条评论，平均 62 条/内容）。
  所以**别把 `-n` 和 `--max-comments` 当成精确承诺**，以最终落盘条数为准。
- 节奏是故意压慢的（翻页、请求之间都有等待）。**不要试图靠加大参数提速**，会触发平台风控。

### 2.5 示例

```bash
# 最小可用：知乎搜"美食"，20 条内容，每条 3 条评论
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p zhihu -k 美食 -n 20 --comments --max-comments 3 --json

# 多关键词（3 个词 × 15 条 = 45 条），任务起个名，导出成 Excel
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p bili -k "人工智能,量子计算,可控核聚变" -n 15 -f excel --name 前沿科技 --json

# 只要内容、不要评论；夜里跑长任务所以先探票
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p dy -k 新能源汽车 -n 100 --no-comments --probe --json

# 按内容 ID 抓详情（ID 从已有数据里来）
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p zhihu -k 占位 --type detail --specified-id 66f1a2b3c4d5e6f7a8b9c0d1 --json
```

> 注：`detail` 类型下 `-k` 仍需给一个非空值（占位即可），它是参数校验要求，不参与采集。

### 2.6 `run --json` 返回的字段（一个都不会少）

```json
{
  "ok": true,
  "status": "成功",
  "reason": "",
  "reason_kind": "",
  "reason_kind_label": "",
  "advice": "",
  "name": "美食",
  "platform": "zhihu",
  "type": "search",
  "keywords": ["美食"],
  "count_per_keyword": 20,
  "format": "jsonl",
  "requested": 20,
  "contents": 18,
  "comments": 1126,
  "attempt": 1,
  "retries": 0,
  "max_attempts": 2,
  "empty_result": false,
  "exit_code": 0,
  "qr_prompted": false,
  "duration_s": 268.4,
  "task_dir": "…\\tasks\\美食_zhihu_20261115",
  "report": "…\\tasks\\美食_zhihu_20261115\\report.md",
  "log_path": "…\\tasks\\美食_zhihu_20261115\\run_attempt_1.log",
  "primary_file": "…\\search_contents_2026-11-15.jsonl",
  "files": [
    {"path": "…", "name": "search_contents_2026-11-15.jsonl", "kind": "contents", "size": 123456, "records": 18}
  ],
  "spot_check": {"ok": true, "samples": ["样本1: 「…」 作者=xxx ✓"]}
}
```

逐字段读法：

| 字段 | 类型 | 怎么用 |
|---|---|---|
| `ok` | bool | **唯一结论字段**：`status` 是"成功"或"部分完成"就是 true |
| `status` | str | `成功`（条数达标）/`部分完成`（有数据但不足额）/`失败`（零产出）/`预检失败`（压根没开跑） |
| `reason` | str | 失败或部分完成时的原因原话；成功时是空串 |
| `reason_kind` | str | `not_logged_in`/`no_data`/`network`/`other`；成功时是空串（见 §8） |
| `reason_kind_label` | str | 上面那个的中文名，可直接转给人看 |
| `advice` | str | 处置建议原文，可直接转给人看 |
| `name` / `platform` / `type` / `keywords` / `count_per_keyword` / `format` | — | 回显本次实际生效的参数（`platform` 已归一成短名，`format` 已归一成 `excel` 等） |
| `requested` | int | 请求条数基数（`count × 词数`） |
| `contents` | int | 实际落盘的内容条数（多个格式并存时按格式分组取最大值，不重复计数） |
| `comments` | int | 实际落盘的评论条数；没开评论就是 0 |
| `attempt` | int | 第几次才跑出结果（1 = 一次过；0 = 预检就停了） |
| `retries` | int | 中间重试了几次 = `attempt - 1` |
| `max_attempts` | int | 最多允许跑几次 = `1 + retries` 参数 |
| `empty_result` | bool | `true` = 跑通了但平台确实返回 0 条（**不是故障**，别重跑） |
| `exit_code` | int | 爬虫子进程的退出码，排查用，不必据此下结论 |
| `qr_prompted` | bool | 跑的过程中弹过扫码窗口 = 票在中途过期了 |
| `duration_s` | float | 本任务总耗时（秒），含启动浏览器 |
| `task_dir` | str | 任务归档目录，`export` 和 `status --task` 都吃它 |
| `report` | str | 人读的 markdown 报告路径 |
| `log_path` | str | 这次尝试的完整子进程日志（查根因先看它尾部） |
| `primary_file` | str | 主成果文件（有 excel 就给 excel，没有就给主要数据文件） |
| `files[]` | array | 落盘文件清单：`path`/`name`/`kind`（contents/comments/creators/excel）/`size`，非 excel 还带 `records`，excel 带 `sheets` |
| `spot_check.ok` | bool / null | 抽查 3 条样本的完整性（标题或正文非空、作者字段有值——脱敏值也算有、链接以 http 开头）。一条数据都没有时是 `null` |
| `spot_check.samples[]` | array | 每行一句人话，`✓`/`✗ 缺哪项` |

> 探针（`--probe`）没过时，`status=预检失败`，`reason` 里会直接给证据日志路径（留在 `agent_crawler\_probe\` 下）；
> 探针成功后那份临时数据会被删掉，不会混进 `tasks\`，也不进历史。

**参数校验没过时**，返回的是精简版，只有三个字段：

```json
{"ok": false, "status": "预检失败", "reason": "参数校验未通过：数量 500 超过上限 300（可在 state.json 调整 count_cap）"}
```

所以解析时**先判 `ok`，再做 `.get("contents", 0)` 这种防御性读取**，别假设字段一定齐。

---

## 3. `batch` —— 一串任务排队跑

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py batch --file agent_crawler\批量任务示例.json --json
```

批量文件是**非空 JSON 数组**，每项的字段名跟 `run` 的参数同名（`platform`/`keywords`/`count`/`format`/…），
可以用 `name` 起名。文件建议 UTF-8（带不带 BOM 都行，读的时候兼容了）。

```json
[
  {"platform": "bili", "keywords": "人工智能", "count": 20, "name": "AI行业动态"},
  {"platform": "bili", "keywords": "量子计算,可控核聚变", "count": 15, "comments": false, "name": "前沿科技"},
  {"platform": "wb", "keywords": "新能源汽车", "count": 20, "format": "excel", "name": "新能源舆情"}
]
```

行为要点：

- **顺序执行，任务之间固定歇 10 秒**（不是并发，也不许改成并发）。
- 整条队列**共用一把运行锁**：开头抢一次，跑完才摘。中途不再重复查"有没有别人在跑"。
- 单项失败不影响后面的任务，会继续跑。
- 只在**队列开头**做一次深度预检；`--probe` 也只探一次，后面的任务沿用结论。

返回结构：

```json
{
  "ok": false,
  "status": "完成",
  "total": 3,
  "ok_count": 2,
  "failed_by_kind": {"no_data": 1},
  "tasks": [ { …同 run 的单任务结构，一条一个… } ]
}
```

- `ok` = 是否**每个**任务都有产出；
- `failed_by_kind` 只统计没产出的任务，按四个桶计数；
- 退出码：全部有产出 `0`，部分有产出 `2`，一个都没有 `1`。

批量文件读不了（不存在 / 不是数组 / 空数组）时返回
`{"ok": false, "status": "预检失败", "reason": "…", "total": 0, "ok_count": 0, "tasks": []}`。

---

## 4. `status` —— 看环境、看进度、看某个任务

只读，不加锁，**可以在别人跑任务时随便调**。

### 4.1 不带 `--task`：看整体

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py status --json
```

```json
{
  "ok": true,
  "now": "2026-11-15 18:02:11",
  "running_groups": [
    {"root_pid": 12345, "label": "zhihu", "members": [{"pid": 12345, "cmd": "…"}, {"pid": 12347, "cmd": "…"}]}
  ],
  "running_proc_count": 4,
  "leftover_browsers": [{"pid": 22334, "cmd": "…user-data-dir=…"}],
  "lock": {
    "held": true, "pid": 12345, "alive": true, "age_min": 3.4,
    "stale": false, "task": "美食",
    "describe": "PID 12345｜任务「美食」｜平台 zhihu｜已跑 3分25秒｜启动于 2026-11-15 17:58:46"
  },
  "progress": "进度估算：内容 16/60 条（27%），已跑 2分51秒，按当前速度约还需 5分46秒（线性估算，仅供参考）",
  "history": [
    {"ts": "2026-11-15 17:40:02", "name": "美食", "status": "成功",
     "contents": 18, "requested": 20, "attempt": 1, "retries": 0,
     "reason": "", "task_dir": "…", "reason_kind": "", "reason_kind_label": ""}
  ]
}
```

- **`running_proc_count` 不等于任务数**。一次 `run` 实际会看到 4 个 python 进程（venv 启动器 →
  真进程 → 时间壳 → 真进程），是一条启动链。**任务数看 `running_groups` 的长度**，进程数看 `running_proc_count`。
- `lock.held=false` 时 `lock` 里只有 `held` 一个字段，其余都没有（没人在跑）。
- `lock.stale=true` 表示牌子 30 分钟没刷新，属于失效残留，可以接手；`lock.alive=false` 表示挂牌进程已经死了。
- `progress` 是**线性外推**，仅供参考。数据还没起来时它不会空着，而是给你一句"样本太少，等跑过 1 分半再估"；
  只有拿不到任务目录、没留 `params.json`、或当前是批量队列时才返回空串。
- `history` 是最近 10 条，最新的在最前面。

### 4.2 带 `--task`：看单个任务的底细

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py status --task 美食 --json
```

`--task` 认三种写法，按这个顺序找：

1. 绝对路径或相对路径（这个目录存在就直接用它）；
2. `tasks\` 下的目录名：先试完全同名，再试"名字里含这段字"（唯一命中才用；命中多个就报出来让你自己选，不替你猜）；
3. `state.json` 历史里的 `task_dir`（同样是完全匹配或片段匹配）。

所以 `--task 美食`、`--task 美食_zhihu_20261115`、`--task "%MC_ROOT%\tasks\美食_zhihu_20261115"` 都能用。
找不到时返回 `{"ok": false, "reason": "找不到任务目录", "tried": [...它到底在哪找过...], "query": "…"}`。

```json
{
  "ok": true,
  "query": "美食",
  "task_dir": "…\\tasks\\美食_zhihu_20261115",
  "name": "美食",
  "platform": "zhihu",
  "type": "search",
  "keywords": ["美食"],
  "count_per_keyword": 20,
  "format": "jsonl",
  "want_comments": true,
  "requested": 20,
  "running": false,
  "progress": "",
  "attempt_count": 1,
  "retries_done": 0,
  "latest_attempt": 1,
  "contents": 18,
  "comments": 1126,
  "percent": 90,
  "finished": true,
  "status": "成功",
  "reason": "",
  "reason_kind": "",
  "params_from_report": "",
  "attempts": [
    {"attempt": 1, "dir": "…\\attempt_1", "contents": 18, "comments": 1126,
     "log_path": "…\\run_attempt_1.log", "last_write": "2026-11-15 17:44:30", "has_data": true}
  ],
  "report": "…\\report.md",
  "tried": ["…"]
}
```

要点：

- `attempts[]` 是**每次尝试各拿到多少**，一次重试一条。`has_data=false` 的那次一条没落盘，日志在 `log_path`。
- `contents`/`comments`/`percent` 取的是**编号最大的那次尝试**（重试过的话前面的都算废的）。
- `finished` = 有终态状态且此刻没在跑；`running` = 按锁上登记的任务名/目录路径对上了。
- `params_from_report`：老任务的 `params.json` 丢了时，参数是从 `report.md` 里读回来的，这里会有原话。
- 找不到任务时 `ok=false`、退出码 1；其余情况退出码 0。

---

## 5. `export` —— 把已采任务导成交付用 Excel

只多写一个 xlsx，**原始数据一个字不改**（不排序、不去重、不改正文、不加统计列）。

| 参数 | 含义 | 默认 |
|---|---|---|
| `--task` | **必填**，写法同 §4.2 的三种 | — |
| `--out` | 输出 xlsx 路径 | `agent_crawler\exports\<任务名>_<时间戳>.xlsx` |
| `--kinds` | 导哪几类，逗号分隔 | `contents,comments`（还有 `creators`） |
| `--attempt` | 指定第几次尝试 | 取编号最大、且真落了数据的那次 |
| `--no-unified` | 不加「统一格式」页 | 默认**会加** |
| `--json` | stdout 只输出 JSON | 关 |

sheet 结构：`口径说明` → `内容` / `评论` / `创作者`（按 `--kinds`）→ `统一格式` / `统一格式_评论`。

「统一格式」页是给下游程序用的规范副本，列名和顺序固定：
内容 9 列（内容ID/标题/正文/作者昵称/发布时间/点赞数/评论数/链接/关键词）、
评论 9 列（评论ID/所属内容ID/父评论ID/评论正文/评论者昵称/评论时间/点赞数/子评论数/IP属地）。
对不上的列**留空，不猜**（比如微博没标题列就空着，知乎没有 IP 属地就空着）。

导出后会把文件读回来再校验一遍（冻结首行、自动筛选、长数字确实是文本、sheet 名与行数对得上、
**统一页行数必须等于原始页行数**）。校验不过就返回 2，不会假报成功。

```json
{
  "ok": true,
  "status": "成功",
  "reason": "",
  "file": "…\\exports\\美食_20261115-180201.xlsx",
  "task_dir": "…\\tasks\\美食_zhihu_20261115",
  "attempt_dir": "…\\tasks\\美食_zhihu_20261115\\attempt_1",
  "sheets": [
    {"kind": "contents", "kind_cn": "内容", "sheet": "内容", "rows": 18, "cols": 9},
    {"kind": "comments", "kind_cn": "评论", "sheet": "评论", "rows": 1126, "cols": 9}
  ],
  "unified_sheets": [
    {"kind": "contents", "sheet": "统一格式", "rows": 18, "columns": ["内容ID", "标题", "…"],
     "mapping": [{"column": "内容ID", "from": "content_id"}], "unmapped": []}
  ],
  "skipped_kinds": ["creators"],
  "problems": []
}
```

- `problems[]` 里是回读校验没过的项；为空 = 全过。有内容时 `ok=false`、`status=部分完成`、退出码 2。
- `unmapped[]` 是统一页里没对上、留空的列名（不是错误，是平台确实没这个字段）。
- `skipped_kinds[]` 是这次任务里压根没采到的类（比如没开评论就没有 `comments`）。
- 导出失败（找不到任务、文件被 Excel 占用等）返回
  `{"ok": false, "status": "失败", "reason": "…"}`，退出码 1。

---

## 6. `login` / `defaults`

### `login`

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py login -p zhihu
```

- **AI 不要主动调它**：会弹出浏览器窗口等人扫码（内部按 600 秒轮询，也就是最多等约 10 分钟，扫完自动继续）。
- 已经登录的平台会直接告诉你"无需再扫码"然后退出。
- **只想问"还用不用登录"，用 `--check-only`**（配合 `--json` 最省事）：

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py login -p <平台> --check-only --json
```

  只读登录态、**不弹浏览器**。返回 `ok` / `platform` / `logged_in` / `status` / `message` / `need_user_action` / `platform_note`。
  `logged_in=false` 时 `need_user_action=true`，意思是"这一步必须有人到电脑前处理"，不是你能自己解决的。
- 加 `--json` 但**没加** `--check-only`：如果票还有效，行为同 `--check-only`（直接回报，不弹窗）；
  如果票失效了，程序会**拒绝执行**（`ok=false`、退出码 2）并提示"加 --json 时不弹浏览器"。
  这是故意的——弹窗等扫码的操作在 JSON 模式下没法进行，硬等只会超时。
- 输出里的账号昵称、uid、目录名都会被抹掉，只留"已登录/没登录"的结论。

正常补票路径不是 `login`，而是免扫码链路：桌面 Edge 扩展导出票 → `edge_login_sync.py --import-json all latest`。
`run` 的预检会自动尝试导入一次（`--auto-import` 默认开），所以多数情况你什么都不用做，直接重跑 `run` 就行。

### `defaults`

```bash
.venv\Scripts\python.exe agent_crawler\agent_crawl.py defaults                          # 看
.venv\Scripts\python.exe agent_crawl.py defaults --set count=30 format=excel             # 改
```

只有一条规则要记住：**任务成功后，本次的 `count`/`format`/`type`/`comments`/`max_comments`/`media`/`headless`
会自动写回 `state.json` 当新默认值**。也就是说同一台机器上，省略参数时用的不一定是出厂值。

所以 AI 想结果可复现，就**把关键参数每次都显式写全**，别依赖记忆值。

`defaults` 可改的键：`count` `format` `type` `comments` `max_comments` `media` `headless`
`retries` `count_cap` `timeout_base_min` `timeout_max_min`。改成别的键会报"未知默认项"。
AI 一般不需要改这套；要改 `count_cap`（单次数量上限）这种，先问人。

---

## 7. 机器可读的 `RESULT:` 行（没有 `--json` 时的备份方案）

不加 `--json` 时，报告最后一行是固定格式：

```
RESULT: name=美食 status=成功 contents=18/20 comments=1126 file=%MC_ROOT%\tasks\...\xxx.jsonl
```

⚠️ 加了 `--json` 之后，这行会被改道到 **stderr**（因为 stdout 要让给 JSON）。
所以：**要么读 stdout 的 JSON，要么读 stderr 里的 `RESULT:` 行，别两套混着用。**

---

## 8. 失败了怎么办：四个桶

`reason_kind` 只有四种，处理办法完全不同。JSON 里已经带了 `reason_kind_label` 和 `advice`，
直接把它们转给人就行；不要自己另编一套说法。

| `reason_kind` | 中文名 | 是什么意思 | `advice` 原文（照转即可） |
|---|---|---|---|
| `not_logged_in` | 没登录 | 没票、票过期、二维码没扫上、开跑前探针取不到数据 | 先补票再跑（login，或用免扫码插件 --import）；不补票重跑结果一样 |
| `no_data` | 平台没内容 | 跑通了，平台确实就没这些数据 | 不是故障，别重跑；换个关键词或放宽条数 |
| `network` | 网络或接口出问题 | 超时、断网、风控拦截、浏览器起不来、进程异常退出、认不出的错 | 等几分钟重试或换网络；先 status 看有没有残留浏览器占着。要是日志里写「风控/验证码/400002」，那是平台要人机验证：别开无头，让它开可见窗口，你先手动过一次验证再跑 |
| `other` | 不是爬取失败 | 参数校验没过、本机已有任务占着锁、批量文件读不了、你按了 Ctrl+C | 开跑前就被拦下或你手动停的；按上面的提示处理，不用查网络和登录 |

几个必须分清的情况：

- **`empty_result=true`**：`status` 会是"失败"，但 `reason_kind` 是 `no_data`。
  **这不是故障，重跑一百次也一样。** 正确动作是换关键词。
- **`status=部分完成`（`ok=true`）**：有数据，只是没到请求条数。数据可用，别当失败处理。
  要补量就加关键词或另开一个任务。
- **快手（`ks`）目前的已知状态**：票能搬进去，但平台自检接口不认（回 405），爬虫会转去点「登录」按钮然后超时；
  真跑还会撞上搜索接口 `400002` 要人机验证。这属于**平台风控**，不是插件的问题。
  这类失败会被归到 `not_logged_in`，原因原话里带「登录自检未通过」。**遇到就别反复重试**，如实告诉人。
- **抖音/小红书的登录态本机验不了**（接口要签名），预检只能按"有票就算过"。票过期往往跑起来才暴露，
  `qr_prompted=true` 就是这个信号。

---

## 9. 退出码对照

| 命令 | 0 | 2 | 1 | 其他 |
|---|---|---|---|---|
| `run` | 成功 | 部分完成（有数据但不足额） | 失败 / 预检失败 | 130 = Ctrl+C |
| `batch` | 每个任务都有产出 | 部分有产出 | 一个都没有 / 批量文件读不了 | — |
| `status` | 正常（含"没有任务在跑"） | — | `--task` 指定的任务找不到 | — |
| `export` | 校验全过 | 回读校验没过 | 找不到任务 / 导出失败 | — |
| `login` / `defaults` | 正常 | — | 失败 / 参数不认识 | — |

⚠️ 如果命令是通过某些后台通道（如 DSH background job）发起的，通道会把非零退出码统一归一化成 `1`，
这时**必须看 JSON 的 `status`，不能看退出码**。

---

## 10. AI 最容易犯的错（照这个清单自查）

1. **把 `-n` 当成"总共要多少条"**。它是**每个关键词**的条数。3 个词 `-n 20` = 60 条。
2. **想靠调大 `-n` 拿一万条**。单次上限 300，而且平台的翻页/限流会截断；到量靠多关键词 + 多任务。
3. **看到 `status=失败` 就重跑**。先看 `reason_kind`：`no_data` 重跑没用，`not_logged_in` 不补票重跑也没用。
4. **自己编失败原因**。原因原话在 `reason`，归类在 `reason_kind`，建议在 `advice`，照转。
5. **开 `--headless`**。默认可见窗口是故意的，无头更容易被风控。不要"为了快/为了没窗口"去开它。
6. **并发跑**。同一时间只允许一个任务（一把 `run.lock`）。想排队用 `--wait-lock`，**绝不要删锁去抢**。
7. **忘了 `--json` 之后 stdout 里没有中文报告**。进度在 stderr，别把 stderr 当错误。
8. **用 `--passthrough` 绕封装层**。那是给人用的逃生口，AI 用它会绕过参数校验和预检。
9. **重复调 `login`**。它真的会弹窗。补票优先走"重跑 `run`（预检会自动导入）"。
10. **假设 JSON 字段一定齐**。参数校验失败时只有 `ok`/`status`/`reason` 三个字段。

---

## 11. 不许干的事（硬边界）

这些是这套东西的设计前提，破坏任何一条都会伤到登录态或数据：

1. **不改 MediaCrawler 原码**。`agent_crawler\` 是独立封装层，`tasks\` 是独立归档目录。
   要改采集行为，只能通过封装层参数，不能去动 `main.py` / `config/` / `media_platform/`。
2. **不加并发、不批量猛爬**。不并行、不缩短等待、不加大 `MAX_CONCURRENCY_NUM`、不提高翻页速度。
   慢是故意的。
3. **不碰这些东西**：cookie / 登录态文件 / `browser_data\` / `state.json` 里的登录信息 /
   已采到的数据 / 导出的 Excel / `.bak` 备份 / 运行日志。
   要清理历史记录，先问人。
4. **不把登录信息、本机路径写进任何产出**：日志、报告、说明文档、状态样例里都不许出现
   cookie 值、token、账号、绝对路径。
5. **临时文件自己清**。诊断脚本、临时库、试跑产物，跑完删掉，别留在 `tasks\` 或交付目录里。
6. **不确定就说不确定**。别把推断写成实测（比如某个平台的列名没核对过，就说"没核对过"）。
7. **不代用户做决定**。要不要重跑、要不要换关键词、要不要动配置，把 `advice` 转给人，让人拍板。

---

## 12. 一页模板（可以直接照抄改参数）

```bash
# ① 先看环境：有没有人在跑、有没有残留浏览器
.venv\Scripts\python.exe agent_crawler\agent_crawl.py status --json

# ② 跑一个任务（关键参数全部显式写，不依赖记忆值）
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run ^
  -p zhihu -k 美食 -n 20 -f jsonl -t search ^
  --comments --max-comments 3 --no-media --no-headless --retries 1 ^
  --json

# ③ 想长跑就先探票（多花 20~40 秒，避免白跑几小时）
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p dy -k 新能源汽车 -n 100 --probe --json

# ④ 只看这次跑到哪儿了
.venv\Scripts\python.exe agent_crawler\agent_crawl.py status --task 美食 --json

# ⑤ 导成交付用 Excel（多看一页「统一格式」，下游好拼表）
.venv\Scripts\python.exe agent_crawler\agent_crawl.py export --task 美食 --kinds contents,comments --json
```

读结果的固定套路：

```
先看 ok
├─ true  → 读 contents / comments / files / primary_file，交付
└─ false → 读 status
           ├─ 预检失败 → 读 reason（参数或锁的问题，改完再跑）
           ├─ 失败     → 读 reason_kind + advice（照转给人）
           │            └─ empty_result=true 时是 no_data，别重跑
           └─ 部分完成 → 数据可用，ok 其实也是 true
```
