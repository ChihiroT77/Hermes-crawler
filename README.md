# Hermes —— 让 AI 帮你抓社交平台数据

**一句话：装好之后，你只要对 AI 说「帮我抓 B 站 10 条人工智能视频的评论」，它自己就去干了。**

你不需要懂代码，不需要记命令。它把 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 这个专业爬虫包了一层「翻译壳」，让 AI 能听懂你的话、自己跑完、把结果存成 Excel。

支持 MCP 协议 —— Claude Desktop、Cursor、Trae、dsh 这类 AI 客户端都能接上。

---

## 这东西解决什么问题

MediaCrawler 本身很能打，但它是**给程序员用的**：要配环境、要背十几个参数、要自己盯进度、失败了还看不出原因。

Hermes 在它外面加了一层壳：

| 原来的麻烦 | Hermes 的做法 |
|---|---|
| 参数十几个，记不住 | 十个工具，每个都有中文说明，AI 自己会挑 |
| 跑起来没反馈，不知道死了没 | 让 AI 一次等到跑完，直接拿结果 |
| 失败了不知道为什么 | 返回里直接写清楚失败原因（没登录 / 被限流 / 参数错） |
| 数据是一堆 jsonl，不会用 | 一条命令导成排版好的 Excel，双击就能看 |
| 任务跑一半断了 | 能接着跑，重复的自动跳过 |
| 两个任务撞车把浏览器搞坏 | 启动前先检查，冲突直接拦住 |

---

## 快速开始

### 最省事：双击安装（推荐给不懂命令行的人）

1. 打开 <https://github.com/ChihiroT77/Hermes>
2. 点右上角绿色的 **Code** 按钮 → **Download ZIP**
3. 下载完**右键解压**（千万别直接在压缩包里双击，会装不上）
4. 进解压出来的文件夹，**双击 `一键安装.bat`**
5. 等它自己跑完。中途可能要你点一两次确认，按提示来就行

装完窗口里会告诉你「接下来做什么」，照着做即可。

> **不需要装 Git，也不需要自己去下 MediaCrawler。**
> 安装包里自带一份打好补丁的源码（`vendor/mediacrawler-ded4273.zip`），全程不联网下载源码。

### 方式 A：用命令行装（懂一点技术的用这个）

