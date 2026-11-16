# Hermes —— 让 AI agent 帮你抓社交平台数据

把 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 包成一层 **AI 可调用的爬虫插件**。
你对 AI 说「爬 B 站 10 条人工智能视频」，它自己去跑、等结果、导 Excel。

支持 MCP 协议 —— Claude Desktop、Cursor、dsh 这类客户端都能接。

---

## 这东西解决什么问题

MediaCrawler 本身是个很强的爬虫，但它是**给程序员用的**：要配环境、要记参数、要盯进度、要自己判断失败原因。

Hermes 在它外面加了一层壳：

| 原来的麻烦 | Hermes 的做法 |
|---|---|
| 参数十几个，记不住 | 十个 MCP 工具，带中文说明 |
| 跑起来没反馈，不知道死了没 | `crawl_wait` 一次性等完给结果 |
| 失败了不知道为什么 | 返回 JSON 里直接写 `reason_kind` |
| 数据是一堆 jsonl，不会用 | 一条命令导成排版好的 Excel |
| 任务跑一半断了 | `crawl_resume` 接着跑，按 ID 去重 |
| 两个任务撞车把浏览器搞坏 | 启动前预检，冲突直接拦 |

---

## 快速开始

### 方式 A：让人来装（最快）

```powershell
git clone https://github.com/ChihiroT77/Hermes.git Hermes
cd Hermes
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

装完会提示下一步。默认装在 `%USERPROFILE%\MediaCrawler`。

### 方式 B：让 AI 来装（你说的"解放双手"）

把这句丢给你的 AI：

> 请读一下这个目录里的 `install_for_ai.md`，然后按里面写的把 Hermes 装好并接通。

**AI 读的是那份几百字的指引，不是整个项目**。它知道先干什么、怎么判断成功、失败了怎么办。

---

## 装之前需要什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10/11 | 进程检测用了 PowerShell，Mac/Linux 没适配 |
| Git | 任意版本 | 用来下载 MediaCrawler |
| Python | **3.11 以上**（推荐 3.12） | 安装时勾 "Add python.exe to PATH" |
| 浏览器 | Edge 或 Chrome | Windows 自带 Edge 就行 |
| 磁盘 | 约 1.5 GB | 源码 + Python 环境 + 浏览器内核 |

**不需要**你自己去下 MediaCrawler —— 脚本会装。

---

## 装完怎么用

### 先登录（每个平台一次）

```powershell
.venv\Scripts\python.exe agent_crawler\agent_crawl.py login -p bili
```

会把浏览器弹出来，手机扫码，登录完自动关。平台代号：`bili` / `dy` / `wb` / `zhihu` / `tieba` / `ks`。

### 再跑一次

```powershell
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p bili -k "人工智能" -n 10
```

### 接 AI 客户端

在客户端的 MCP 配置里加（把 `<安装目录>` 换成你的实际路径）：

```json
{
  "mcpServers": {
    "Hermes": {
      "command": "<安装目录>\\.venv\\Scripts\\python.exe",
      "args": ["<安装目录>\\agent_crawler\\mcp_server.py"]
    }
  }
}
```

接完重启客户端，会看到十个工具。

---

## 十个工具

| 工具 | 干什么 |
|---|---|
| `crawl_start` | 开跑（默认后台） |
| `crawl_wait` | **启动后接着等，跑完一次性给结果** |
| `crawl_status` | 查进度 / 看历史 |
| `crawl_cancel` | 喊停（优雅收尾，数据保留） |
| `crawl_estimate` | 只估算：大概几条、要等多久 |
| `crawl_export` | 把采好的数据导成 Excel |
| `crawl_batch` | 批量队列 |
| `crawl_resume` | 断点续跑 + ID 去重 |
| `crawl_login_check` | 只查登录态，不弹窗 |
| `crawl_defaults` | 看 / 改默认参数 |

---

## 平台支持情况

| 平台 | 内容 | 评论 | 备注 |
|---|---|---|---|
| B站 `bili` | ✅ | ✅ | 推荐 |
| 抖音 `dy` | ✅ | ✅ | 推荐 |
| 微博 `wb` | ✅ | ✅ | 推荐 |
| 知乎 `zhihu` | ✅ | ✅ | 推荐 |
| 贴吧 `tieba` | ✅ | ❌ | 无评论数据 |
| 快手 `ks` | ✅ | ❌ | 无评论数据 |
| 小红书 `xhs` | ⚠️ | ⚠️ | **已软弃用**，反爬最严，不保证能跑通 |

---

## 输出格式

`jsonl`（默认，最全）/ `json` / `csv` / `excel`。

Excel 分页：`口径说明` + `内容` / `评论` / `创作者` + `统一格式` / `统一格式_评论`（各平台列名统一后的版本，跨平台合并数据用这个）。

---

## 常见问题

**Q：能不能爬小红书？**
不建议。反爬机制最狠，强验证码人工有时也过不去。代码留着，但别指望稳定。

**Q：为什么第一次跑很慢？**
要下 Playwright 浏览器内核，还要建 Python 环境。装完就好。

**Q：能同时跑两个任务吗？**
不能。同一个浏览器 profile 并发会互相踩。预检会直接拦住。

**Q：数据存哪？**
`tasks\<任务名>\attempt_N\<平台>\jsonl\`。Excel 导出默认在 `agent_crawler\exports\`。

**Q：Mac / Linux 能用吗？**
不能用。进程检测和浏览器路径都是 Windows 专用。

---

## 许可证（重要，请读）

本项目**不是**独立爬虫，而是 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的封装层 + 补丁。

MediaCrawler 采用 [NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)：

- ✅ 可以用于**学习和研究**
- ❌ **不能商用**
- ❌ 不能大规模爬取
- ⚠️ 分发时必须保留原作者的版权声明

**因此：**

1. 本仓库**不包含 MediaCrawler 源码**，只包含补丁和封装层。安装时由脚本从官方仓库下载。
2. 本仓库的所有内容同样**仅供学习研究，禁止商用**。
3. 使用本项目产生的任何法律后果由使用者自行承担。请遵守目标平台的 `robots.txt` 和服务条款。

细看 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)。

---

## 目录结构

```
Hermes/
├── install.ps1              一键安装脚本（支持从零装）
├── install_for_ai.md        给 AI agent 读的安装指引
├── 一键安装.bat             双击就能跑（Windows）
├── patches/                 27 个补丁（改 MediaCrawler 的地方）
├── newfiles/                新增文件（封装层，原项目里没有的）
│   ├── agent_crawler/       核心：命令行、MCP 服务、文档
│   └── tools/               Excel 导出
├── dsh-plugin/              dsh 插件版（实验性，未完成，见下）
├── VERSION                  版本号
└── LICENSE-NOTICE.md        许可证说明
```

**关于 `dsh-plugin/`：** 这里是把同一套内核包成 dsh 插件外壳的版本（工具名 `hermes_*`）。它只对 dsh 有效，其他 MCP 客户端用不上，目前**还没做完**，暂时不建议使用。常规使用请看上面的 MCP 配置。

---

## 更新

已经装过的话，重新跑一次 `install.ps1` 就行 —— 它会识别「以前装过」，跳过打补丁，只把封装层覆盖成最新版。

---

## 反馈

这是个人项目，主要给自己用。有问题提 issue，但不保证及时回复。
