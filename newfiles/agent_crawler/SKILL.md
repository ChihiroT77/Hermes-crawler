---
name: Hermes
description: "Hermes-crawler：用本机 MediaCrawler 采集社交平台数据——B站/小红书/抖音/快手/微博/贴吧/知乎的关键词搜索、指定内容、作者主页，可选一级评论与媒体文件，自带预检、超时保护、结果对账与 Excel 导出。用户要爬取、采集、抓取这些平台的帖子、视频、评论、博主数据，或要把采到的数据导成 Excel 时使用。"
---

# Hermes-crawler 数据采集（MediaCrawler 封装层）

本机 `%MC_ROOT%` 已部署 MediaCrawler，并配有面向 AI 的封装脚本。
**一律通过封装脚本调用；绝不直接跑 MediaCrawler，绝不改它的源码和配置**（只允许改 `agent_crawler\` 下的封装文件）。

## 起手三步

1. **一律加 `--json`。** stdout 只吐一段固定结构 JSON，人看的进度全部改道到 stderr。
2. **判结果只看 JSON 里的 `ok` / `status` / `reason_kind`**，不要靠退出码（见「失败怎么判」）。
3. **别为了"看一眼"去跑 `login`** —— 它会真的弹浏览器窗口等人扫码。

完整字段说明、每个参数的范围、返回样例，都在 `agent_crawler\给AI的调用说明.md`（12 节，对着源码核过）。
本文只放最常用的和高风险的。

## 调用方式

工作目录必须是 `%MC_ROOT%`，用它的 venv 解释器：

```
%MC_ROOT%\.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p <平台> -k "<关键词,最多10个>" -n <条数> -t search --comments --max-comments <单条评论上限> -f jsonl --name <任务名> --json
```

- 平台 `-p`：bili / zhihu / wb / dy / tieba / ks（也认中文别名）。
  **`xhs`（小红书）已软弃用**：反爬最狠（强验证码人工也常过不去），不推荐、不保证能跑通。
  代码保留，真要试就跑，但别向用户承诺。调用它时 `estimate` / `run` 的返回值里
  `platform_note` 字段会给出这段警告。
- `-n` 是**每个关键词**的条数（上限 300）；多关键词写 `-k "a,b,c"` 一次跑完，比分次跑省浏览器启动和登录检查
- `-t`：**必须显式写**。不写会沿用上一次任务的类型（历史上跑过 detail 就会要求 `--specified-id` 并报参数错）。`-t search` 关键词搜索；`-t detail --specified-id <id>` 抓指定内容；`-t creator --creator-id <id>` 抓作者
- 不需要评论就去掉 `--comments`；要图片/视频文件加 `--media`
- 六个子命令：`run` / `batch`（JSON 队列批量）/ `login -p <平台>`（扫码）/ `status`（只读，可在别人跑时随便调）/ `defaults` / `export`（导 Excel）
- 另有四个新子命令：`estimate`（只估算不真跑）、`cancel`（喊停正在跑的）、`run --detach`（后台跑）、`resume`（断点续跑）——见下文「长任务：先估、再后台、能喊停」和「断点续跑」
- 完整参数表：`agent_crawler\agent_crawl.py --help`；人看的手册：`agent_crawler\使用说明.md`

## 长任务：先估、再后台、能喊停

这三个能力是为「跑几十分钟的长任务」准备的。**几十条以内的小任务不必用**，直接前台跑更省事。

**第一步：先估（不真跑）**

```
%MC_ROOT%\.venv\Scripts\python.exe agent_crawler\agent_crawl.py estimate -p <平台> -k "<词>" -n <条数> --comments --max-comments <N> --json
```

参数和 `run` 完全一样，但**不会启动浏览器、不发网络请求**。返回里看：
- `eta_text`（大概多久）、`confidence_label`（把握大小：高/中/低）
- `basis`：依据是什么 —— 有本机实测历史就写「按本机 N 次实测反推」，没有就明说「只能保守粗估，不保证」
- `history_expect`：按历史达成率，你要 100 条实际大概率只给多少条（非空时值得转告用户）
- `cautions`：必须原样转给用户的坑（没登录、要量超上限、现在有任务占着、估算超过超时上限）

**第二步：后台跑**

```
... agent_crawl.py run -p <平台> -k "<词>" -n <条数> --name <任务名> --json --detach
```

`--detach` 让封装立刻回一句「已启动（进程号/任务名/日志路径）」就撒手，任务脱离当前进程继续跑，**关掉终端也不影响**。返回里：
- `status`：`后台已启动` / `已完成（任务短，后台很快就跑完了）` / `启动即退出`
- `how_to_check` / `how_to_cancel`：照抄给用户的两条命令
- `log`：后台屏幕输出文件（`agent_crawler\logs\`，只在排查时看，别主动读给用户）

**第三步：查进度 / 喊停**

```
... agent_crawl.py status --task <任务名> --json      # 进度，同前台任务
... agent_crawl.py cancel --wait 30 --json            # 喊停
```

- `cancel` **不是硬杀**：写一张 `cancel.request` 纸条，干活进程每 2 秒自己看一眼，收到就按优雅流程收尾（停止爬取 → 关浏览器 → 写报告 → 摘牌），**已落盘的数据全部保留**，没跑完的部分不补。
- 喊停后的任务状态是 `已取消`（一条没拿到）或 `部分完成`（已拿到一些），`reason` 会写明是人为喊停。
- 牌子还在但进程已死（被强行关掉）时，`cancel` 会顺带把残留牌子清掉并返回 `status=无任务在跑`；下次开跑也会自动撕掉过期纸条，不会误伤新任务。
- `status` 的 JSON 多一个 `cancel_pending`（纸条是否已递出未生效）。

**探针什么时候自动开**

`--probe` 是「开跑前先用一个泛词真取 1 条数据，确认票还能用」，多花 20~40 秒冷启动。
从 1.10.0 起它变成三态，**默认不写就是自动判断**：

- 显式 `--probe` → 一定探；显式 `--no-probe` → 一定不探
- 都不写 → 只在「这台机器验不了票的平台（dy / ks / xhs）」且「这次共要 100 条以上」时自动开

为什么单独挑这三个平台：`edge_login_sync.py --check` 只能对 bili/wb/tieba/zhihu 真问一句「票还有效吗」，
dy/ks/xhs 的接口要签名，本机只能确认「cookie 在」，**票过期了也照样显示已登录**。长任务最怕这种情况：
预检查全绿、跑到一半才发现票早废了。自动开了会在屏幕和 `--json` 的 `probe_note` 里说明原因，
不想探加 `--no-probe` 即可。登录任务（`login`）本身不探。

## 断点续跑（resume）：接着上次的进度抓

跑到一半断了（网络、被喊停、电脑休眠、票过期后补完票），不用从头再来：

```
... agent_crawl.py resume --task <任务名片段> --json
```

`--task` 给 `tasks\` 下的目录名就行，只给片段也可以（唯一命中才用）。它会**只读磁盘、不联网**地先算一份账：

- 每个关键词上次抓了多少条、够不够目标数 → 够数的直接跳过，不白跑
- 从旧日志的 `page: N` 推算断点页，从**下一页**接着抓
- 关键限制：原码 `--start` 是**全局单值**，一次运行里所有关键词共用一个起始页。所以续跑只能
  **一个词一个词单独补**，每个词各用各的起始页——这也是为什么它看着比一次跑完慢
- `--start N` 的真实语义是「把抓取窗口整体往后平移」，**不是「只抓 N 页」**，总量不变；
  平台分页边界会抖，断点页附近必然重采到旧数据 → 所以合并时**按内容 ID 去重**（dy=aweme_id、
  bili/ks=video_id、wb/tieba/xhs=note_id、zhihu=content_id），重复的丢掉不算漏数据
- **只支持 jsonl / json / csv**。旧任务是 Excel 会直接拒绝并让你换格式——两份 xlsx 合并容易静默出错，
  宁可拒绝也不做不可靠的事
- ks / tieba / zhihu 的日志没有「关键词+页码」这种行，断点页只能按「已抓条数 ÷ 每页条数」保守推算，
  推算不出来的词会从第 1 页重抓，靠 ID 去重兜底；每条依据都写在返回的 `per_keyword[].page_basis` 里
- 只处理 `search`（按关键词翻页）类型。`detail` / `creator` 是按 ID 抓，没有「第几页」，会直接说明
  并让你重跑同样的命令（按 ID 抓本身就会覆盖写，不会真重复）

产出：一个**新目录**（`<原名>_续跑`），里面是合并好的 `resume_contents_日期.jsonl`（有评论再加
`resume_comments_日期.jsonl`）和 `resume_summary.json`（逐词账本 + 每次补抓的结果）。
**旧任务目录一律不动**，出问题随时能回退。

返回里重点看：`status`（续跑完成 / 部分完成 / 无事可做 / 已取消 / 失败）、`old_contents` /
`new_contents` / `merged_contents` / `duplicates_dropped`、`per_keyword`。
退出码照旧：0=完成或无事可做，2=部分完成或已取消，1=失败或预检不过。

常用开关：`--attempt N`（读第几次尝试，默认取最大的那次）、`-n <条数>`（覆盖旧任务的目标条数）、
`--from-scratch`（不管旧进度，所有词从第 1 页重抓，仍按 ID 去重）。

## MCP 工具层（给支持 MCP 的 AI 平台用）

不想让 AI 拼命令行，就接 `agent_crawler\mcp_server.py`。它只是命令行外面薄薄一层壳：工具调用 → 翻译成 `agent_crawl.py ... --json` → 把 JSON 原样交回。**不重复实现、不篡改判断逻辑**，所以上面所有结论对它同样成立。

```
command: %MC_ROOT%\.venv\Scripts\python.exe
args:    ["%MC_ROOT%\agent_crawler\mcp_server.py"]
```

十个工具：`crawl_start`（默认后台跑）/ `crawl_wait`（**启动后接着等，跑完一次性拿结果——AI 调 start 之后必须调它**）/ `crawl_status` / `crawl_cancel` / `crawl_estimate` / `crawl_export` / `crawl_batch`（批量队列，默认后台）/ `crawl_resume`（断点续跑，默认后台）/ `crawl_login_check`（只查登录态，不会替你登录）/ `crawl_defaults`（看 / 改默认参数）。参数 schema 自带中文说明书。
自测（不用接平台）：`.venv\Scripts\python.exe agent_crawler\mcp_server.py --selftest`。

**登录这件事 MCP 替你不了**：`crawl_login_check` 只能回答"现在票还能不能用"，真要补票必须有人在电脑前扫码或者用免扫码扩展。所以遇到 `not_logged_in`，正确做法是**请人到电脑前处理**，不要反复重试。

**同一件事别两套混用**：接了 MCP 就用工具，没接就用命令行，不要一半工具一半命令行。

## 判结果：读 JSON，不读 `RESULT:` 行

`--json` 下 stdout 只有 JSON，`RESULT: name=… status=… contents=…` 那行被改道到 **stderr**。
所以：**要么读 stdout 的 JSON，要么读 stderr 的 `RESULT:` 行，别两套混着用。**

```
先看 ok
├─ true  → 读 contents / comments / files / primary_file，交付
└─ false → 读 status
           ├─ 预检失败 → 读 reason（参数或锁的问题，改完再跑）
           ├─ 失败     → 读 reason_kind + advice（照转给人）
           │            └─ empty_result=true 时是 no_data，别重跑
           └─ 部分完成 → 有数据只是不足额，ok 其实也是 true，数据可用