```powershell
git clone https://github.com/ChihiroT77/Hermes.git Hermes
cd Hermes
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

默认装在 `%USERPROFILE%\MediaCrawler`。

### 方式 B：让 AI 来装（你说的"解放双手"）

把仓库里的 `install_for_ai.md` 连同整个文件夹交给你的 AI，然后说：

> 请读一下这个目录里的 `install_for_ai.md`，然后按里面写的把 Hermes 装好并接通。

**AI 读的是那份几百字的指引，不是整个项目**。它知道先干什么、怎么判断成功、失败了怎么办。

---

## 装之前需要什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10/11 | 进程检测用了 PowerShell，Mac/Linux 没适配 |
| Python | **3.11 以上**（推荐 3.12） | 去 <https://www.python.org/downloads/> 下，安装时**必须勾上 "Add python.exe to PATH"**（这一步漏了后面全跑不起来） |
| 浏览器 | Edge 或 Chrome | Windows 自带 Edge 就行，不用另装 |
| Git | 可以不装 | 用「双击安装」方式完全不需要它 |
| 磁盘 | 约 1.5 GB | 源码 + Python 环境 + 浏览器内核 |

**不需要**你自己去下 MediaCrawler —— 源码已经打包在安装包里。

> **装之前建议先自查一件事**：按 `Win` → 输入 `powershell` → 回车，在黑窗口里敲 `python --version` 并回车。
> 如果显示的版本是 3.11 或更高，就说明 Python 装好了。如果提示「不是内部或外部命令」，说明没装或没勾 PATH。

---

## 装完怎么用

有两种用法。**只想让 AI 帮你干活，直接看「用法二」**；想自己跑一次看看效果，看「用法一」。

### 用法一：自己跑一次（验证装好了）

这里要打开「命令行」。**不会开的照这个做**：按 `Win` 键 → 输入 `powershell` → 回车，就出来一个黑窗口。然后把下面的命令**复制粘贴**进去，按回车。

**第 1 步：登录（每个平台只需一次）**

```powershell
.venv\Scripts\python.exe agent_crawler\agent_crawl.py login -p bili
```

会弹出浏览器窗口显示二维码，**用手机 B 站 App 扫一下**，扫完窗口自动关，以后不用再扫。

> 注意：命令要在**安装目录**里执行。如果提示找不到文件，先 `cd` 到安装目录（默认是 `%USERPROFILE%\MediaCrawler`）。

平台代号：`bili`（B站）/ `dy`（抖音）/ `wb`（微博）/ `zhihu`（知乎）/ `tieba`（贴吧）/ `ks`（快手）。

**第 2 步：抓一次**

```powershell
.venv\Scripts\python.exe agent_crawler\agent_crawl.py run -p bili -k "人工智能" -n 10
```

跑完会把数据存到 `tasks\<任务名>\` 下面，Excel 可以直接双击打开。

### 用法二：接上 AI，让它干活（推荐）

这一步就是把 Hermes「介绍」给你的 AI 客户端。**不同软件位置不一样，下面逐个说。**

#### 先拿到两个路径

配置里要填两处路径，都是你自己电脑上的（**不能照抄别人的**）：

- Python 程序：`<安装目录>\.venv\Scripts\python.exe`
- 服务脚本：`<安装目录>\agent_crawler\mcp_server.py`

`<安装目录>` 默认是 `C:\Users\你的用户名\MediaCrawler`。不确定的话，去那个文件夹，点地址栏就能看到完整路径。**路径里的反斜杠要写两个**（`\` 写成 `\\`）。

#### 配到哪个软件？

<details>
<summary><b>Trae / Cursor 这类编辑器</b>（点开看步骤）</summary>

1. 打开软件的**设置**（快捷键一般是 `Ctrl + ,`）
2. 在设置里搜 `MCP`
3. 找到「MCP Servers」或「添加 MCP 服务器」，选**手动编辑 JSON**
4. 把下面的内容粘进去（路径换成你自己的）
5. 保存，**重启软件**

</details>

<details>
<summary><b>Claude Desktop</b>（点开看步骤）</summary>

1. 打开 Claude Desktop → 左上角菜单 → **Settings** → **Developer**
2. 点 **Edit Config**，会自动打开一个叫 `claude_desktop_config.json` 的文件
3. 把下面的内容粘进去（路径换成你自己的）
4. 保存，**完全退出 Claude（右键托盘图标 → Quit）再重新打开**

</details>

<details>
<summary><b>dsh</b>（点开看步骤）</summary>

dsh 用的是插件，不是 MCP 配置。插件在仓库的 `dsh-plugin/` 里，11 个工具和 MCP 版一一对应（含 `hermes_wait` 等任务跑完）。

不过这个外壳**没在真实的 dsh 里验证过**，所以出问题请先用 MCP 方式接（配法和上面 Trae 一样）。

</details>

#### 要粘的内容

```json
{
  "mcpServers": {
    "Hermes": {
      "command": "C:\\Users\\你的用户名\\MediaCrawler\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\你的用户名\\MediaCrawler\\agent_crawler\\mcp_server.py"]
    }
  }
}
```

> **如果原来的文件里已经有别的东西**（比如已经有 `mcpServers` 一节），不要整个覆盖掉，只把 `"Hermes": { ... }` 这一段加进去，注意用逗号隔开。

#### 怎么确认接上了

重启客户端后，问它一句：

> 你有 Hermes 的工具吗？列出来给我看。

它应该能列出十个工具（`crawl_start`、`crawl_wait` 等）。列出来了就是接通了。

#### 然后就能这样说话

> 用 Hermes 帮我抓 B 站「人工智能」相关视频 10 条，带评论，每个视频抓 20 条。
>
> 抓完导成 Excel 给我。

**你不用记任何命令**，AI 会自己调工具、自己等、自己处理失败。

---

## 可选：装个插件，以后都不用扫码

**不装也能用**。不装的话，每个平台第一次要手机扫一次码，扫完就一直有效（登录票会过期，过期了要再扫）。

装了的话，插件会在你正常用浏览器时顺手把登录票导出来，省掉扫码这一步。

**怎么装（只需一次）：**

1. 进安装目录，打开 `agent_crawler` 文件夹
2. **双击 `安装免扫码插件.bat`**
3. 它会自动帮你复制文件、打开 Edge 扩展页和一个文件夹
4. 在扩展页里手动做三步：
   - 第 1 步：打开**开发人员模式**开关（页面右上角或左下角）
   - 第 2 步：点**加载解压缩的扩展**
   - 第 3 步：选中刚弹出的那个文件夹，点「选择文件夹」
5. 工具栏出现一个蓝色小人图标 = 成功

> **为什么最后三步必须你手动点？** 浏览器不允许程序替用户装扩展，这是 Edge / Chrome 的安全限制，绕不过去。装完就永久生效了。

详细说明见 `agent_crawler\免扫码说明.txt`。

**注意**：插件只对**同一个浏览器**有效。你平时用什么浏览器刷 B 站，插件就装在哪个浏览器里。

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

**Q：提示「没登录」怎么办？**
跑一次登录命令扫码就行（见上面「用法一」第 1 步）。也可以装那个免扫码插件，省掉这一步。

**Q：能不能爬小红书？**
不建议。反爬机制最狠，验证码有时人工也过不去。代码还在，但别指望稳定跑通。

**Q：为什么第一次跑很慢？**
要建 Python 环境、下浏览器内核。只慢这一次，之后就好了。

**Q：能同时跑两个任务吗？**
不能。两个任务抢同一个浏览器会把状态搞乱，脚本会直接拦住你。

**Q：数据存哪了？**
安装目录下的 `tasks\` 文件夹，每个任务一个子文件夹，里面的 `.xlsx` 双击就能看。

**Q：要抓的数据在哪能改？**
不用改配置文件。直接用自然语言告诉 AI 就行（关键词、条数、要不要评论、每篇多少条评论）。

**Q：Mac / Linux 能用吗？**
不能。脚本用了 Windows 专有的进程检测和浏览器路径。

**Q：怎么彻底卸载？**
删掉安装目录（默认 `C:\Users\你的用户名\MediaCrawler`）就行了，Python 环境就在里面，不会污染系统。
如果装过免扫码插件，再去 Edge 扩展页把它移除，并删掉 `%LOCALAPPDATA%\MediaCrawler免扫码插件\` 这个文件夹。

---

## 许可证（重要，请读）

本项目**不是**独立爬虫，而是 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的封装层 + 补丁。

MediaCrawler 采用 [NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)：

- ✅ 可以用于**学习和研究**
- ❌ **不能商用**
- ❌ 不能大规模爬取
- ⚠️ 分发时必须保留原作者的版权声明

**因此：**

1. 本仓库**附带**一份打好补丁的 MediaCrawler 源码（`vendor/mediacrawler-ded4273.zip`），目的是让人离线安装。
   该源码版权归原作者所有，许可全文见 `vendor/mediacrawler-LICENSE.txt`。
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
├── vendor/
│   ├── mediacrawler-ded4273.zip     打好补丁的 MediaCrawler 源码（离线安装用）
│   └── mediacrawler-LICENSE.txt     原作者许可证全文
├── patches/                 27 个补丁（改 MediaCrawler 的地方）
├── newfiles/                新增文件（封装层，原项目里没有的）
│   ├── agent_crawler/       核心：命令行、MCP 服务、文档
│   └── tools/               Excel 导出
├── dsh-plugin/              dsh 插件版（实验性，见下）
├── VERSION                  版本号
└── LICENSE-NOTICE.md        许可证说明
```

**关于 `dsh-plugin/`：** 这里是把同一套内核包成 dsh 插件外壳的版本（工具名 `hermes_*`，共 11 个）。它只对 dsh 有效，其他 MCP 客户端用不上。

工具已经齐了（`hermes_run` 点火、`hermes_wait` 等结果、`hermes_status` 看进度…），但**没在真实的 dsh 环境里跑过**——我只能在本机模拟 dsh 的工具注册表验证逻辑，装进 dsh 由 AI 真正调用这一步验不了。所以定位是「实验性」：能用 MCP 就先用 MCP，dsh 这条等有人实际用过反馈。

---

## 更新

已经装过的话，重新跑一次 `install.ps1` 就行 —— 它会识别「以前装过」，跳过打补丁，只把封装层覆盖成最新版。

---

## 反馈

这是个人项目，主要给自己用。有问题提 issue，但不保证及时回复。
