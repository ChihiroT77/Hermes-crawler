# 给 AI 看的安装指引（Hermes 爬虫插件）

> 这份文件是给 **AI agent** 读的。人类用户可以只看 `README.md`。
> 你要做的事：把 Hermes 装到这台 Windows 电脑上，并接到 AI 客户端里。
> **每一步都要看到实际输出确认成功，不要假设成功。**

---

## 0. 你要先确认的三件事

依次在这台电脑上执行，把结果记下来：

```powershell
# 1) 是不是 Windows
$PSVersionTable.PSVersion      # 有输出即 Windows PowerShell
# 2) 有没有 git
git --version                  # 期望：git version 2.x
# 3) 有没有 Python 3.11+
python -c "import sys;print(sys.version)"
```

**判定：**

- `git` 没有 → 停下，告诉用户："请先装 Git（https://git-scm.com/download/win），装完重开终端，再让我继续。"不要自己下载安装包去装。
- Python 低于 3.11 或没有 → 停下，告诉用户："请先装 Python 3.12（https://www.python.org/downloads/windows/），安装时勾上 Add python.exe to PATH。"
- 两项都有且版本够 → 继续第 1 步。

---

## 1. 问用户装到哪（不要自己猜）

**默认建议**：`$env:USERPROFILE\MediaCrawler`（即 `C:\Users\<用户名>\MediaCrawler`）。

问用户一句：

> 「MediaCrawler 默认装在 `C:\Users\<你的用户名>\MediaCrawler`，可以吗？想换盘的话告诉我路径（比如 `D:\MediaCrawler`）。」

**注意：**

- 路径里**不要有空格和中文**，更不要放 `Program Files`（权限问题）。
- 如果用户指定的目录已存在且非空，**不要删**，换个目录或问用户怎么办。

---

## 2. 跑安装脚本

安装包根目录有 `install.ps1`。执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<安装包目录>\install.ps1" -Target "<用户指定的目录>"
```

**如果你要无人值守（不弹"按回车关闭"）**，先设环境变量：

```powershell
$env:MC_NO_PAUSE = '1'
```

**这一步会做什么**（你要能跟用户解释清楚）：

1. 检查 git / python
2. `git clone` 官方 MediaCrawler（国内网络慢的话，加 `-RepoUrl https://ghfast.top/https://github.com/NanmiCoder/MediaCrawler.git`）
3. 切到补丁对应的基线版本
4. 建 `.venv`、装依赖（**3~8 分钟，没输出是正常的**）
5. 打 27 个补丁
6. 复制封装层（`agent_crawler\` 等）
7. 确认浏览器
8. 自检

**怎么判断成功**：脚本最后打印

```
  装完了 —— Hermes <版本号>
```

并且第 9 步自检里出现版本号和 `status` 的输出。**看到这两样才算成功。**
脚本以退出码 0 结束。

**先干跑一遍更稳**（不改任何文件）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<安装包目录>\install.ps1" -Target "<目录>" -DryRun
```

---

## 3. 失败怎么处理（按症状查）

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `没装 git` | 环境缺 git | 让用户装 Git，重开终端 |
| `没找到 Python 3.11 以上` | 环境缺 Python | 让用户装 Python 3.12 并勾 PATH |
| `下载失败` | 连不上 GitHub | 加 `-RepoUrl` 用镜像重跑 |
| `一半能打、一半打不上` | 源码版本不对或有改动 | **别硬来**。脚本没落盘任何改动。重新干净 clone 一份再装 |
| `依赖没装全` | pip 网络问题 | 让用户执行脚本给出的那条 pip 命令，或换镜像：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| `文件被占用` | 有爬虫在跑 | 让用户关掉正在跑的任务再重试 |

**铁律**：报错时**不要**去改 MediaCrawler 的源码、不要手工编辑 `.env`、不要删用户的 `browser_data` 或 `tasks` 目录。

---

## 4. 登录（这一步 AI 替不了）

装完**不代表能爬**。每个平台都要用户自己扫码登录一次。

**先查状态**（不弹窗，安全）：

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\agent_crawl.py" status
```

要看某平台登录态：

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\agent_crawl.py" login -p bili --check-only
```

**需要补登录时**，告诉用户：

> 「请到电脑前，我要弹一个 B 站页面出来，你用手机扫码登录，登录完我会自动关掉窗口。」

然后执行：

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\agent_crawl.py" login -p bili
```

**不要**在用户不在电脑前时跑 `login`。也不要反复重试 `login` —— 一次不行就请用户来处理。

---

## 5. 接进 AI 客户端（MCP）

在客户端的 MCP 配置里加：

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

把 `<安装目录>` 换成第 1 步用户给的绝对路径，**反斜杠要写成 `\\`**。

**接之前先自测服务本身能不能启动**：

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\mcp_server.py" --selftest
```

期望输出里能看到工具清单（`crawl_start`、`crawl_wait` 等 10 个）。

**接完必须重启客户端**，否则工具不会出现。

---

## 6. 装完怎么验（一定要做完再宣布完成）

### 6.1 命令行自检

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\agent_crawl.py" version
```

期望：打印版本号（如 `1.11.1`）。

### 6.2 真爬一次（建议 5 条，别贪多）

```powershell
& "<安装目录>\.venv\Scripts\python.exe" "<安装目录>\agent_crawler\agent_crawl.py" run -p bili -k "学习" -n 5 --json
```

**判定看 JSON 里的字段**，不看退出码：

- `"ok": true` 且 `status` 表示成功 → 通过
- `reason_kind` 是 `not_logged_in` → 回到第 4 步让用户登录
- 其他失败 → 把 JSON 原样贴给用户，别自己瞎猜

### 6.3 MCP 工具验（如果接了客户端）

让客户端调用 `crawl_status`，能返回结果就说明接线通了。

**汇报格式**：版本号 + 真爬结果（几条/耗时）+ MCP 是否通。**三样都有才叫装好了。**

---

## 7. 你绝对不能做的事

1. **不要修改 MediaCrawler 的原始源码**（除了安装脚本打的补丁）。排查问题只读不写。
2. **不要碰** `.env`、`state.json` 里的登录信息、`browser_data\`、已导出的 Excel。
3. **不要把用户的登录昵称、uid、本机路径写进任何交付文档**。
4. **不要加并发**、不要同时跑两个任务（同一个浏览器 profile 会冲突）。
5. **不要替用户装浏览器扩展**（免扫码扩展必须用户自己在 `edge://extensions` 里加载）。
6. **不要承诺**小红书能跑通（反爬最严，已软弃用）。

---

## 8. 装完给用户的一句话

> 「装好了，版本 X.X.X。已经真爬了 5 条 B 站数据验证通过。现在你说要爬什么平台、什么关键词、多少条就行 —— 第一次用某个平台会需要你扫码登录一次。」

---

## 附：完整的参数文档在哪

- `agent_crawler\使用说明.md` —— 给人看的完整手册
- `agent_crawler\给AI的调用说明.md` —— 每个参数、每个返回字段的详解
- `agent_crawler\SKILL.md` —— 精简版操作要点
- `agent_crawler\列名对照表.md` —— 各平台字段对照