```

参数校验没过时返回的是精简版，**只有 `ok`/`status`/`reason` 三个字段**，所以先判 `ok`，其余用 `.get()` 防御性读取。

## 失败怎么判：四个桶

`reason_kind` 只有四种，JSON 里已经带了中文名 `reason_kind_label` 和 `advice`，**直接转给人，不要自己另编说法**。

| `reason_kind` | 意思 | 该怎么办（= `advice` 原文） |
|---|---|---|
| `not_logged_in` | 没票、票过期、二维码没扫上、开跑前探针取不到数据 | 先补票再跑；不补票重跑结果一样 |
| `no_data` | 跑通了，平台确实就没这些数据 | **不是故障，别重跑**；换关键词或放宽条数 |
| `network` | 超时、断网、风控拦截、浏览器起不来、进程异常退出 | 等几分钟重试或换网络；先 `status` 看有没有残留浏览器占着。日志写「风控/验证码/400002」= 平台要人机验证，先手动过一次 |
| `other` | 参数校验没过、本机已有任务占锁、批量文件读不了、按了 Ctrl+C | 开跑前就被拦下的，按提示改，不用查网络和登录 |

- **`empty_result=true`** → `status` 是"失败"但 `reason_kind=no_data`。重跑一百次也一样。
- **快手（`ks`）已知状态**：票能搬进去，但平台自检接口不认（回 405），真跑还会撞搜索接口 `400002` 要人机验证。属平台风控，不是插件问题，**别反复重试**。
- **抖音/小红书登录态本机验不了**（接口要签名），预检只能"有票就放行"，票过期往往跑起来才暴露 —— `qr_prompted=true` 就是这个信号。

## 免扫码登录（用户最在意的一点）

用户不想反复扫码。桌面 Edge 的登录态**搬不过去**（新版 Edge 用 App-Bound `v20` 加密，钥匙绑死 msedge.exe 和原 profile 路径；默认 profile 还禁止开调试端口）。

**首选：让浏览器自己交钥匙。** `agent_crawler\edge_cookie_ext\` 是一个 Edge 扩展，在浏览器内部调官方 `chrome.cookies.getAll` 直接拿明文 —— 不用解密，也不受 v20 限制。

```
# 用户操作：桌面 Edge 登录好该平台 —— 之后就自动了（扩展后台每 15 分钟 / cookie 一变 30 秒内自己导出）
# 爬虫开跑时预检没票 → 自动 --import-json 最新导出 → 再验一次（日志写"发现扩展导出 → 自动导入"）
%MC_ROOT%\.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p tieba -t search -k "原神" -n 3 --json
%MC_ROOT%\.venv\Scripts\python.exe agent_crawler\edge_login_sync.py --check <平台>   # 实测有效性
```

- 扩展当前版本 **1.8**，后台自带自动导出（`background.js`）：后台启动即导一次 + `chrome.cookies.onChanged` 30 秒合并 + `alarms` 每 15 分钟兜底；文件 `mc_cookies_auto_*`，自己轮换只留 2 份；成功时图标角标显示条数（15 秒消失），失败显示红色 `!`。用户装/更新后需在 `edge://extensions` 点一次"重新加载"（**不要替用户装或重载**）。
- 两个必须知道的坑（2026-11-14 踩完）：①**这台机器上 `chrome.storage.local` 是坏的**（`Default\Local Extension Settings\<id>\000003.log` 恒 0 字节，`set` 静默失败），所以 1.4 起状态只放内存、关键链路不碰 storage；②**Service Worker 里没有 `URL.createObjectURL`**（只有页面有），1.5 起靠同源隐藏页 `offscreen.html` 造 blob 地址、失败退 `data:` 地址，manifest 因此需要 `offscreen` 权限。凡给这个扩展加功能，别用 `chrome.storage`，别在 SW 里用 DOM/blob API。
- 排查"点了重载没反应"：`edge://extensions` 卡片里点「服务工作进程」链接唤醒 SW 并看它的 console（平时显示"不活动"是正常休眠）。正常应依次出现 `后台脚本启动 x.x → 开始导出 → 读完 cookie → 拿到下载地址 → 导出成功`，卡在哪步问题就在哪步；只查 `History` 的 downloads 表没有行**不能**证明 SW 没跑过。
- 爬虫侧自动导入在 `agent_crawl.py` 的 `auto_import_from_export()`，默认开，`--no-auto-import` 关；只认 24 小时内的导出，太旧不导。
- 手动路线仍然可用（自动没跟上时）：点扩展"全部导出" → `--import-json all latest`。
- **贴吧搜索曾整体报错** `'str' object cannot be interpreted as an integer`：百度把 post 的 `time` 返回成数字字符串，`tools/time_util.py:66` 先 `int()` 判千分位、第 68 行却把原值丢给 `time.localtime()`。修法是在 `agent_crawl.py` 里用 `python -c` 包一层 `TIME_SHIM_SRC`（只把纯数字字符串转 int）再 `runpy.run_path('main.py')`，**没有改 media_platform 源码**。2026-11-13 实测：登录态首页 + 搜索出 6 条真数据。
- `--passthrough` 用 `split_passthrough()` 切分（保 `{…}`/引号里的空格）；JSON 里别写多余空格也行，但要整段包住。

- `--import-json <平台|all> <文件>`；文件写 `latest` 就自动取"下载"里最新的 `mc_cookies_*.json`；只想更新一个平台写 `--import-json bili latest`。
- 扩展只能由用户自己装一次：`edge://extensions` → 开发人员模式 → 加载解压缩的扩展 → 选 `agent_crawler\edge_cookie_ext`。**不要替用户装**。导出的 json 是明文凭据，导入完应删除。
- 备选路线（扩展没装 / 不想装）：用户从桌面 Edge F12 → 网络 → 右键请求 → 复制为 cURL → 存 txt，然后 `--import <平台> --curl-file <txt>`。
- 快手在桌面 Edge 本来就没登录，扩展也变不出票：要么让用户先在桌面登录快手再点按钮，要么扫码一次。
- **贴吧（百度系）走"原域名写入"**：`--import-json` 对 tieba 默认逐条照抄原始域名/路径/Secure/SameSite，并先清掉该域的旧 cookie（`--host-faithful` 可强制给其它平台用）。压平成单域名写会让 tieba ↔ passport 反复 302 成死循环（`ERR_TOO_MANY_REDIRECTS`），即使票本身有效。另外桌面 Edge 若只在百度账号登录过、没打开过贴吧，导出里不会有 `.tieba.baidu.com` 的会话票，靠 `BDUSS` 那一套也能进登录态首页。

- 详见 `agent_crawler\免扫码说明.txt`。`--import` 会先拿票去平台接口验一遍再写，写前自动备份目标 Cookies 库，写完回读比对。
- 预检（`agent_crawl.py` 的登录预检）现在会真去平台接口问一句，不再是"看文件在不在"；结果 `OK` 才放行，`?` 表示该平台接口要签名、本机验不了（dy/xhs），按有票放行。`--check` 若显示"读不到（浏览器正开着）"，那是该平台 profile 正被采集任务占用、Cookies 库被独占，不是登录失效，等跑完再看。
- 多个会话可能共用这台机器的爬虫：9222 端口和各平台 profile 是全局独占的。**现在有运行锁了**：`agent_crawler\run.lock` 是门口挂牌，写着谁在跑/什么任务/多久没刷新（每 30 秒心跳）。开跑被拒（`本机已有任务在跑…`）→ 就是别人在跑，等它跑完；要排队加 `--wait-lock 10`；**别杀别人的批次，也别手删别人的牌子**（只有牌子主人进程已死、或 30 分钟没刷新时才可删）。先看 `status`，它第一行就报锁现状。
- **别再手动"查进程看有没有别人在跑"了**：预检自己会查，而且会排除自己和自己的父进程（venv 的 `python.exe` 外面还套一层同命令行进程，手工判断极易把自己认成"别人"而误报冲突）。1.11.1 起**只读命令不再算冲突**（`status`/`defaults`/`version`/`export`/`estimate` 不占浏览器），所以"边等边查进度"是安全的，不会再把自己的 status 当成别人的任务。
- 登录态现状（2026-11-15 核）：bili、wb、zhihu 的爬虫 profile 登录态有效且实测跑通；zhihu 真跑拿到 18 条内容 + 1126 条评论。dy/xhs 的票本机验不了（接口要签名），以预检和真跑结果为准。快手票搬得进但平台不认（见上）。


任务一跑几分钟到几十分钟：**长任务用 `--detach` 甩到后台**（见上），别同步死等；**两个任务不能并行**（共享浏览器调试端口 9222），并发请求会被运行锁拒绝——`batch` 整条队列共用一个锁，批量请走 `batch` 而不是连发多个 `run`。

## 铁律

1. **判结果看 `--json` 的 `ok`/`status`/`reason_kind`**，不看退出码（经后台作业通道会被归一化成 1，不可信）；不加 `--json` 时才看输出末尾的 `RESULT:` 行。
2. **零产出分两种，别混**：跑挂了（登录/风控/网络/超时）→ 看 `reason_kind` 按原因处置；**跑通了但平台就是没内容**（`empty_result=true`，`reason_kind=no_data`）→ 状态仍是"失败"但原因写 `平台返回 0 条（…，关键词「XXX」）；证据日志：<平台日志原文>`，这是可信结论，**不许自动重试**（重试只会浪费时间还可能加深风控）。若写了"N 个关键词全部为空 → 更像是登录票降级或被风控限流"，才值得先 login 或换词再试一次。
3. 绝不通过调登录并发、限速参数来提速——那是封号风险，平台反爬限速是耗时主因，没有安全的提速手段。
4. **补票优先免扫码**：重跑 `run`（预检会自动导入 Edge 扩展的导出），或 `edge_login_sync.py --import-json all latest`；实在没法才跑 `login -p <平台>` 扫码（内部按 600 秒轮询，最多约 10 分钟，扫完自动继续）。注意 `--import`/`--import-json` 是 `edge_login_sync.py` 的参数，**不是** `agent_crawl.py` 的参数，别拼错命令。
5. 汇报必须给真实数字：实际抓到 X 条 / 请求 Y 条，禁止虚报进度。
6. `status` 报的进程数会**按启动链归并**（一次 run 实际 4 个 python 进程：venv 启动器→真进程→TIME_SHIM 壳→真进程）。看"任务数"判断有没有并发（看 `running_groups` 长度），别看到"4 个进程"就以为有 4 个人在跑。
7. **交付要 Excel 就 `export`，别手工转、别在原始数据上加工**：`agent_crawl.py export --task <任务名片段|绝对路径> --json`，只读磁盘（别人跑任务时也能用）。它只搬不改——不排序不去重不改正文不加统计列；派生只有「来源文件」列、列表转 JSON 文本、长数字按文本存、去掉 Excel 存不了的控制字符，全在口径页计数申报。`export` 会读回自己写的文件自查（冻结首行、筛选、长数字是文本、行数对账），回读不过返回 2，**此时不许说导出成功**。缺的类别标「未采集」，绝不用别的指标顶替。
8. **夜里跑长任务/批量先加 `--probe`**：dy/xhs 的票预检只能看文件在不在（接口要签名），票失效要跑到一半才暴露。`--probe` 用泛词真取 1 条，取不到就停在预检（不建任务目录），并区分"票失效/风控降级"、"弹了扫码窗口（补票命令）"、"超时（浏览器/网络）"。探针数据进 `_probe\` 成功即删，不进 `tasks\` 也不进历史；batch 只在队列开头探一次。单条小任务别开（冷启动 20~40 秒比任务还长）。
9. **用户问"还要多久"→ 直接跑 `status`，别自己猜**：锁有人在跑时会多一行 `↳ 进度估算：内容 16/60 条（27%）… 约还需 5分46秒`。它是数磁盘 jsonl 行数 + 从第一条数据落盘算速率，**线性外推，仅供排期参考**（实测偏高约 19%）；跑不满 90 秒或不足 3 条时只报已落盘条数。回答时把"估算"和"实际已落盘"分开说，不要把估算当成已完成条数报给用户。

## 速度预期（实测，用于排期和报价）

- 内容：每条约 15~20 秒（5 条 ≈ 2 分钟，30 条 ≈ 8~10 分钟）
- 评论：知乎实测 **1126 条评论 4 分 28 秒**（不含启动浏览器那 14 秒）。按此外推：1000 条约 4 分钟、5000 条约 20 分钟、**1 万条约 40 分钟**。
- 但"一万条"不是一条命令能给的：单次 `count` 上限 300，得靠多关键词 + 多任务叠出来，外推值只作参考 —— 其他平台、更大量级可能被限流拖慢，**不保证**。
- 大评论量用 `--max-comments 50` 这类深翻页，并走 `batch` 分批跑，防风控；不要靠加大参数提速。
- 超时由封装自动估算（`10 + ceil(count/10) + ceil(requested×max_comments/60)` 分钟，钳到 3~180）；显式 `--timeout-min` 如实生效。

## 结果与汇报

- 数据落在 `%MC_ROOT%\tasks\<任务名>\attempt_N\<平台>\jsonl\`；Excel 默认落 `agent_crawler\exports\`
- **做表前先看列名**：`agent_crawler\列名对照表.md`。同是点赞有 `liked_count`/`like_count`/`voteup_count`；发布时间有秒、有毫秒、也有现成字符串（贴吧 `publish_time`、微博 `create_date_time`）。用错列会整表错。
- 封装输出自带逐条对账 + 3 条抽查（`spot_check`），直接引用其结论汇报
- PowerShell 陷阱：内联 `python -c` 带中文会被 GBK 编码搅乱——写成临时 UTF-8 `.py` 文件再跑；外部命令必须是 pwsh 脚本的最后一条语句，否则拿不到退出码
