# -*- coding: utf-8 -*-
"""
agent_crawl.py — MediaCrawler 参数化封装层（方案 B）
====================================================

设计目标（按用户优先级）：
  1. 不出错     —— 预校验 / 登录预检 / 冲突检测 / 数量对账 / 完整性抽查，
                   成功零废话，失败给明确原因 + 补救建议，绝不假进度。
  2. 高效率     —— 一次命令构造全部参数；默认值记忆；批量任务队列；
                   固定报告格式（人看 + 机器解析）。
  3. 零侵入     —— 不修改 MediaCrawler 任何源码；本文件与状态全部位于
                   agent_crawler/ 独立目录（git 不跟踪）。

子命令：
  run        单任务爬取（核心入口）
  batch      批量任务队列（JSON 文件，顺序执行 + 进度汇总）
  login      触发平台扫码登录（保存登录态）
  status     查看最近任务历史 / 当前冲突进程 / 残留浏览器
  defaults   查看 / 修改记忆的默认参数

用法示例：
  .venv\\Scripts\\python.exe agent_crawler\\agent_crawl.py run -p bili -k "考研" -n 20
  .venv\\Scripts\\python.exe agent_crawler\\agent_crawl.py run -p 小红书 -k "露营" -n 30 -f excel
  .venv\\Scripts\\python.exe agent_crawler\\agent_crawl.py login -p zhihu
  .venv\\Scripts\\python.exe agent_crawler\\agent_crawl.py batch --file tasks.json

退出码：0=成功；2=部分完成；1=失败/预检未通过；130=用户中断
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import queue
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# 常量与路径
# ----------------------------------------------------------------------------
# 版本号：只在这里改一处。SKILL.md / 文档 / MCP 都读这个常量，避免几处写法打架。
# 语义：主.次.修 —— 主=不兼容的用法变化；次=加新命令/新字段；修=只修 bug 不改接口。
AGENT_VERSION = "1.11.1"
AGENT_VERSION_NOTE = "第十二轮：预检不再把 status/defaults/version/export/estimate 这类只读命令当成『别人在跑』（修掉白拦一次）"

HOME = Path(__file__).resolve().parent                 # 封装层所在目录
ROOT = HOME.parent                                      # 项目根
TASKS_ROOT = ROOT / "tasks"                             # 固定归档根目录
STATE_FILE = HOME / "state.json"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
MAIN_PY = ROOT / "main.py"

# 运行锁：多会话/多 agent 共用这台机器时，谁开跑谁在门口挂牌，避免抢同一个浏览器
LOCK_FILE = HOME / "run.lock"
LOCK_HEARTBEAT_SEC = 30     # 心跳间隔（秒）：还在跑就定期刷一下牌子时间
LOCK_STALE_SEC = 1800       # 牌子超过 30 分钟没动静 → 视为失效，允许接管
# status 里"估一下还要多久"的门槛：太早估全是冷启动噪声，不如不估
PROGRESS_MIN_ELAPSED = 90   # 秒
PROGRESS_MIN_SAMPLES = 3    # 条

# 后台运行（run --detach）：主进程把活交给一个脱离的子进程，自己立刻返回。
# detach 之后主进程就不再管这个任务了，进度靠 status，喊停靠 cancel。
CANCEL_FILE = HOME / "cancel.request"      # 喊停的纸条：谁要停，就写到这里
DETACH_LOG_DIR = HOME / "logs"             # 后台任务的屏幕输出落这里（state.json 不存这些）
CANCEL_POLL_SEC = 2                        # run_attempt 里每轮检查有没有喊停

PLATFORM_ALIASES: Dict[str, str] = {
    "xhs": "xhs", "小红书": "xhs", "xiaohongshu": "xhs", "红书": "xhs",
    "dy": "dy", "抖音": "dy", "douyin": "dy",
    "ks": "ks", "快手": "ks", "kuaishou": "ks",
    "bili": "bili", "bilibili": "bili", "b站": "bili", "B站": "bili", "哔哩哔哩": "bili",
    "wb": "wb", "微博": "wb", "weibo": "wb",
    "tieba": "tieba", "贴吧": "tieba", "百度贴吧": "tieba",
    "zhihu": "zhihu", "知乎": "zhihu",
}
# 平台可用性备注：只提示、不禁止。软弃用而不是删代码——删了以后谁想自己折腾就没路了，
# 而且删平台要动 PLATFORM_ALIASES、平台目录表等一堆地方，收益不值得风险。
PLATFORM_NOTES: Dict[str, str] = {
    "xhs": ("小红书反爬最狠（强验证码，人工也经常过不去），本项目不推荐、不保证能跑通。"
            "要稳定出数据请换 bili / zhihu / wb / tieba。"),
}
SUPPORTED_FORMATS = {"json", "jsonl", "csv", "excel"}
FORMAT_ALIASES = {"json": "json", "jsonl": "jsonl", "csv": "csv",
                  "excel": "excel", "xlsx": "excel", "表格": "excel"}
TYPE_ALIASES = {"search": "search", "搜索": "search",
                "detail": "detail", "详情": "detail",
                "creator": "creator", "创作者": "creator", "博主": "creator"}

# 日志原因 → (原因分类, 是否可自动重试)
REASON_RULES: List[Tuple[str, str, bool]] = [
    ("登录未完成（未扫码/二维码超时）",
     r"Waiting for scan code login|login .*failed|not found qrcode|Login.*failed", False),
    ("平台风控/反爬拦截",
     r"\b(403|412)\b|risk[_ ]?control|访问异常|请求被拦截|操作太频繁|滑动验证|验证码"
     r"|400002|ANTICRAWL|captcha|indentity-verification", False),
    # 快手实测：票是有效的，但快手自己的登录自检接口（graphql visionProfileUserList）回 405，
    # 爬虫据此认为没登录，去点「登录」按钮，找得到就扫码、找不到就 30 秒超时。
    # 这种情况不是网络问题，重试也不会变。
    ("快手登录自检未通过（自检接口不认票，爬虫转去点登录按钮后超时）",
     r"waiting for locator\(\"//p\[text\(\)='登录'\]\"\)", False),
    ("网络异常",
     r"TimeoutError|ConnectionError|SSLError|MaxRetryError|NetworkError|timed?\s?out|net::ERR", True),
    ("浏览器异常/启动失败",
     r"Failed to launch|Target closed|Browser closed|ChannelClosed|Executable doesn't exist", True),
]

# 「平台确实执行了、但返回 0 条」的日志特征。
# 这不是基础设施故障：进程正常跑完，平台就是说"这个关键词没内容"。
# 下面每一行的正则在 media_platform\<平台>\core.py 源码和 tasks\ 历史日志里都核对过，
# 不是凭想象写的。新增平台特征时请同样先找真实字符串。
# (平台短名, 正则, 这个特征在平台侧是什么意思)
EMPTY_RESULT_RULES: List[Tuple[str, str, str]] = [
    ("dy", r"search douyin keyword: .* is empty,\[\]|aweme_list:\[\]",
     "抖音搜索接口返回空数组"),
    # B站有两种循环，日志不一样：普通关键词搜索是 "moving to next keyword"，
    # 按时间范围爬是 "on <日期>, moving to next day"（core.py:233 / core.py:308）
    ("bili", r"No more videos for .* moving to next (?:keyword|day)",
     "B站搜索接口无结果"),
    ("xhs", r"\[XiaoHongShuCrawler\.search\] No more content",
     "小红书搜索接口无后续内容"),
    ("zhihu", r"No more content!|\[ZhihuCrawler\.search\] Search content error",
     "知乎搜索接口无内容"),
    ("tieba", r"Search note list is empty",
     "贴吧搜索接口返回空列表"),
    ("ks", r"got 0 videos",
     "快手搜索接口返回 0 个视频"),
]
# 曾经想用 Excel 层的「没有可导出的数据，跳过生成 Excel」当通用空证据，
# 实测不可靠：jsonl 格式下这层照样初始化、照样打这句，成功任务里也有。
# 所以这条不能用，空结果只认平台自己的日志。
# 微博搜索在 code 里对空结果没有任何日志（filter_search_result_card 过滤完就静默），
# 所以只能走弱证据：日志里确实有搜索行，但整个任务一条没落盘。
WEAK_EMPTY_PLATFORMS = {"wb": r"\[WeiboCrawler\.search\] search weibo keyword:"}

# 从各类日志行里抠关键词名（各平台行格式不同，抠不到就返回空，不硬猜）
_KEYWORD_LINE_RE = re.compile(r"keyword[:：]\s*(.+?)\s*(?:,\s*page|,\s*date|,\s*aweme_list|,\s*got|$)")

DEFAULT_STATE: Dict[str, Any] = {
    "defaults": {
        "count": 20,            # 每个关键词爬取条数
        "format": "jsonl",
        "type": "search",
        "comments": True,       # 是否抓评论
        "max_comments": 10,     # 每条内容的评论数
        "media": False,         # 是否下载媒体文件
        "headless": False,      # 兜底值：是否无头模式（实际生效值以 state.json 为准）
        "retries": 1,           # 基础设施类错误自动重试次数
        "count_cap": 2000,      # 单次数量硬上限
        "timeout_base_min": 10, # 超时 = base + ceil(count/10) 分钟
        "timeout_max_min": 180,
    },
    "history": [],
}

SEP = "═" * 64

# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def _stdout_utf8() -> None:
    global REAL_OUT
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    REAL_OUT = sys.stdout


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = json.loads(json.dumps(DEFAULT_STATE))
    # 补齐缺失键
    for k, v in DEFAULT_STATE.items():
        st.setdefault(k, json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v)
    for k, v in DEFAULT_STATE["defaults"].items():
        st["defaults"].setdefault(k, v)
    return st


def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def sanitize_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s[:40] if s else "task"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}分{s:02d}秒"


# ----------------------------------------------------------------------------
# 给机器读的输出（--json）
# 原则：给人和给 AI 看的两条通道分开，互不干扰。
#   • 不加 --json：屏幕上那套报告一个字不变（照旧）。
#   • 加 --json：人类看的进度/报告全部改道到 stderr，stdout 只留一段 JSON。
# 字段名固定、缺值补空（""/0），不随任务类型变——AI 读一次就知道每个位置是什么。
# ----------------------------------------------------------------------------
REAL_OUT = sys.stdout       # 真正的 stdout（_stdout_utf8 里重新绑定，未重绑时即当前值）


class _stdout_to_stderr:
    """--json 模式下，把 print 出来的过程信息改道到 stderr，给 stdout 让路。"""

    def __init__(self, on: bool) -> None:
        self.on = on
        self.real = None

    def __enter__(self):
        if self.on:
            self.real = sys.stdout
            sys.stdout = sys.stderr
        return self

    def __exit__(self, *exc):
        if self.on and self.real is not None:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            sys.stdout = self.real
        return False


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def emit_json(obj: Any) -> None:
    REAL_OUT.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
    REAL_OUT.flush()


def _file_payload(f: Dict[str, Any]) -> Dict[str, Any]:
    out = {"path": str(f.get("path", "")), "name": str(f.get("name", "")),
           "kind": str(f.get("kind", "")), "size": int(f.get("size") or 0)}
    if "records" in f:
        out["records"] = int(f.get("records") or 0)
    if "sheets" in f:
        out["sheets"] = dict(f.get("sheets") or {})
    return out


def result_payload(r: Dict[str, Any]) -> Dict[str, Any]:
    """把一次任务的结果整理成固定结构的字典（run 与 batch 共用）。"""
    vp = r.get("params") or {}
    primary = r.get("primary") or {}
    sc = r.get("spot_check") or {}
    task_dir = str(r.get("task_dir", ""))
    att = int(r.get("attempt") or 0)
    mx = int(r.get("max_attempts") or 1)
    kind = reason_kind(r.get("reason", ""))
    return {
        "ok": r.get("status") in ("成功", "部分完成"),
        "status": str(r.get("status", "")),
        "reason": str(r.get("reason", "")),
        "reason_kind": kind,
        "reason_kind_label": kind_label(kind),
        "advice": kind_advice(kind),
        "name": str(vp.get("name", r.get("name", ""))),
        "platform": str(vp.get("platform", "")),
        "platform_note": str(r.get("params", {}).get("platform_note", "")),
        "type": str(vp.get("type", "")),
        "keywords": list(vp.get("keywords") or []),
        "count_per_keyword": int(vp.get("count") or 0),
        "format": str(vp.get("format", "")),
        "requested": int(r.get("requested") or 0),
        "contents": int(r.get("contents") or 0),
        "comments": int(r.get("comments") or 0),
        "attempt": att,
        "retries": max(0, att - 1),
        "max_attempts": mx,
        "empty_result": bool(r.get("empty_result")),
        "exit_code": r.get("exit_code"),
        "qr_prompted": bool(r.get("qr_prompted")),
        "duration_s": round(float(r.get("duration") or 0), 1),
        "task_dir": task_dir,
        "report": str(Path(task_dir) / "report.md") if task_dir else "",
        "log_path": str(r.get("log_path", "")),
        "primary_file": str(primary.get("path", "")),
        "files": [_file_payload(f) for f in (r.get("files") or [])],
        "spot_check": {"ok": sc.get("ok"), "samples": list(sc.get("samples") or [])},
        "probe": bool(vp.get("probe")),
        "probe_auto": bool(vp.get("probe_auto_reason")),
        "probe_note": str(vp.get("probe_auto_reason") or ""),
    }


def run_ps(script: str, timeout: int = 60) -> str:
    """PowerShell -EncodedCommand 执行，规避引号地狱；返回 stdout（utf-8）。"""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return r.stdout or ""
    except Exception as e:
        return f"__PS_ERROR__|{e}"


def ps_find_crawler_procs(exclude_pids: Optional[set] = None) -> List[Tuple[str, str]]:
    """检测正在运行的 MediaCrawler 进程（python*.exe main.py 或 agent_crawl.py）。
    返回 [(pid, cmdline)]。uvicorn(api.main:app) 不会匹配。
    说明：这里同时认 main.py 与 agent_crawl.py —— 只认 main.py 会漏掉"别的会话已开跑、
    但 main.py 还没起来"的间隙（2026-11-14 实测踩到）。exclude_pids 用于排除自己。"""
    script = r'''
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$rows = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match '^(python|pythonw)\.exe$' -and $_.CommandLine -match '(main\.py|agent_crawl\.py)'
}
foreach ($r in $rows) { '{0}|{1}' -f $r.ProcessId, $r.CommandLine }
'''
    out = run_ps(script)
    skip = {str(p) for p in (exclude_pids or set())}
    procs: List[Tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("__PS_ERROR__"):
            continue
        pid, _, cmd = line.partition("|")
        if pid.strip().isdigit() and pid.strip() not in skip:
            procs.append((pid.strip(), cmd.strip()))
    return procs


def ps_proc_table() -> Dict[int, int]:
    """pid → 父 pid 全表。只给「进程链归并」用，拿不到就返回空 dict（归并不起来不算错）。"""
    script = r'''
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.ParentProcessId }
'''
    out = run_ps(script, timeout=90)
    table: Dict[int, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("__PS_ERROR__"):
            continue
        a, _, b = line.partition("|")
        if a.strip().isdigit() and b.strip().isdigit():
            table[int(a)] = int(b)
    return table


# 封装层认得的子命令。run/batch/login 会真去开浏览器抓数据；
# status/defaults/version/export/estimate 只是抬头看一眼、算一算、搬个文件。
_SUB_RE = r"agent_crawl\.py\"?\s+(run|batch|login|status|defaults|export|estimate|resume|cancel|version)"
_READONLY_SUBS = {"status", "defaults", "version", "export", "estimate"}


def _proc_label(cmd: str) -> str:
    """从命令行抠一个能让人认出来的标签：平台 + 子命令。抠不到就返回空串。"""
    plat = ""
    m = re.search(r"(?:--platform|-p)\s+([a-zA-Z]+)", cmd)
    if m:
        plat = m.group(1)
    sub = ""
    if "agent_crawl.py" in cmd:
        m2 = re.search(_SUB_RE, cmd)
        if m2:
            sub = m2.group(1)
    elif "main.py" in cmd:
        sub = "main.py"
    return " ".join(x for x in (plat, sub) if x)


def _proc_is_busy(cmd: str) -> bool:
    """这条进程算不算「真在干活、会占住浏览器」？

    为什么需要：ps_find_crawler_procs 只认命令行里有没有 agent_crawl.py，
    于是 `agent_crawl.py status --json` 这种只读命令也被当成「别人正在爬」。
    AI 一边等结果一边查进度，预检就会把它自己刚查的那一下当并发冲突拦掉
    （2026-11-16 真机踩到：日志里是两个 agent_crawl.py status --json）。

    规则：认得出是只读子命令 → 不算；认得出是干活子命令 → 算；
    认不出（main.py、命令行残缺）→ 保守算，宁可多拦一次。"""
    m = re.search(_SUB_RE, cmd)
    if m:
        return m.group(1) not in _READONLY_SUBS
    if "main.py" in cmd:
        return True
    # 命令行里只有 agent_crawl.py、看不到子命令（可能被截断）：保守算在干活
    return "agent_crawl.py" in cmd


def merge_proc_chains(procs: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """把同一条启动链上的爬虫进程归成一个「逻辑任务」。
    实测一次 run 会同时出现 4 个进程：venv 启动器 → 真进程 → TIME_SHIM shell → 真进程。
    各算一个的话 status 会报「4 个进程正在运行」，看着像并发跑了 4 个任务，纯误导。
    归并办法：顺着父 pid 往上找，最顶上的那个「也叫爬虫」的祖先进程就是这个任务的头，
    凡是它的子孙都归到它名下。返回 [{root, pids, members, label}]，按头 pid 排序。
    注意：祖先进程若已退出，父 pid 可能被系统复用，理论上会把两个任务并成一个；
    这里只用于显示，不影响任何安全判定（并发拦截靠 run.lock）。"""
    if not procs:
        return []
    matched = {int(pid): cmd for pid, cmd in procs if str(pid).isdigit()}
    table = ps_proc_table()

    def top_matched_ancestor(pid: int) -> int:
        root, cur, hops = pid, pid, 0
        while hops < 64:          # 防陈旧数据里的父链成环
            hops += 1
            ppid = table.get(cur)
            if not ppid or ppid == cur:
                break
            if ppid in matched:
                root = ppid
            cur = ppid
        return root

    groups: Dict[int, List[Tuple[str, str]]] = {}
    for pid, cmd in sorted(matched.items()):
        groups.setdefault(top_matched_ancestor(pid), []).append((str(pid), cmd))
    out = []
    for root, mem in groups.items():
        # 链里第一个成员往往是 `-c "import sys, runpy..."` 那种壳，抠不出东西，
        # 所以挨个试，取第一个能抠出「平台+子命令」的
        label = ""
        for _, cmd in mem:
            label = _proc_label(cmd)
            if label:
                break
        out.append({"root": root, "pids": [p for p, _ in mem], "members": mem, "label": label})
    out.sort(key=lambda g: g["root"])
    return out


def ps_find_crawler_browsers(platform: Optional[str] = None) -> List[Tuple[str, str]]:
    """检测残留的爬虫 Edge 主进程（--user-data-dir 含 cdp_*_user_data_dir 且非子进程）。
    platform=None 时匹配所有平台。"""
    pat = f"cdp_{platform}_user_data_dir" if platform else "_user_data_dir"
    script = r'''
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$pat = '__PAT__'
$rows = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object {
  $_.CommandLine -like ('*'+$pat+'*') -and $_.CommandLine -notlike '*--type=*'
}
foreach ($r in $rows) { '{0}|{1}' -f $r.ProcessId, $r.CommandLine }
'''.replace("__PAT__", pat)
    out = run_ps(script)
    procs: List[Tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("__PS_ERROR__"):
            continue
        pid, _, cmd = line.partition("|")
        if pid.strip().isdigit():
            procs.append((pid.strip(), cmd.strip()))
    return procs


def ps_scan_environment(platform: Optional[str], exclude_pids: Optional[set] = None
                        ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """一次 PowerShell 同时取「爬虫进程」和「残留浏览器」。

    预检原本各起一次 PowerShell，固定启动开销约 2 秒；合并后只起一次。
    解析规则与 ps_find_crawler_procs / ps_find_crawler_browsers 完全一致，
    返回 (procs, browsers)。"""
    pat = f"cdp_{platform}_user_data_dir" if platform else "_user_data_dir"
    script = r'''
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$pat = '__PAT__'
$pp = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match '^(python|pythonw)\.exe$' -and $_.CommandLine -match '(main\.py|agent_crawl\.py)'
}
foreach ($r in $pp) { 'P|{0}|{1}' -f $r.ProcessId, $r.CommandLine }
$bb = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object {
  $_.CommandLine -like ('*'+$pat+'*') -and $_.CommandLine -notlike '*--type=*'
}
foreach ($r in $bb) { 'B|{0}|{1}' -f $r.ProcessId, $r.CommandLine }
'''.replace("__PAT__", pat)
    out = run_ps(script)
    skip = {str(p) for p in (exclude_pids or set())}
    procs: List[Tuple[str, str]] = []
    browsers: List[Tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("__PS_ERROR__"):
            continue
        kind, _, rest = line.partition("|")
        pid, _, cmd = rest.partition("|")
        if not pid.strip().isdigit():
            continue
        if kind == "P":
            if pid.strip() not in skip:
                procs.append((pid.strip(), cmd.strip()))
        elif kind == "B":
            browsers.append((pid.strip(), cmd.strip()))
    return procs, browsers


def graceful_close_browser(pid: str, wait_sec: int = 12) -> bool:
    """优雅关闭浏览器（WM_CLOSE，等其自行退出保存状态）。绝不强杀（防 profile 损坏）。"""
    try:
        subprocess.run(["taskkill", "/PID", pid], capture_output=True, timeout=15)
    except Exception:
        pass
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(1.5)
        alive = any(p == pid for p, _ in ps_find_crawler_browsers())
        if not alive:
            return True
    return False


def kill_process_tree(pid: int) -> None:
    """强杀进程树（仅用于超时/中断时的 python 子进程）。"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
    except Exception:
        pass


def graceful_stop_crawler(proc: "subprocess.Popen", grace_sec: int = 20) -> bool:
    """超时/中断时发送停止信号。实测注意（2026-11-09 超时演练）：MediaCrawler 未注册
    SIGBREAK 处理器，Windows 收到 CTRL_BREAK 后由系统直接终结进程——收尾钩子
    （关浏览器/Excel 导出）不会运行，但 jsonl 因逐行落盘而完整；残留浏览器由
    封装层安全网 WM_CLOSE 清理。返回 True=进程在 grace_sec 内退出，False=强杀。"""
    if proc.poll() is None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # 需 CREATE_NEW_PROCESS_GROUP
        except Exception:
            pass
        try:
            proc.wait(timeout=grace_sec)
            return True
        except subprocess.TimeoutExpired:
            pass
    kill_process_tree(proc.pid)
    return False


# ----------------------------------------------------------------------------
# 登录态预检
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 运行锁（门口挂牌）
# ----------------------------------------------------------------------------
class LockBusy(Exception):
    """锁被别的会话/进程持有，本次不该开跑。"""


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    out = run_ps("$p = Get-Process -Id %d -ErrorAction SilentlyContinue; "
                 "if ($p) { 'ALIVE' } else { 'GONE' }" % int(pid), timeout=20)
    return "ALIVE" in out


def _own_process_chain() -> set:
    """自己 + 全部祖先进程 pid。

    为什么需要：.venv\\Scripts\\python.exe 只是个启动壳，真正跑代码的是它拉起的系统
    Python 子进程（实测父 48724 → 子 13164）。只排除 os.getpid() 会漏掉这层壳，
    于是每次跑任务都把自己判成"别人在跑"。给 exclude_pids 用。
    """
    out = run_ps(r'''
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
foreach ($p in Get-CimInstance Win32_Process) { '{0}|{1}' -f $p.ProcessId, $p.ParentProcessId }
''')
    ppid: Dict[int, int] = {}
    for line in out.splitlines():
        pid, _, par = line.strip().partition("|")
        if pid.isdigit():
            ppid[int(pid)] = int(par) if par.strip().isdigit() else 0
    chain: set = set()
    cur = os.getpid()
    for _ in range(32):
        if cur <= 0 or cur in chain:
            break
        chain.add(cur)
        cur = ppid.get(cur, 0)
    return chain


def read_lock() -> Optional[Dict[str, Any]]:
    """读牌子：None=没人挂牌；{} = 牌子在但内容读不出（可能正被别人写）。"""
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def describe_lock(data: Optional[Dict[str, Any]]) -> str:
    if not data:
        return "牌子在，但内容读不出来"
    started = float(data.get("started_ts") or 0)
    dur = human_duration(time.time() - started) if started else "未知"
    return (f"PID {data.get('pid')}｜任务「{data.get('task') or '?'}」"
            f"｜平台 {data.get('platform') or '?'}｜已跑 {dur}"
            f"｜启动于 {data.get('started_at') or '?'}")


class RunLock:
    """抢占 → 心跳 → 释放。同一时刻全机只允许一个持锁者。"""

    def __init__(self) -> None:
        self.held = False
        self.fresh = False          # 刚抢到（供预检判断是否做深度检查）
        self.data: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._beat: Optional[threading.Thread] = None

    def acquire(self, vp: Dict[str, Any], wait_min: float = 0.0) -> List[str]:
        notes: List[str] = []
        data = read_lock()
        takeover = False

        if data is not None:
            holder = int(data.get("pid") or 0) if isinstance(data, dict) else 0
            try:
                mtime = LOCK_FILE.stat().st_mtime
            except OSError:
                mtime = 0
            fresh = (time.time() - mtime) < LOCK_STALE_SEC

            if holder == os.getpid():
                notes.append("锁已由本进程持有 ✓")
                self.held = True
                self.data = data
                return notes

            if holder and _pid_alive(holder) and fresh:
                # 别人真的在跑：默认直接拒绝（不硬闯、不杀别人任务）
                deadline = time.time() + max(0.0, float(wait_min)) * 60
                while time.time() < deadline:
                    notes.append(f"锁被占用，等待中（最多 {wait_min:g} 分钟）…")
                    time.sleep(5)
                    again = read_lock()
                    if again is None:
                        break
                    holder2 = int(again.get("pid") or 0) if isinstance(again, dict) else 0
                    if not holder2 or not _pid_alive(holder2):
                        break
                if read_lock() is not None:
                    raise LockBusy(
                        "本机已有任务在跑，同一浏览器/端口不能并发：\n  "
                        + describe_lock(read_lock())
                        + "\n  ↳ 等它跑完再来。若确认是失效残留（人已跑、进程已死），"
                          f"手动删掉 {LOCK_FILE.name} 再重试；封装层不会去杀别人的任务。")
            else:
                notes.append("发现失效的牌子，接管：" + describe_lock(data))
                takeover = True

        if takeover:
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise LockBusy(f"摘不掉旧牌子（{e}），请手动删除 {LOCK_FILE} 后重试。")

        # 原子创建：两个进程同时抢时，只有一个能建成功
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockBusy("另一会话刚刚抢先拿到锁，本次退出以免撞车。")

        self.data = {
            "pid": os.getpid(),
            "task": vp.get("name", ""),
            "platform": vp.get("platform", ""),
            "keywords": ",".join(vp.get("keywords") or []),
            "started_at": now_str(),
            "started_ts": time.time(),
            "host": os.environ.get("COMPUTERNAME", ""),
            "cmd": " ".join(sys.argv[1:])[:300],
        }
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        self.held = True
        self.fresh = True
        notes.append(f"已挂牌（PID {os.getpid()}｜任务「{self.data['task']}」）✓")
        self._start_beat()
        return notes

    def update_task(self, vp: Dict[str, Any]) -> None:
        """批量队列里换任务：把牌子上的任务名改成当前这个。"""
        if not self.held:
            return
        self.data.update({"task": vp.get("name", ""), "platform": vp.get("platform", ""),
                          "keywords": ",".join(vp.get("keywords") or [])})
        try:
            LOCK_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _start_beat(self) -> None:
        def beat() -> None:
            while not self._stop.wait(LOCK_HEARTBEAT_SEC):
                try:
                    if LOCK_FILE.exists():
                        os.utime(LOCK_FILE, None)   # 只刷时间戳，告诉别人"我还活着"
                except Exception:
                    pass

        self._beat = threading.Thread(target=beat, daemon=True)
        self._beat.start()

    def release(self) -> None:
        self._stop.set()
        if not self.held:
            return
        try:
            data = read_lock()
            if isinstance(data, dict) and int(data.get("pid") or 0) == os.getpid():
                LOCK_FILE.unlink()      # 只摘自己的牌子
        except Exception:
            pass
        self.held = False
        self.fresh = False


_LOCK: Optional[RunLock] = None


# ----------------------------------------------------------------------------
# 喊停（cancel）
# 做法：不硬杀别人的进程，只递一张"纸条"（cancel.request），干活的那个进程每 2 秒
# 自己看一眼纸条；看到写的是自己，就按超时那套优雅收尾（发停止信号 → 关浏览器 →
# 写报告 → 摘牌）。这样数据不会被腰斩，登录态也不会被弄坏。
# ----------------------------------------------------------------------------
def read_cancel() -> Optional[Dict[str, Any]]:
    """读纸条：None=没人喊停；{} = 纸条在但读不出内容。"""
    try:
        # utf-8-sig：别的工具（记事本/PowerShell）写进来可能带 BOM，别因为这个当成没纸条
        data = json.loads(CANCEL_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def clear_cancel() -> None:
    try:
        CANCEL_FILE.unlink()
    except Exception:
        pass


def clear_stale_cancel() -> bool:
    """撕掉过期纸条：纸条指向的进程已经不在了，就不是给现在这个任务留的。

    为什么要主动撕：任务被强杀时来不及自己清纸条，纸条就留在磁盘上；等下一个任务
    开跑，一看纸条就以为"有人要我停"，白跑一场。所以每次开跑前先清一次。
    """
    c = read_cancel()
    if c is None:            # 压根没纸条
        return False
    try:
        pid = int(c.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    # 纸条在、但内容残缺或指向一个已不在的进程 → 是上一个任务留下的，撕掉
    if not pid or not _pid_alive(pid):
        clear_cancel()
        return True
    return False


def cancel_targets_me() -> bool:
    """纸条上写的是不是我这个进程。"""
    c = read_cancel()          # None=没纸条；{}=纸条坏了
    if not c:                  # 两种都不该让我停下（坏纸条由 clear_stale_cancel 收拾）
        return False
    try:
        return int(c.get("pid") or 0) == os.getpid()
    except (TypeError, ValueError):
        return False


def auto_import_from_export(platform: str, max_age_hours: float = 24.0) -> Tuple[bool, str]:
    """预检没过时，自动把 Edge 扩展最新导出的票导进爬虫 profile，再让预检重试一次。

    这就是"用户在桌面 Edge 登录好，剩下全自动"的那一环：
    扩展在后台自动导出 → 这里自动导入 → 用户什么都不用点。
    没装扩展 / 没导出文件 / 文件太旧时，什么也不改，只回一句原因。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import edge_login_sync as els
    except Exception as e:  # 模块坏了不能拖累爬虫
        return False, f"导入模块不可用（{type(e).__name__}: {e}）"

    try:
        found = els.newest_export()
    except Exception as e:
        return False, f"找导出文件出错（{type(e).__name__}: {e}）"
    if not found:
        return False, "下载文件夹里没有 mc_cookies_*.json（扩展还没导出过）"

    try:
        age_h = (time.time() - os.path.getmtime(found)) / 3600.0
    except OSError:
        age_h = 0.0
    if age_h > max_age_hours:
        return False, f"最新导出是 {age_h:.1f} 小时前的，太旧不敢用"

    print(f"  发现扩展导出：{os.path.basename(found)}（{age_h * 60:.0f} 分钟前）→ 自动导入 {platform}")
    try:
        data = json.load(open(found, "r", encoding="utf-8"))
        per = els._platforms_from_json(data)
    except Exception as e:
        return False, f"导出文件读不出来（{type(e).__name__}: {e}）"
    target = platform if platform in per else "all"
    if target == "all":
        print(f"  这份导出里没有 {platform}，改成全量导入")

    try:
        rc = els.cmd_import_json(target, found, None, False, False, False)
    except Exception as e:
        return False, f"自动导入出错（{type(e).__name__}: {e}）"
    if rc != 0:
        return False, f"自动导入没成功（返回 {rc}）"
    return True, os.path.basename(found)


def check_login_state(platform: str) -> Tuple[bool, str]:
    """登录态预检：先看有没有 profile，再真的拿 cookie 去平台接口问一句。
    "?" 表示这个平台本机验不了（接口要签名），按"有票就算过"处理。"""
    base = ROOT / "browser_data"
    candidates = [
        base / f"cdp_{platform}_user_data_dir",
        base / f"{platform}_user_data_dir",
    ]
    ud = None
    for c in candidates:
        if c.is_dir():
            ud = c
            break
    if ud is None:
        return False, f"未找到 {platform} 的登录数据目录（browser_data/cdp_{platform}_user_data_dir）"
    default = ud / "Default"
    cookie = (default / "Cookies").is_file() or (default / "Network" / "Cookies").is_file()
    if not (default.is_dir() and cookie):
        return False, f"目录 {ud.name} 存在但未发现 Cookies，登录态可能已失效"

    # 实测：拿票去平台接口问一句（只读，不改文件，不启动浏览器）
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import edge_login_sync as els
        jar = els.read_platform_cookies(str(ud), platform)
        flat = els._jar_for(jar, els.PLATFORM_DOMAINS[platform])
        verdict = els.probe_login(platform, flat)
    except Exception as e:
        return True, f"已找到登录数据目录 {ud.name}（实测失败：{e}）"
    if verdict.startswith("OK"):
        return True, f"{verdict}｜{ud.name}"
    if verdict.startswith("FAIL"):
        hint = ("    1) 最省事：在桌面 Edge 点一下「爬虫登录票导出」扩展按钮，然后执行\n"
                "       .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --import-json all latest\n"
                f"    2) 或者：F12 → 网络 → 右键复制为 cURL → --import {platform} --curl-file 文件\n"
                f"    3) 都嫌麻烦：扫码一次 agent_crawler\\agent_crawl.py login --platform {platform}")
        if "没有登录票" in verdict:
            return False, f"{platform} 的爬虫浏览器里没有登录票。\n{hint}"
        return False, f"{verdict} → 按下面的办法补票：\n{hint}"
    return True, f"{verdict}｜{ud.name}"


# ----------------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------------
class ParamError(Exception):
    pass


def validate_params(p: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    d = st["defaults"]
    # 平台
    raw_platform = str(p.get("platform", "")).strip()
    if not raw_platform:
        raise ParamError("缺少平台参数 --platform（推荐：bili/B站、zhihu/知乎、wb/微博、tieba/贴吧；"
                         "dy/抖音、ks/快手 可试；xhs/小红书 不推荐）")
    platform = PLATFORM_ALIASES.get(raw_platform.lower(), PLATFORM_ALIASES.get(raw_platform))
    if platform is None:
        raise ParamError(f"不支持的平台「{raw_platform}」。支持：bili/dy/ks/wb/tieba/zhihu"
                         f"（xhs 已不推荐，仍可用但不保证；均支持中文别名）")

    # 关键词
    raw_kw = str(p.get("keywords", "")).strip()
    if not raw_kw:
        raise ParamError("缺少关键词 --keywords，且不能为空字符串")
    keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]
    if not keywords:
        raise ParamError(f"关键词解析为空：{raw_kw!r}")
    if len(keywords) > 10:
        raise ParamError(f"关键词过多（{len(keywords)} 个，上限 10）")

    # 数量（按每个关键词计）
    try:
        count = int(d["count"]) if p.get("count") is None else int(p["count"])
    except (TypeError, ValueError):
        raise ParamError(f"数量 --count 必须是正整数，收到：{p.get('count')!r}")
    if count < 1:
        raise ParamError(f"数量必须 ≥ 1，收到：{count}")
    if count > int(d["count_cap"]):
        raise ParamError(f"数量 {count} 超过上限 {d['count_cap']}（可在 state.json 调整 count_cap）")

    # 格式
    raw_fmt = str(p.get("format") or d["format"]).strip().lower()
    fmt = FORMAT_ALIASES.get(raw_fmt)
    if fmt is None:
        raise ParamError(f"不支持的格式「{raw_fmt}」。封装层支持：json/jsonl/csv/excel"
                         f"（数据库类格式请直接使用 main.py）")

    # 类型与专属参数
    raw_type = str(p.get("type") or d["type"]).strip().lower()
    ctype = TYPE_ALIASES.get(raw_type)
    if ctype is None:
        raise ParamError(f"不支持的任务类型「{raw_type}」。支持：search/detail/creator")
    specified_id = (p.get("specified_id") or "").strip()
    creator_id = (p.get("creator_id") or "").strip()
    if ctype == "detail" and not specified_id:
        raise ParamError("detail（详情）类型需要 --specified_id（内容ID，多个用逗号分隔）")
    if ctype == "creator" and not creator_id:
        raise ParamError("creator（创作者）类型需要 --creator_id（创作者ID，多个用逗号分隔）")

    # 任务名
    name = sanitize_name(str(p.get("name") or keywords[0]))

    # 其他开关（注意：argparse 省略时值为 None，须显式回落默认值）
    comments = d["comments"] if p.get("comments") is None else bool(p["comments"])
    try:
        max_comments = int(d["max_comments"]) if p.get("max_comments") is None else int(p["max_comments"])
    except (TypeError, ValueError):
        raise ParamError("--max_comments 必须是正整数")
    if max_comments < 1 or max_comments > 20000:
        raise ParamError("--max_comments 取值范围 1~20000")
    media = d["media"] if p.get("media") is None else bool(p["media"])
    headless = d["headless"] if p.get("headless") is None else bool(p["headless"])
    try:
        retries = int(p.get("retries") if p.get("retries") is not None else d["retries"])
    except (TypeError, ValueError):
        raise ParamError("--retries 必须是 0~3 的整数")
    if retries < 0 or retries > 3:
        raise ParamError("--retries 取值范围 0~3")

    # 起始页（续跑用）：0=不传 --start，走原码默认（各平台是第 1 页）
    try:
        start_page = int(p.get("start_page") or 0)
    except (TypeError, ValueError):
        raise ParamError("--start 必须是整数页码")
    if start_page < 0:
        raise ParamError("--start 不能是负数")

    # 请求总量基数：search=每词数量×词数；detail/creator=ID 个数
    if ctype == "detail":
        requested = len([x for x in specified_id.split(",") if x.strip()])
    elif ctype == "creator":
        requested = len([x for x in creator_id.split(",") if x.strip()])
    else:
        requested = count * len(keywords)

    # 超时（分钟）：base + ceil(count/10)；开评论时按实测(~16s/条)追加评论拉取时长
    # 实测校准：bili 30条×cap10评论(199条) 评论阶段≈8min；宁可超时宽，避免 90% 进度被假超时
    if p.get("timeout_min"):
        # 显式指定：尊重用户值（含 <3 分钟的短超时），仅做 ≥1 与全局上限保护
        timeout_min = max(1, min(int(p["timeout_min"]), int(d["timeout_max_min"])))
    else:
        timeout_min = int(d["timeout_base_min"]) + -(-count // 10)
        if comments:
            timeout_min += -(-(requested * max_comments) // 60)
        # 自动值才做下限钳制，避免过短假超时
        timeout_min = max(3, min(timeout_min, int(d["timeout_max_min"])))

    # 探针三态：显式 --probe / --no-probe 优先；都没给就按「验不了票的平台 + 长任务」自动开。
    # is_login_task 是登录任务本身，探针没意义（它就是在测登录）。
    probe = bool(p.get("probe", False))
    probe_auto_reason = ""
    if p.get("probe") is None and not p.get("is_login_task", False):
        probe, probe_auto_reason = auto_probe_decision(platform, requested)

    return {
        "platform": platform, "keywords": keywords, "count": count, "format": fmt,
        "platform_note": PLATFORM_NOTES.get(platform, ""),
        "type": ctype, "specified_id": specified_id, "creator_id": creator_id,
        "name": name, "comments": comments, "max_comments": max_comments,
        "media": media, "headless": headless, "retries": retries,
        "auto_import": True if p.get("auto_import") is None else bool(p["auto_import"]),
        "timeout_min": timeout_min, "passthrough": p.get("passthrough") or "",
        "is_login_task": bool(p.get("is_login_task", False)),
        "probe": probe, "probe_auto_reason": probe_auto_reason,
        "wait_lock": max(0.0, float(p.get("wait_lock") or 0)),
        "start_page": start_page,
        "requested": requested,
    }


# ----------------------------------------------------------------------------
# 预检（冲突 / 残留浏览器 / 登录态）
# ----------------------------------------------------------------------------
def preflight(vp: Dict[str, Any], deep: bool = True) -> List[str]:
    """返回需要打印的预检动作记录；冲突时抛 ParamError。

    deep=True ：做"别人在跑吗 / 有残留浏览器吗"的深度检查 —— 只在刚抢到锁时做一次。
    deep=False：本进程已持锁（批量队列的后续任务），跳过复查。否则会把上一轮自己的
                收尾进程/浏览器误判成"别人在跑"，进而误关自己的浏览器。
    """
    notes: List[str] = []

    if deep:
        # 1)+2) 一次扫描同时拿进程和残留浏览器（省一次 PowerShell 启动，约 2 秒）
        procs, leftovers = ps_scan_environment(vp["platform"], exclude_pids=_own_process_chain())
        # 只读命令（status/defaults/version/export/estimate）不占浏览器，不算冲突。
        # 否则 AI 边等边查进度，会把自己刚跑的那条 status 当成"别人在爬"而白拦一次。
        busy = [(pid, cmd) for pid, cmd in procs if _proc_is_busy(cmd)]
        if busy:
            groups = merge_proc_chains(busy)
            lines = "\n".join(
                f"    任务 {g['root']}{'（' + g['label'] + '）' if g['label'] else ''}"
                f"：{len(g['members'])} 个进程\n"
                + "\n".join(f"      PID {pid}: {cmd[:150]}" for pid, cmd in g["members"])
                for g in groups)
            raise ParamError(
                f"检测到已有 MediaCrawler 任务正在运行"
                f"（{len(groups)} 个任务 / 共 {len(busy)} 个进程，同一条启动链已归并）：\n{lines}\n"
                f"  ↳ 同一浏览器 profile 无法并发；请等待其完成，或手动结束后重试。"
            )
        notes.append("无爬虫进程冲突 ✓")

        # 2) 残留浏览器（可能持有 profile 锁）。持锁状态下见到的才是真残留，可以关
        if leftovers:
            for pid, cmd in leftovers:
                notes.append(f"发现残留浏览器 PID {pid}，正在优雅关闭…")
                if graceful_close_browser(pid):
                    notes.append(f"残留浏览器已关闭 ✓（登录态保留在磁盘）")
                else:
                    raise ParamError(
                        f"平台 {vp['platform']} 的爬虫浏览器（PID {pid}）无法优雅关闭。\n"
                        f"  ↳ 请手动关闭该 Edge 窗口后重试；封装层不会强杀浏览器（防登录态损坏）。"
                    )
        else:
            notes.append("无残留浏览器 ✓")
    else:
        notes.append("本进程已持锁，跳过进程/浏览器复查 ✓")

    # 3) 登录态（登录任务自身跳过）
    if not vp["is_login_task"]:
        ok, msg = check_login_state(vp["platform"])
        if not ok and vp.get("auto_import", True):
            # 先别急着报错：扩展可能已经把最新票导出来了，导进来再验一次
            did, why = auto_import_from_export(vp["platform"])
            if did:
                ok2, msg2 = check_login_state(vp["platform"])
                if ok2:
                    ok, msg = True, f"{msg2}（自动导入 {why}）"
                else:
                    msg = f"{msg2}（已自动导入 {why}，还是不行）"
            else:
                print(f"  自动导入没做：{why}")
        if not ok:
            raise ParamError(
                f"平台 {vp['platform']} 未登录：{msg}\n"
                f"  ↳ 最省事：在桌面 Edge 登录好该平台，扩展会自动导出，重跑本命令即可\n"
                f"     也可以手动：点扩展图标 → 全部导出 → 再跑一次\n"
                f"     实在要扫码：agent_crawl.py login -p {vp['platform']}"
            )
        notes.append(f"登录预检通过 ✓（{msg}）")
    return notes


# ----------------------------------------------------------------------------
# 任务目录
# ----------------------------------------------------------------------------
def make_task_dir(vp: Dict[str, Any]) -> Path:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y%m%d")
    base = f"{vp['name']}_{vp['platform']}_{date}"
    if vp["is_login_task"]:
        base = f"_login_{vp['platform']}_{date}"
    task_dir = TASKS_ROOT / base
    n = 2
    while task_dir.exists():
        task_dir = TASKS_ROOT / f"{base}_r{n}"
        n += 1
    task_dir.mkdir(parents=True)
    # 把本次参数落一份在任务目录里：跑的过程中 status 只能靠它算"请求多少条"，
    # 事后也能从目录本身看出这次请求的是什么（不用翻 state.json 历史）。
    try:
        (task_dir / "params.json").write_text(json.dumps({
            "name": vp.get("name", ""), "platform": vp.get("platform", ""),
            "type": vp.get("type", ""), "keywords": vp.get("keywords") or [],
            "count": vp.get("count", 0), "requested": vp.get("requested", 0),
            "comments": bool(vp.get("comments")),
            "max_comments": vp.get("max_comments", 0),
            "media": bool(vp.get("media")), "headless": bool(vp.get("headless")),
            "probe": bool(vp.get("probe")),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return task_dir


# ----------------------------------------------------------------------------
# 命令构造与执行
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 进程内小补丁：部分平台的接口把发布时间返回成"数字字符串"
# （贴吧 /mo/q/search/multsearch 的 post 时间就是 "1763033458"），MediaCrawler 的
# tools.time_util.get_time_str_from_unix_time 先 int() 判了千分位、后面却把**原值**
# 丢给 time.localtime()，于是抛 "'str' object cannot be interpreted as an integer"，
# 整个搜索直接失败。这里用 python -c 在当前子进程里把那个函数包一层：只把纯数字字符串
# 转成整数，其它一律走原实现。不改 media_platform 里的任何源码。
TIME_SHIM_SRC = (
    "import sys, runpy\n"
    "import tools.time_util as _tu\n"
    "_orig = _tu.get_time_str_from_unix_time\n"
    "def _safe(ts=None, *a, **k):\n"
    "    if isinstance(ts, str) and ts.strip().isdigit():\n"
    "        ts = int(ts.strip())\n"
    "    return _orig(ts, *a, **k)\n"
    "_tu.get_time_str_from_unix_time = _safe\n"
    "try:\n"
    "    import tools.utils as _u\n"
    "    if hasattr(_u, 'get_time_str_from_unix_time'):\n"
    "        _u.get_time_str_from_unix_time = _safe\n"
    "except Exception:\n"
    "    pass\n"
    "sys.argv = ['main.py'] + sys.argv[1:]\n"
    "runpy.run_path('main.py', run_name='__main__')\n"
)


def split_passthrough(s: str) -> List[str]:
    """按空格切分透传参数，但保住 {…}、[…]、"…" 里的空格。

    原来直接 .split()，像 --config_overrides {"CDP_DEBUG_PORT": 9224} 会被切成
    三个 token，JSON 里的空格把参数拦腰截断（typer 报 No such command '9224}'）。
    """
    if not s:
        return []
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    # 整段被引号包住的 token 去掉外层引号（subprocess 以列表传参，不需要 shell 引号）
    cleaned: List[str] = []
    for tok in out:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        cleaned.append(tok)
    return cleaned


def build_command(vp: Dict[str, Any], attempt_dir: Path) -> List[str]:
    cmd = [
        str(VENV_PY), "-c", TIME_SHIM_SRC,
        "--platform", vp["platform"],
        "--lt", "qrcode",
        "--type", vp["type"],
        "--keywords", ",".join(vp["keywords"]),
        "--crawler_max_notes_count", str(vp["count"]),
        "--save_data_option", vp["format"],
        "--save_data_path", str(attempt_dir),
        "--get_comment", "true" if vp["comments"] else "false",
        "--get_media", "true" if vp["media"] else "false",
        "--headless", "true" if vp["headless"] else "false",
    ]
    if vp["comments"]:
        cmd += ["--max_comments_count_singlenotes", str(vp["max_comments"])]
    if vp["type"] == "detail" and vp["specified_id"]:
        cmd += ["--specified_id", vp["specified_id"]]
    if vp["type"] == "creator" and vp["creator_id"]:
        cmd += ["--creator_id", vp["creator_id"]]
    if vp["passthrough"]:
        cmd += split_passthrough(vp["passthrough"])
    # 续跑用：从第几页开始抓。0 表示不传，走原码默认（START_PAGE=1）。
    # 注意原码语义：--start 不是"少抓"，而是把抓取窗口整体往后平移一页一页数，
    # 能不能真拿到新数据取决于该关键词后面还有没有页。
    sp = int(vp.get("start_page") or 0)
    if sp > 0:
        cmd += ["--start", str(sp)]
    return cmd


def classify_reason(log_text: str) -> Tuple[str, bool]:
    """(原因, 可自动重试)"""
    for reason, pattern, retryable in REASON_RULES:
        if re.search(pattern, log_text, re.IGNORECASE):
            return reason, retryable
    return "未知（详见日志尾部）", False


# ----------------------------------------------------------------------------
# 失败原因分桶：三类互不混装 + 一个「压根没开始爬」的桶
#   为什么要分：这三件事的处理办法完全不同，混在一句话里人看不出该干嘛。
#     not_logged_in 没登录/票失效   → 唯一动作是补票，不补票重跑一百次也一样
#     no_data       平台确实没内容  → 不是故障，重跑不会变出数据，该换词
#     network       网络/接口/浏览器出问题 → 等几分钟或换网络，可以重试
#     other         开跑前被参数/占锁拦下、或你自己按了 Ctrl+C → 按提示改，不用查网络
# ----------------------------------------------------------------------------
REASON_KINDS: Dict[str, Dict[str, str]] = {
    "not_logged_in": {
        "label": "没登录",
        "advice": "先补票再跑（login，或用免扫码插件 --import）；不补票重跑结果一样",
    },
    "no_data": {
        "label": "平台没内容",
        "advice": "不是故障，别重跑；换个关键词或放宽条数",
    },
    "network": {
        "label": "网络或接口出问题",
        "advice": "等几分钟重试或换网络；先 status 看有没有残留浏览器占着。"
                  "要是日志里写「风控/验证码/400002」，那是平台要人机验证：别开无头，"
                  "让它开可见窗口，你先手动过一次验证再跑",
    },
    "other": {
        "label": "不是爬取失败",
        "advice": "开跑前就被拦下或你手动停的；按上面的提示处理，不用查网络和登录",
    },
}


def reason_kind(reason: str) -> str:
    """把一段失败原因归到一个桶里。没原因（成功）返回空字符串。"""
    s = (reason or "").strip()
    if not s:
        return ""
    # 1) 压根没开始爬：参数不对、本机已有任务占着、批量文件读不了、你按了 Ctrl+C、喊停
    for kw in ("参数校验未通过", "不支持的平台", "本机已有任务在跑", "批量文件", "用户中断", "已取消"):
        if kw in s:
            return "other"
    # 2) 平台确实没内容：进程正常跑完，一条没拿到 / 拿的比要的少
    for kw in ("平台返回 0 条", "平台明确返回空", "平台实际返回"):
        if kw in s:
            return "no_data"
    # 3) 没登录：预检就报未登录、二维码没扫上、票过期、开跑前探针拿不到数据（探针失败首要是先补票）
    for kw in ("未登录", "没有登录票", "登录数据目录", "未发现 Cookies",
               "登录未完成", "没扫上码", "登录态已过期", "登录票", "探针未取到数据",
               # 快手实测：票搬进来了，但平台自检不认，爬虫转去点登录按钮后超时。
               # 这不是网络问题，归到「没登录」才有人会去重新登录/过验证。
               "登录自检未通过"):
        if kw in s:
            return "not_logged_in"
    # 4) 其余（超时/网络断开/风控拦截/浏览器起不来/进程异常退出/认不出的错）都属接口侧
    return "network"


def kind_label(kind: str) -> str:
    return REASON_KINDS.get(kind, {}).get("label", "") if kind else ""


def kind_advice(kind: str) -> str:
    return REASON_KINDS.get(kind, {}).get("advice", "") if kind else ""


def _squash(line: str, max_len: int = 200) -> str:
    """压成单行并截断，用于当证据展示"""
    s = " ".join(line.split())
    return s[:max_len] + ("…" if len(s) > max_len else "")


def _keyword_from_line(line: str) -> str:
    m = _KEYWORD_LINE_RE.search(line)
    if m:
        return m.group(1).strip().strip("'\"")
    m = re.search(r"for '([^']+)'", line)
    return m.group(1).strip() if m else ""


def detect_empty_result(log_text: str, platform: str) -> Optional[Dict[str, Any]]:
    """判断「平台返回 0 条」。

    只在调用方已经确认「进程正常退出 + 一条都没落盘」时调用。
    命中返回 {text, evidence, keyword, all_empty}，否则 None。

    注意：这是「成功执行但无数据」，不是基础设施故障，**不该自动重试**。
    """
    lines = log_text.splitlines()

    for pl, pattern, desc in EMPTY_RESULT_RULES:
        if pl != platform:
            continue
        for idx, line in enumerate(lines):
            m_hit = re.search(pattern, line, re.IGNORECASE)
            if not m_hit:
                continue
            kw = _keyword_from_line(line)
            # 空标记行本身可能不带关键词（如贴吧），往上找最近的搜索行
            if not kw:
                for back in range(idx - 1, max(-1, idx - 25), -1):
                    kw = _keyword_from_line(lines[back])
                    if kw:
                        break
            evidence = _squash(line)
            # 证据行前面是时间戳前缀，截断后常常正好把空标记切掉（"page: 1 is e…"），
            # 那这条证据就等于没说清。命中标记不在证据里就补上去。
            mark = " ".join(m_hit.group(0).split())
            if mark and mark.lower() not in evidence.lower():
                evidence += f" …[空标记:{mark}]"
            # 本次涉及的每个关键词是不是都空了？全空更像票降级/风控，部分空才是词本身没内容
            searched = {_keyword_from_line(ln) for ln in lines
                        if re.search(r"keyword[:：]", ln) and _keyword_from_line(ln)}
            empty_kws = {_keyword_from_line(ln) for ln in lines
                         if re.search(pattern, ln, re.IGNORECASE) and _keyword_from_line(ln)}
            if kw:
                empty_kws.add(kw)
            all_empty = bool(searched) and empty_kws >= searched
            text = f"平台返回 0 条（{desc}" + (f"，关键词「{kw}」" if kw else "") + "）"
            if all_empty:
                text += (f"；{len(searched)} 个关键词全部为空 → 更像是登录票降级或被风控限流，"
                         f"建议先 login 或换网络再试")
            text += f"；证据日志：{evidence}"
            return {"text": text, "evidence": evidence, "keyword": kw,
                    "all_empty": all_empty}

    # 弱证据（微博）：日志里有搜索行，但平台对空结果不给任何标记
    weak = WEAK_EMPTY_PLATFORMS.get(platform)
    if weak:
        m = re.search(weak + r"\s*(.+?),\s*page", log_text)
        if m:
            kw = m.group(1).strip()
            evidence = _squash(m.group(0))
            return {"text": (f"平台返回 0 条（微博搜索接口未返回任何内容，"
                             f"该平台对空结果不打印标记，按「搜索跑过但仍 0 条」推断"
                             + (f"，关键词「{kw}」" if kw else "") + f"）；证据日志：{evidence}"),
                    "evidence": evidence, "keyword": kw, "all_empty": True}
    return None


def tail(text: str, n: int = 15) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def run_attempt(vp: Dict[str, Any], task_dir: Path, attempt: int
                ) -> Dict[str, Any]:
    """执行一次爬取尝试。返回 {exit_code, timed_out, log_text, duration, interrupted}"""
    attempt_dir = task_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / f"run_attempt_{attempt}.log"
    cmd = build_command(vp, attempt_dir)

    print(f"\n{SEP}")
    print(f"▶ 第 {attempt} 次尝试  {now_str()}")
    shown = " ".join(cmd)
    print(f"  命令: {shown[:220]}{' …' if len(shown) > 220 else ''}")
    print(SEP)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUNBUFFERED", "1")

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # 使 CTRL_BREAK 可送达
    )

    log_lines: List[str] = []
    q: "queue.Queue[Optional[str]]" = queue.Queue()
    state = {"qr_prompted": False}

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()

    deadline = time.time() + vp["timeout_min"] * 60
    start = time.time()
    timed_out = False
    canceled = False
    interrupted = False
    stop_mode: Optional[bool] = None  # 收尾方式：True=优雅退出 / False=强杀
    qr_line = re.compile(r"Waiting for scan code login", re.IGNORECASE)

    with open(log_path, "w", encoding="utf-8") as lf:
        while True:
            # 喊停优先于超时：有人递了纸条，就按优雅流程收（数据保留、浏览器关掉、牌子摘掉）
            if cancel_targets_me():
                canceled = True
                stop_mode = graceful_stop_crawler(proc)
                print("\n⛔ 收到取消请求：" + ("已发送停止信号，进程退出（已落盘的数据保留）"
                                              if stop_mode else "进程 20 秒未响应，已强杀进程树"))
                break
            if time.time() > deadline:  # 每轮必查：日志再密集也能准点触发，最坏晚1秒
                timed_out = True
                stop_mode = graceful_stop_crawler(proc)
                print("\n⏱ 超时：" + ("已发送停止信号，进程退出（jsonl 逐行落盘故数据完好；浏览器若残留由安全网清理）"
                                     if stop_mode else "进程 20 秒未响应停止信号，已强杀进程树（浏览器将单独清理）"))
                break
            try:
                item = q.get(timeout=CANCEL_POLL_SEC)
            except queue.Empty:
                continue
            if item is None:
                break
            line = item.rstrip("\n")
            log_lines.append(line)
            try:
                lf.write(line + "\n")
                lf.flush()
            except Exception:
                pass
            print("  | " + line)  # 实时转发，绝不假进度
            if qr_line.search(line) and not state["qr_prompted"]:
                state["qr_prompted"] = True
                print("\n" + "!" * 64)
                print("!! 需要人工操作：请在弹出的浏览器窗口中用手机 App 扫码登录")
                print(f"!! 登录窗口最长约 10 分钟（内部 600 秒轮询），扫完码会自动继续")
                print("!" * 64 + "\n")

    exit_code: Optional[int] = None
    if not timed_out and not interrupted and not canceled:
        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            exit_code = proc.wait(timeout=30)
    else:
        try:
            exit_code = proc.wait(timeout=30)
        except Exception:
            exit_code = -1

    duration = time.time() - start

    # 安全网：进程结束后若浏览器残留，优雅关闭
    leftover = ps_find_crawler_browsers(vp["platform"])
    for pid, _cmd in leftover:
        print(f"  ↳ 检测到爬虫浏览器残留 PID {pid}，优雅关闭…")
        graceful_close_browser(pid)

    return {
        "exit_code": exit_code, "timed_out": timed_out, "interrupted": interrupted,
        "canceled": canceled, "stop_mode": stop_mode,
        "log_text": "\n".join(log_lines), "duration": duration,
        "attempt_dir": str(attempt_dir), "qr_prompted": state["qr_prompted"],
        "log_path": str(log_path),
    }


# ----------------------------------------------------------------------------
# 结果对账与抽查
# ----------------------------------------------------------------------------
def classify_file(p: Path) -> str:
    n = p.name.lower()
    if n.endswith(".xlsx"):
        return "excel"
    if "_contents_" in n:
        return "contents"
    if "_comments_" in n:
        return "comments"
    if "_creators_" in n or "_profiles_" in n or "_dynamics_" in n or "_contacts_" in n:
        return "creators"
    return "other"


def count_records(p: Path) -> int:
    """按格式统计记录数。"""
    try:
        suffix = p.suffix.lower()
        if suffix == ".jsonl":
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return sum(1 for ln in f if ln.strip())
        if suffix == ".json":
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return sum(len(v) for v in data.values() if isinstance(v, list)) or 1
            return 1
        if suffix == ".csv":
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                rows = list(csv.reader(f))
            return max(0, len(rows) - 1)  # 减表头
    except Exception:
        return -1
    return -1


def count_excel(p: Path) -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True)
        for ws in wb.worksheets:
            n = 0
            for i, _row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                if any(c is not None and str(c).strip() for c in _row):
                    n += 1
            out[ws.title.lower()] = n
        wb.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def collect_results(attempt_dir: Path) -> Dict[str, Any]:
    """扫描 attempt 目录，汇总各文件与记录数。"""
    files: List[Dict[str, Any]] = []
    for p in sorted(attempt_dir.rglob("*")):
        if not p.is_file():
            continue
        kind = classify_file(p)
        entry: Dict[str, Any] = {"path": str(p), "name": p.name, "kind": kind,
                                 "size": p.stat().st_size}
        if kind == "excel":
            entry["sheets"] = count_excel(p)
        else:
            entry["records"] = count_records(p)
        files.append(entry)
    # 同一批数据可能同时落 jsonl/csv 与 excel（excel 由 jsonl 导出，属同一数据集的
    # 不同视图）：按格式分组取最大值，避免双重计数；count_records 失败(-1)按0处理。
    def _fmt_of(f: Dict[str, Any]) -> str:
        return Path(f["name"]).suffix.lower().lstrip(".")

    def _bucket_count(bucket: str) -> int:
        by_fmt: Dict[str, int] = {}
        for f in files:
            if f["kind"] == bucket:
                n = max(0, f.get("records") or 0)
                by_fmt[_fmt_of(f)] = by_fmt.get(_fmt_of(f), 0) + n
            elif f["kind"] == "excel":
                n = max(0, (f.get("sheets") or {}).get(bucket) or 0)
                if n:
                    by_fmt["excel"] = by_fmt.get("excel", 0) + n
        return max(by_fmt.values(), default=0)

    contents = _bucket_count("contents")
    comments = _bucket_count("comments")
    creators = _bucket_count("creators")
    # 主成果文件优先 excel（人读最友好），否则第一个内容文件
    primary = (next((f for f in files if f["kind"] == "excel"), None)
               or next((f for f in files if f["kind"] == "contents"), None))
    return {"files": files, "contents": contents, "comments": comments,
            "creators": creators, "primary": primary}


def load_sample_records(attempt_dir: Path, k: int = 3) -> List[Dict[str, Any]]:
    """从内容文件随机抽取 k 条记录（jsonl/json/csv/excel 通吃）。"""
    records: List[Dict[str, Any]] = []
    try:
        for p in sorted(attempt_dir.rglob("*")):
            if not p.is_file() or classify_file(p) not in ("contents", "excel"):
                continue
            suffix = p.suffix.lower()
            if suffix == ".jsonl":
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln:
                            try:
                                records.append(json.loads(ln))
                            except Exception:
                                pass
            elif suffix == ".json":
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    records.extend(x for x in data if isinstance(x, dict))
            elif suffix == ".csv":
                with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                    records.extend(dict(r) for r in csv.DictReader(f) if r)
            elif suffix == ".xlsx":
                from openpyxl import load_workbook
                wb = load_workbook(p, read_only=True)
                if "Contents" in wb.sheetnames:
                    ws = wb["Contents"]
                    rows = list(ws.iter_rows(values_only=True))
                    if rows:
                        header = [str(h) for h in rows[0]]
                        records.extend(dict(zip(header, r)) for r in rows[1:] if r)
                wb.close()
    except Exception:
        pass
    if not records:
        return []
    random.seed(42)
    return random.sample(records, min(k, len(records)))


def spot_check(attempt_dir: Path) -> Dict[str, Any]:
    """完整性抽查：标题/内容非空、作者字段存在（脱敏视为正常）、链接字段合法。"""
    TITLE_FIELDS = ["title", "note_title", "desc_title", "content_title", "name"]
    # 贴吧叫 user_nickname、知乎 creator_nickname、小红书 nickname……各平台字段名不同，
    # 漏一个就会把"作者有值"误报成"作者=无"（2026-11-13 贴吧验收踩过）。
    AUTHOR_FIELDS = ["nickname", "user_nickname", "author", "user_name", "nickname_masked",
                     "up_name", "creator_nickname", "creator_name", "user_nick_name"]
    LINK_FIELDS = ["link", "url", "video_url", "note_url", "content_url", "source_url",
                   "video_url_real", "web_url"]
    samples = load_sample_records(attempt_dir)
    if not samples:
        return {"ok": False, "detail": "未能读取到任何记录，无法抽查", "samples": []}
    details: List[str] = []
    all_ok = True
    for i, rec in enumerate(samples, 1):
        title = next((str(rec.get(f, "")).strip() for f in TITLE_FIELDS
                      if str(rec.get(f, "")).strip()), "")
        author = next((str(rec.get(f, "")).strip() for f in AUTHOR_FIELDS
                       if str(rec.get(f, "")).strip()), "")
        content = str(rec.get("content", rec.get("desc", ""))).strip()
        link_val = next((str(rec.get(f, "")).strip() for f in LINK_FIELDS
                         if str(rec.get(f, "")).strip()), "")
        checks = []
        checks.append(("标题或内容", bool(title or content)))
        checks.append(("作者字段", bool(author)))  # 脱敏值（如 *星人）也视为通过
        if link_val:
            checks.append(("链接", link_val.startswith("http")))
        bad = [n for n, ok in checks if not ok]
        if bad:
            all_ok = False
        shown = (title or content)[:30]
        details.append(f"样本{i}: 「{shown}…」 作者={author or '无'} "
                       f"{'✓' if not bad else '✗ ' + ','.join(bad)}")
    return {"ok": all_ok, "detail": "；".join(details), "samples": details}


# ----------------------------------------------------------------------------
# export：把已经落在磁盘上的任务目录，转成一份能直接交给人的 Excel
# ----------------------------------------------------------------------------
# 口径（改之前先读，别把「只搬不改」的承诺弄丢）：
# 1) 只搬不改：不排序、不去重、不改写正文、不加统计列。原值原样搬。
# 2) 只加三样"说明性"的东西，三样都写进「口径说明」页里：
#    - 末尾一列「来源文件」：多个 jsonl 拼在一起时，才知道这一行是哪来的；
#    - 列表/字典字段（话题标签、图片列表等）序列化成 JSON 文本（Excel 装不下对象）；
#    - 超长正文截断到 Excel 单元格上限以内（32767 字符），截断条数会写在口径页。
# 3) 长数字列（ID / 哈希 / 原始时间戳）一律按**文本**写。否则 Excel 把它显示成
#    1.7896E+18，用户在表里看到的 ID 是错的，复制出来也对不回原数据。
EXPORT_DIR = HOME / "exports"
# 哪些列必须按文本存：*_id / id、含 hash、*_ts、*time（原始时间戳）
TEXT_COL_RE = re.compile(r"(^|_)(id)$|hash|_ts$|time$|uuid", re.I)
EXCEL_CELL_MAX = 32767         # Excel 单元格硬上限
EXCEL_CELL_KEEP = 32000        # 截断时保留的长度，留点余量
KIND_CN = {"contents": "内容", "comments": "评论", "creators": "创作者"}
PLATFORM_CN = {"dy": "抖音", "bili": "B站", "ks": "快手", "wb": "微博",
               "xhs": "小红书", "tieba": "贴吧", "zhihu": "知乎"}
DATA_SUFFIXES = {".jsonl", ".json", ".csv", ".xlsx"}
# 上游顺手产出的派生件（文件名里也带 _comments_），不能当数据源
SKIP_NAME_MARK = ("_word_cloud", "_word_freq", "_freq", "_wordcloud")

# 三项自查要看的关键列（按"意思"认列，不查平台表——七個平台叫法不同，认意思更稳）
KEY_COL_RULES: List[Tuple[str, str]] = [
    ("ID", r"(_id|^id)$"),
    ("正文", r"^(desc|content|content_text|text)$"),
    ("作者昵称", r"^(nickname|user_nickname)$"),
    ("发布时间", r"^(create_date_time|publish_time|create_time|created_time|time)$"),
]
COUNT_COL_RE = re.compile(r"(_count|_num|viewd_count)$", re.I)
_TIME_STR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")
# Excel 的 XML 装不下这些控制字符，openpyxl 会直接报 IllegalCharacterError 拒收整个单元格。
# 和 openpyxl 内部用的正则完全一致（\t \n \r 是留着的，正常换行不受影响）。
# 去掉它们属于"改内容"，所以条数必须写进口径页，并且原值在源 jsonl 里仍然完好。
ILLEGAL_CHAR_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def resolve_task_dir(task: str, st: Dict[str, Any]) -> Tuple[Optional[Path], List[str]]:
    """--task 认三种写法：绝对/相对路径、tasks\\ 下的目录名（可只给片段）、state.json 里历史的 task_dir。

    返回 (找到的目录, 找过的地方)。找不到时把"找过的地方"全打出来，别静默失败——
    用户拿错任务名时最想知道的是"你到底在哪找过"。
    """
    tried: List[str] = []
    raw = (task or "").strip().strip('"').strip("'")
    if not raw:
        return None, ["--task 是空的"]
    p = Path(raw)
    if p.is_dir():
        return p.resolve(), [f"路径 {p.resolve()}"]
    tried.append(f"路径 {p}（不存在）")
    exact = TASKS_ROOT / raw
    if exact.is_dir():
        return exact.resolve(), tried
    tried.append(f"{exact}（不存在）")
    if TASKS_ROOT.is_dir():
        hits = sorted(d for d in TASKS_ROOT.iterdir() if d.is_dir() and raw in d.name)
        if len(hits) == 1:
            tried.append(f"tasks 下名字含「{raw}」的目录 → 唯一命中 {hits[0].name}")
            return hits[0].resolve(), tried
        if len(hits) > 1:
            tried.append(f"tasks 下名字含「{raw}」的目录有 {len(hits)} 个，没法替你选："
                         + "、".join(h.name for h in hits[:6]))
        else:
            tried.append(f"tasks 下没有名字含「{raw}」的目录")
    for h in (st.get("history") or []):
        td = str(h.get("task_dir") or "")
        if not td:
            continue
        if td == raw or Path(td).name == raw or raw in Path(td).name:
            if Path(td).is_dir():
                tried.append(f"state.json 历史 → {td}")
                return Path(td), tried
    tried.append("state.json 历史里的 task_dir（没匹配上）")
    return None, tried


def pick_attempt(task_dir: Path, want: Optional[int] = None) -> Optional[Path]:
    """挑一次尝试目录：--attempt 指定就用它，否则取编号最大、真落了数据的那一次。

    （重试过的任务 attempt_N 有多个；最新那次可能还在跑或刚失败，所以优先取"有数据"的。）
    """
    if want:
        d = task_dir / f"attempt_{want}"
        return d if d.is_dir() else None
    cands = [d for d in task_dir.glob("attempt_*") if d.is_dir()]
    cands.sort(key=lambda d: int(re.sub(r"\D", "", d.name) or 0), reverse=True)
    for d in cands:
        if _kind_sources(d, "contents") or _kind_sources(d, "comments"):
            return d
    return cands[0] if cands else None


def _kind_sources(attempt_dir: Path, kind: str) -> List[Path]:
    """找一个 attempt 目录里属于某类（contents/comments）的所有数据文件。

    优先按文件名认（*_contents_*.jsonl 这种）；如果任务是用 -f excel 跑的（只有 xlsx），
    再看 xlsx 里有没有同名 sheet。两种都认，是因为两种格式都可能出现在历史任务里。

    注意排除上游顺手生成的派生件：词云 png、词频 json（文件名里也带 _comments_，
    不排除就会被当成评论灌进表里，凭空多出「词/频次」两列）。
    """
    out = []
    for p in sorted(attempt_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in DATA_SUFFIXES:
            continue
        low = p.name.lower()
        if any(mark in low for mark in SKIP_NAME_MARK):
            continue
        if classify_file(p) == kind:
            out.append(p)
    if out:
        return out
    for p in sorted(attempt_dir.rglob("*.xlsx")):
        sheets = count_excel(p)
        if sheets.get(kind):
            out.append(p)
    return out


def read_data_rows(p: Path, kind: str) -> List[Dict[str, Any]]:
    """把一个数据文件读成行列表，**保持原行序**。jsonl / json / csv / xlsx 都认。"""
    suffix = p.suffix.lower()
    rows: List[Dict[str, Any]] = []
    try:
        if suffix == ".jsonl":
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                    except Exception:
                        # 一行坏掉不能拖死整张表：原样留个痕，口径页对账时会露出来
                        rows.append({"__解析失败的行__": ln[:EXCEL_CELL_KEEP]})
                        continue
                    rows.append(obj if isinstance(obj, dict) else {"value": obj})
            return rows
        if suffix == ".json":
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [r if isinstance(r, dict) else {"value": r} for r in data]
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        rows.extend(r if isinstance(r, dict) else {"value": r} for r in v)
            return rows
        if suffix == ".csv":
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                return [dict(r) for r in csv.DictReader(f)]
        if suffix == ".xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(p, read_only=True, data_only=True)
            ws = next((w for w in wb.worksheets if w.title.lower().startswith(kind[:6])), None)
            ws = ws or (wb.worksheets[0] if wb.worksheets else None)
            if ws is None:
                wb.close()
                return []
            it = ws.iter_rows(values_only=True)
            header = next(it, None)
            if not header:
                wb.close()
                return []
            keys = [str(h) if h is not None else f"col{i + 1}" for i, h in enumerate(header)]
            for row in it:
                if not any(c is not None and str(c).strip() for c in row):
                    continue
                rows.append({k: v for k, v in zip(keys, row)})
            wb.close()
            return rows
    except Exception as e:
        # 读不动就把这条文件当 0 行，但必须让调用方知道原因（写进口径页）
        rows = []
        setattr(p, "_read_error", str(e))
    return rows


def _cell_value(col: str, v: Any, stat: Dict[str, int]) -> Any:
    """原值 → 单元格值。只做"塞得进单元格"的转换，不改内容。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (list, dict)):
        stat["列表/字典转JSON文本"] = stat.get("列表/字典转JSON文本", 0) + 1
        s = json.dumps(v, ensure_ascii=False)
    elif isinstance(v, (int, float)):
        if TEXT_COL_RE.search(col):
            stat["长数字按文本存"] = stat.get("长数字按文本存", 0) + 1
            return str(v)
        return v
    else:
        s = str(v)
    if ILLEGAL_CHAR_RE.search(s):
        # Excel 装不下的控制字符（多为 emoji 被截断后留下的残渣），去掉并计数申报
        n = len(ILLEGAL_CHAR_RE.findall(s))
        stat["控制字符去掉"] = stat.get("控制字符去掉", 0) + n
        s = ILLEGAL_CHAR_RE.sub("", s)
    if len(s) > EXCEL_CELL_MAX:
        stat["超长截断"] = stat.get("超长截断", 0) + 1
        s = s[:EXCEL_CELL_KEEP]
    return s


def build_kind_table(attempt_dir: Path, kind: str) -> Optional[Dict[str, Any]]:
    """把某一类（contents/comments）的所有源文件拼成一张表：列 = 各文件出现过的键的并集。"""
    sources = _kind_sources(attempt_dir, kind)
    if not sources:
        return None
    rows: List[Dict[str, Any]] = []
    src_info: List[Dict[str, Any]] = []
    stat: Dict[str, int] = {}
    src_col = "来源文件"
    for p in sources:
        got = read_data_rows(p, kind)
        if got and any(src_col in r for r in got):
            src_col = "来源文件_派生"      # 数据本身就有这个列名，就让开，别覆盖人家
        for r in got:
            r[src_col] = p.name
        rows.extend(got)
        err = getattr(p, "_read_error", "")
        src_info.append({"path": str(p), "name": p.name, "rows": len(got),
                         "counted": count_records(p), "error": err})
    cols: List[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return {"kind": kind, "rows": rows, "cols": cols, "sources": src_info,
            "cells": stat, "src_col": src_col}


def _col_by_rule(cols: List[str], rule: str) -> Optional[str]:
    rx = re.compile(rule, re.I)
    for c in cols:
        if rx.search(c):
            return c
    return None


def _norm_ts(v: Any) -> Tuple[Optional[datetime], str]:
    """把一个时间值解释成 (datetime, 形态说明)。认不出来就 (None, '认不出')。

    口诀：13 位是毫秒、10 位是秒、带冒号的本来就是可读时间。按**值的形态**判断，
    不看平台表——同一平台不同列的写法也会变，按值判断不会错。
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, "空"
    if isinstance(v, bool):
        return None, "非法值"
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e11:
            return datetime.fromtimestamp(n / 1000), "13位毫秒"
        if n > 1e8:
            return datetime.fromtimestamp(n), "10位秒"
        return None, "非法值"
    s = str(v).strip()
    if s.isdigit():
        return _norm_ts(int(s))
    if _TIME_STR_RE.match(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")), "可读字符串"
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19], fmt), "可读字符串"
                except Exception:
                    continue
        return None, "认不出"
    return None, "认不出"


def self_check_table(tbl: Dict[str, Any]) -> Dict[str, Any]:
    """三项自查：①关键列空值 ②时间/计数字段常识 ③行数与源文件的核对。"""
    rows, cols = tbl["rows"], tbl["cols"]
    kind_cn = KIND_CN.get(tbl["kind"], tbl["kind"])
    lines: List[str] = []
    facts: Dict[str, str] = {}

    # ① 关键列空值（上游字段缺失会一路传导到这里，是"数据看起来齐了其实缺列"的照妖镜）
    empties: List[str] = []
    for label, rule in KEY_COL_RULES:
        col = _col_by_rule(cols, rule)
        if not col:
            empties.append(f"{label} 列不存在")
            continue
        n = sum(1 for r in rows if r.get(col) is None or str(r.get(col)).strip() in ("", "None", "[]", "{}"))
        empties.append(f"{label}({col}) 空 {n}/{len(rows)}")
    lines.append(f"① 关键列空值：{'；'.join(empties)}")

    # ② 时间字段够不够像人话（1970 / 未来时间 / 认不出的值）+ 计数字段有没有负数
    tcol = _col_by_rule(cols, KEY_COL_RULES[3][1])
    if tcol:
        shapes: Dict[str, int] = {}
        n_epoch = n_future = n_bad = 0
        now = datetime.now()
        for r in rows:
            dt, shape = _norm_ts(r.get(tcol))
            shapes[shape] = shapes.get(shape, 0) + 1
            if dt is None:
                if shape in ("非法值", "认不出"):
                    n_bad += 1
                continue
            if dt.year <= 1971:
                n_epoch += 1
            elif dt > now + timedelta(days=1):
                n_future += 1
        shape_txt = " / ".join(f"{k} {v}" for k, v in sorted(shapes.items()))
        lines.append(f"② 时间({tcol})：{shape_txt}；1970 共 {n_epoch}，未来时间 {n_future}，认不出 {n_bad}")
        # 交接文档要求口径页写死"时间列是秒还是毫秒"——按值的形态说，不靠平台表推断
        main_shape = max((k for k in shapes if k not in ("空", "非法值", "认不出")),
                         key=lambda k: shapes[k], default="")
        unit = {"13位毫秒": "毫秒（13 位）", "10位秒": "秒（10 位）",
                "可读字符串": "可读时间字符串（不是数字）"}.get(main_shape, "见上面②")
        facts["发布时间列单位"] = f"{tcol} = {unit}"
    else:
        lines.append("② 时间：这张表没有发布时间列")
        facts["发布时间列单位"] = "无发布时间列"
    negs: List[str] = []
    for c in cols:
        if not COUNT_COL_RE.search(c):
            continue
        bad = 0
        for r in rows:
            v = r.get(c)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0:
                bad += 1
        if bad:
            negs.append(f"{c} 负数 {bad}")
    lines.append("② 计数字段：" + ("、".join(negs) if negs else "没有负数"))

    # 采集时段：用 last_modify_ts（写库时刻）的最早/最晚，这才是"这批数据是什么时候采的"。
    # 注意它是采集时间，不是发布时间——口径页上分开写，免得两边混用。
    ts_col = _col_by_rule(cols, r"_ts$")
    if ts_col:
        dts = [dt for dt, _ in (_norm_ts(r.get(ts_col)) for r in rows) if dt]
        if dts:
            facts["采集时段"] = (f"{min(dts):%Y-%m-%d %H:%M:%S} ~ {max(dts):%Y-%m-%d %H:%M:%S}"
                                 f"（{ts_col}，写库时刻，不是发布时间）")

    # ③ 行数对账：写进去的行 vs 源文件按行数出来的记录数，差多少必须说清
    src_txt = []
    diff_total = 0
    for s in tbl["sources"]:
        d = s["counted"] - s["rows"] if s["counted"] >= 0 else 0
        diff_total += max(0, d)
        src_txt.append(f"{s['name']} 写入 {s['rows']} 行 / 文件记录 {s['counted']}"
                       + (f"（差 {d}）" if d else "")
                       + (f"⚠读取失败：{s['error']}" if s["error"] else ""))
    verdict = "一致" if diff_total == 0 else f"共差 {diff_total} 行（空行、坏行或读不出的行，见「__解析失败的行__」列）"
    lines.append(f"③ 行数对账：{kind_cn}表共 {len(rows)} 行；" + "；".join(src_txt) + f" → {verdict}")
    return {"lines": lines, "diff": diff_total, "facts": facts}


# ----------------------------------------------------------------------------
# 「统一格式」页：给 AI / 下游程序用的规范表
# 为什么加：七个平台的列名五花八门（note_id / video_id / aweme_id / content_id…），
#   同一个意思在不同平台叫不同名。AI 每接一个新平台就得重新认列名，没法直接用。
# 怎么加：**在导出的 Excel 里多加一页**，原始数据页一个字不动（只搬不改的承诺照旧）。
#   统一页是把原表按"意思"搬到固定列名下的一层副本，对不上就留空，绝不猜。
# 依据：列名来自 tasks\ 里七个平台真跑出来的数据；小红书/知乎本机没有真实产出，
#   按原码写入字段取（口径页会标出这两家是"按代码推断，未经真实数据核对"）。
# ----------------------------------------------------------------------------
UNIFIED_CONTENTS: List[Tuple[str, List[str], str]] = [
    ("内容ID", ["note_id", "content_id", "video_id", "aweme_id"], "id"),
    ("标题", ["title"], "text"),
    ("正文", ["content_text", "content", "desc"], "text"),
    ("作者昵称", ["nickname", "user_nickname"], "text"),
    ("发布时间", ["create_time", "created_time", "publish_time", "time"], "time"),
    ("点赞数", ["liked_count", "like_count", "voteup_count"], "int"),
    ("评论数", ["comment_count", "comments_count", "video_comment",
                "total_replay_num", "sub_comment_count"], "int"),
    ("链接", ["note_url", "aweme_url", "content_url", "video_url"], "text"),
    ("关键词", ["source_keyword"], "text"),
]

UNIFIED_COMMENTS: List[Tuple[str, List[str], str]] = [
    ("评论ID", ["comment_id"], "id"),
    ("所属内容ID", ["note_id", "video_id", "aweme_id", "content_id"], "id"),
    ("父评论ID", ["parent_comment_id"], "id"),
    ("评论正文", ["content"], "text"),
    ("评论者昵称", ["nickname", "user_nickname"], "text"),
    ("评论时间", ["create_time", "publish_time"], "time"),
    ("点赞数", ["like_count", "liked_count"], "int"),
    ("子评论数", ["sub_comment_count"], "int"),
    ("IP属地", ["ip_location"], "text"),
]

# 本机真跑过、列名对着真实表头核对过的「平台+表」组合。
# 只核对过内容表的平台不能算整家都核对过——评论表的列名可能对不上。
# 不在这份名单里的（xhs 内容/评论表、ks 评论表），口径页要明说是按源码写入字段推断。
VERIFIED_CONTENT = {"dy", "wb", "bili", "tieba", "ks", "zhihu"}
VERIFIED_COMMENT = {"dy", "wb", "bili", "tieba", "zhihu"}


def verified_kinds(platform: str) -> set:
    """这家平台哪些表真的核对过。"""
    ok = set()
    if platform in VERIFIED_CONTENT:
        ok.add("contents")
    if platform in VERIFIED_COMMENT:
        ok.add("comments")
    return ok


def _pick_col(cols: List[str], names: List[str]) -> str:
    for n in names:
        if n in cols:
            return n
    return ""


def unified_sheet_name(kind: str) -> str:
    return "统一格式" if kind == "contents" else f"统一格式_{KIND_CN.get(kind, kind)}"


def build_unified_table(tbl: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一张原始表按固定列名搬一份。对不上的留空并计数，绝不猜。"""
    rows = tbl["rows"]
    if not rows:
        return None
    kind = tbl["kind"]
    spec = UNIFIED_CONTENTS if kind == "contents" else UNIFIED_COMMENTS
    src_cols = tbl["cols"]

    mapping: List[Tuple[str, str, str]] = []
    for name, cands, typ in spec:
        mapping.append((name, _pick_col(src_cols, cands), typ))
    used = {m[1] for m in mapping if m[1]}
    residual = [c for c in src_cols if c not in used and c != tbl.get("src_col")]

    stat: Dict[str, int] = {}
    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        o: Dict[str, Any] = {"数据类型": KIND_CN.get(kind, kind)}
        for name, col, typ in mapping:
            v = r.get(col) if col else ""
            if typ == "time":
                dt, shape = _norm_ts(v)
                if dt is None and shape in ("非法值", "认不出"):
                    stat["时间认不出留空"] = stat.get("时间认不出留空", 0) + 1
                    v = ""
                elif dt is None:
                    v = ""
                else:
                    v = f"{dt:%Y-%m-%d %H:%M:%S}"
            elif typ == "int":
                if v is None or str(v).strip() == "":
                    v = ""
                else:
                    try:
                        v = int(float(str(v).strip()))
                    except Exception:
                        stat["数字读不出留空"] = stat.get("数字读不出留空", 0) + 1
                        v = ""
            elif typ == "id":
                v = "" if v is None else str(v)
            o[name] = v
        o["来源文件"] = r.get(tbl.get("src_col") or "来源文件", "")
        out_rows.append(o)

    cols = ["数据类型"] + [m[0] for m in mapping] + ["来源文件"]
    unmapped = [m[0] for m in mapping if not m[1]]
    return {"kind": kind, "sheet": unified_sheet_name(kind), "cols": cols,
            "rows": out_rows, "mapping": mapping, "residual": residual,
            "unmapped": unmapped, "cells": stat, "src_rows": len(rows)}


def write_export_xlsx(task_dir: Path, attempt_dir: Path, tables: List[Dict[str, Any]],
                      out_path: Path, meta: Dict[str, str],
                      kinds: Optional[List[str]] = None,
                      unified: Optional[List[Dict[str, Any]]] = None,
                      platform: str = "") -> None:
    """落 xlsx：口径说明页 + 每类一张数据页（首行冻结、自动筛选、长数字列按文本）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "口径说明"
    bold = Font(bold=True)

    def fill_sheet(title: str, cols: List[str], rows: List[Dict[str, Any]],
                   cells: Optional[Dict[str, int]] = None,
                   id_cols: Optional[set] = None) -> str:
        if title in wb.sheetnames:      # 撞名就让开，别报错
            title = f"{title}_2"
        ws = wb.create_sheet(title)
        for ci, col in enumerate(cols, 1):
            c = ws.cell(row=1, column=ci, value=col)
            c.font = bold
            c.alignment = Alignment(vertical="center")
        text_cols = [ci for ci, col in enumerate(cols, 1) if TEXT_COL_RE.search(col)]
        for ri, r in enumerate(rows, 2):
            for ci, col in enumerate(cols, 1):
                v = _cell_value(col, r.get(col), cells if cells is not None else {})
                cell = ws.cell(row=ri, column=ci, value=v)
                if ci in text_cols or (id_cols and col in id_cols):
                    cell.number_format = "@"       # 双保险：单元格也标成文本格式
        last = f"{get_column_letter(max(1, len(cols)))}{max(1, len(rows) + 1)}"
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{last}"
        # 列宽：只按前 60 行估，长文本列固定一个能看的宽度（不是数据的一部分）
        for ci, col in enumerate(cols, 1):
            want = 12
            if ci in text_cols or col in ("作者昵称", "评论者昵称", "来源文件", "链接"):
                want = 22
            for r in rows[:60]:
                s = str(r.get(col) or "")
                want = max(want, min(50, len(s) + 2))
            ws.column_dimensions[get_column_letter(ci)].width = min(50, want)
        return title

    for tbl in tables:
        title = KIND_CN.get(tbl["kind"], tbl["kind"])
        tbl["sheet"] = fill_sheet(title, tbl["cols"], tbl["rows"], tbl["cells"])

    # 「统一格式」页：放在原始数据页后面（人先看数据，AI 直接点名这页）
    for u in (unified or []):
        id_cols = {c for c, t in zip(u["cols"], [""] + [m[2] for m in u["mapping"]] + [""]) if t == "id"}
        u["sheet"] = fill_sheet(u["sheet"], u["cols"], u["rows"], u["cells"], id_cols)

    # 口径说明页放在第一个：人打开表先看到"这表是什么、缺什么、能不能信"
    lines: List[Tuple[str, str]] = [("项目", "内容")]
    for k, v in meta.items():
        lines.append((k, v))
    lines.append(("—— 数据来源 ——", ""))
    for tbl in tables:
        lines.append((f"{KIND_CN.get(tbl['kind'], tbl['kind'])}表 sheet 名", tbl.get("sheet", "")))
        lines.append((f"{KIND_CN.get(tbl['kind'], tbl['kind'])}表行数", str(len(tbl["rows"]))))
        lines.append((f"{KIND_CN.get(tbl['kind'], tbl['kind'])}表列数", str(len(tbl["cols"]))))
        for fk, fv in (tbl.get("check") or {}).get("facts", {}).items():
            lines.append((f"{KIND_CN.get(tbl['kind'], tbl['kind'])}表{fk}", fv))
        for s in tbl["sources"]:
            lines.append((f"　源文件 {s['name']}", f"{s['rows']} 行（{s['path']}）"))
    have = {t["kind"] for t in tables}
    missing = [KIND_CN[k] for k in (kinds or [t["kind"] for t in tables]) if k not in have]
    lines.append(("已知缺失", ("未采集：" + "、".join(missing)
                             + "（这次任务磁盘上没有这类数据，不是导出漏了）") if missing
                            else "无（要的几类都有数据）"))
    lines.append(("—— 口径 ——", ""))
    lines.append(("原值承诺", "只搬不改：没排序、没去重、没改写正文、没加统计列。原值都在。"))
    lines.append(("派生列", "末列「来源文件」是本次导出加的，用来说明每行来自哪个源文件；"
                            "列表/字典字段（话题标签、图片列表等）按 JSON 文本原样存；"
                            f"超过 {EXCEL_CELL_MAX} 字符的正文被截断到 {EXCEL_CELL_KEEP} 字符；"
                            "Excel 存不了的控制字符（emoji 残缺残留）被去掉——"
                            "这几样都按条数写在下面「转换统计」里，源 jsonl 里原值完好。"))
    lines.append(("长数字列", "ID / 哈希 / 原始时间戳按文本存（单元格格式 @），"
                              "所以不会变成 1.7896E+18，但也不能直接参与数值计算。"))
    lines.append(("时间列怎么读", "13 位数字=毫秒、10 位数字=秒、带冒号的本来就是可读时间。"
                                  "发布时间≠采集时间：last_modify_ts 是写库时刻，不要当发布时间用。"))
    for tbl in tables:
        st = tbl["cells"]
        if st:
            lines.append((f"{KIND_CN.get(tbl['kind'], tbl['kind'])}表转换统计",
                          "；".join(f"{k} {v} 处" for k, v in st.items())))
    lines.append(("—— 三项自查 ——", ""))
    for tbl in tables:
        chk = tbl.get("check") or {"lines": []}
        for ln in chk["lines"]:
            lines.append((KIND_CN.get(tbl["kind"], tbl["kind"]), ln))

    # 「统一格式」页的说明：固定列名是什么、哪些对不上留了空、原始页仍是权威
    if unified:
        lines.append(("—— 统一格式页 ——", ""))
        lines.append(("这是什么", "给 AI/程序用的固定列名版：每个平台叫法不同（note_id / video_id / "
                                  "aweme_id / content_id…），这一页统一成中文列名，AI 不用每接一个平台"
                                  "重认一遍。"))
        lines.append(("和原始页的关系", "**原始数据页一个字没动，仍是权威**；统一页只是按列名搬的一份副本，"
                                       "行数、行序与原始页一一对应。"))
        lines.append(("对不上怎么办", "某个平台没有这一项（比如贴吧没有点赞数列），该格留空，**不猜、不补 0**。"
                                     "留空的列在下面逐列列出。"))
        vk = verified_kinds(platform) if platform else set()
        for u in unified:
            cn = KIND_CN.get(u["kind"], u["kind"])
            # 逐表判断：这家平台这张表的列名到底核对过没有
            if platform and u["kind"] not in vk:
                name_cn = PLATFORM_CN.get(platform, platform)
                lines.append((f"{cn}表 这家平台注意",
                              f"{name_cn}（{platform}）的{cn}数据，本机没有真实产出核对过列名，"
                              "统一页的列对应关系是按源码写入字段推断的，第一次用请抽查两行。"))
            lines.append((f"{cn}表 统一页 sheet 名", u.get("sheet", "")))
            lines.append((f"{cn}表 统一页列名", "、".join(u["cols"])))
            pairs = "；".join(f"{n}←{c}" for n, c, _ in u["mapping"] if c) or "（一列都没对上）"
            lines.append((f"{cn}表 列名对照", pairs))
            if u["unmapped"]:
                lines.append((f"{cn}表 对不上的列", "、".join(u["unmapped"]) + "（这些格子留空）"))
            if u["residual"]:
                lines.append((f"{cn}表 未搬进统一页的原列",
                              "、".join(u["residual"][:40])
                              + ("…" if len(u["residual"]) > 40 else "")
                              + "（原值都在原始页，这里没搬）"))
            if u.get("cells"):
                lines.append((f"{cn}表 统一页转换统计",
                              "；".join(f"{k} {v} 处" for k, v in u["cells"].items())))

    for ri, (k, v) in enumerate(lines, 1):
        a = ws0.cell(row=ri, column=1, value=k)
        if k.startswith("——") or ri == 1:
            a.font = bold
        ws0.cell(row=ri, column=2, value=v)
    ws0.column_dimensions["A"].width = 26
    ws0.column_dimensions["B"].width = 110
    ws0.freeze_panes = "A2"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def verify_export(out_path: Path, tables: List[Dict[str, Any]],
                  unified: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """把刚写出去的文件读回来验一遍。返回问题列表，空列表 = 通过。

    光"写成功"不算成功——冻结、筛选、文本格式、sheet 名这些都是会静默丢的，
    所以必须读回来量一遍（这是交接文档里明确要求的验收点）。
    """
    from openpyxl import load_workbook
    problems: List[str] = []
    wb = load_workbook(out_path)
    try:
        if "口径说明" not in wb.sheetnames:
            problems.append("缺「口径说明」sheet")
        for tbl in tables:
            title = tbl.get("sheet") or KIND_CN.get(tbl["kind"], tbl["kind"])
            if title not in wb.sheetnames:
                problems.append(f"缺 sheet「{title}」（现有 {wb.sheetnames}）")
                continue
            ws = wb[title]
            if ws.freeze_panes != "A2":
                problems.append(f"「{title}」首行没冻结（freeze_panes={ws.freeze_panes!r}）")
            if tbl["rows"] and not ws.auto_filter.ref:
                problems.append(f"「{title}」没有自动筛选")
            # 长数字列必须是文本（data_type=='s'）
            for ci, col in enumerate(tbl["cols"], 1):
                if not TEXT_COL_RE.search(col):
                    continue
                checked = 0
                bad = 0
                for ri in range(2, min(ws.max_row, 200) + 1):
                    cell = ws.cell(row=ri, column=ci)
                    if cell.value in (None, ""):
                        continue
                    checked += 1
                    if cell.data_type != "s":
                        bad += 1
                if checked and bad:
                    problems.append(f"「{title}」{col} 列有 {bad}/{checked} 个值不是文本（会变科学计数法）")
            if tbl["rows"]:
                got_rows = sum(1 for ri in range(2, ws.max_row + 1)
                               if any(ws.cell(row=ri, column=ci).value not in (None, "")
                                      for ci in range(1, len(tbl["cols"]) + 1)))
                if got_rows != len(tbl["rows"]):
                    problems.append(f"「{title}」读回 {got_rows} 行，应为 {len(tbl['rows'])} 行")
        # 统一格式页：行数必须和原始页一致（这是"副本没走样"的唯一硬证据）
        for u in (unified or []):
            title = u.get("sheet", "")
            if title not in wb.sheetnames:
                problems.append(f"缺统一格式 sheet「{title}」（现有 {wb.sheetnames}）")
                continue
            ws = wb[title]
            if ws.freeze_panes != "A2":
                problems.append(f"「{title}」首行没冻结")
            if u["rows"] and not ws.auto_filter.ref:
                problems.append(f"「{title}」没有自动筛选")
            got_rows = sum(1 for ri in range(2, ws.max_row + 1)
                           if any(ws.cell(row=ri, column=ci).value not in (None, "")
                                  for ci in range(1, len(u["cols"]) + 1)))
            if got_rows != len(u["rows"]):
                problems.append(f"「{title}」读回 {got_rows} 行，应为 {len(u['rows'])} 行")
            # 表头必须与约定列名逐一对应
            head = [ws.cell(row=1, column=ci).value for ci in range(1, len(u["cols"]) + 1)]
            if [str(h or "") for h in head] != u["cols"]:
                problems.append(f"「{title}」表头与约定列名不一致")
        # 两页行数对账：统一页不能比原始页多也不能少
        for u in (unified or []):
            src = next((t for t in tables if t["kind"] == u["kind"]), None)
            if src is not None and len(u["rows"]) != len(src["rows"]):
                problems.append(f"{KIND_CN.get(u['kind'], u['kind'])}：统一页 {len(u['rows'])} 行，"
                                f"原始页 {len(src['rows'])} 行，对不上")
    finally:
        wb.close()
    return problems


def read_task_meta(task_dir: Path, st: Dict[str, Any]) -> Dict[str, str]:
    """凑「口径说明」页的抬头信息：优先 state.json 的历史（结构化），退而读 report.md。"""
    meta: Dict[str, str] = {}
    meta["任务名"] = task_dir.name
    hit = None
    for h in (st.get("history") or []):
        if str(h.get("task_dir") or "") and Path(str(h["task_dir"])).name == task_dir.name:
            hit = h
            break
    rep = {}
    rp = task_dir / "report.md"
    if rp.is_file():
        txt = rp.read_text(encoding="utf-8", errors="replace")
        for line in txt.splitlines():
            m = re.match(r"^-\s*(生成时间|状态|参数|数量对账|耗时)[:：]\s*(.+)$", line.strip())
            if m:
                rep[m.group(1)] = m.group(2).strip()
    plat = (hit or {}).get("platform") or ""
    if not plat and rep.get("参数"):
        m = re.search(r"平台=(\w+)", rep["参数"])
        plat = m.group(1) if m else ""
    if plat:
        meta["平台"] = f"{PLATFORM_CN.get(plat, plat)}（{plat}）"
    if hit:
        meta["关键词"] = str(hit.get("keywords") or "")
        meta["任务类型"] = str(hit.get("type") or "")
        meta["请求条数"] = f"请求 {hit.get('requested')} 条（每词上限 {hit.get('count')}）"
        meta["实际条数"] = f"内容 {hit.get('contents')}；评论 {hit.get('comments')}"
        meta["任务状态"] = str(hit.get("status") or "")
        if hit.get("reason"):
            meta["失败原因"] = str(hit.get("reason"))
        meta["任务记录时间"] = f"{hit.get('ts') or ''}（state.json 里这条记录的写入时刻）"
        if hit.get("duration_s"):
            meta["耗时"] = human_duration(float(hit["duration_s"]))
    for k, v in rep.items():
        meta.setdefault(f"报告-{k}", v)
    meta["导出时间"] = now_str()
    return meta


def cmd_export(args: argparse.Namespace) -> int:
    """外壳：--json 时把人类看的文字改道到 stderr，stdout 只留一段 JSON。"""
    jm = _json_mode(args)
    box: Dict[str, Any] = {}
    real = sys.stdout
    if jm:
        sys.stdout = sys.stderr
    try:
        code = _export_impl(args, jm, box)
    finally:
        if jm:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            sys.stdout = real
    if jm:
        emit_json(box.get("payload") or {"ok": False, "status": "失败", "reason": "导出没能开始"})
    return code


def _export_impl(args: argparse.Namespace, jm: bool, box: Dict[str, Any]) -> int:
    st = load_state()

    def _fail(msg: str, **extra: Any) -> int:
        if jm:
            box["payload"] = {"ok": False, "status": "失败", "reason": msg, **extra}
        else:
            print(f"⛔ {msg}")
        return 1

    task_dir, tried = resolve_task_dir(args.task, st)
    if task_dir is None:
        if jm:
            box["payload"] = {"ok": False, "status": "失败", "reason": "找不到任务目录", "tried": tried}
        else:
            print("⛔ 找不到任务目录。我按顺序找过这些地方：")
            for t in tried:
                print(f"    - {t}")
            print('  ↳ 最稳的写法是直接给绝对路径：export --task "' + str(TASKS_ROOT) + '\\<任务名>"')
        return 1
    print(f"任务目录：{task_dir}")
    att = pick_attempt(task_dir, args.attempt)
    if att is None:
        if args.attempt:
            have = sorted(p.name for p in task_dir.glob("attempt_*") if p.is_dir())
            return _fail(f"这个任务没有 attempt_{args.attempt}；现有：{'、'.join(have) or '一个都没有'}",
                         task_dir=str(task_dir))
        return _fail("这个任务目录下没有 attempt_N 子目录（任务可能连预检都没过，没有产生任何尝试）。",
                     task_dir=str(task_dir))
    print(f"数据目录：{att}")
    kinds = [k.strip().lower() for k in (args.kinds or "contents,comments").split(",") if k.strip()]
    bad = [k for k in kinds if k not in KIND_CN]
    if bad:
        return _fail(f"--kinds 里有不认识的值：{','.join(bad)}；只支持 contents/comments/creators")
    tables: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for k in kinds:
        tbl = build_kind_table(att, k)
        if tbl is None:
            skipped.append(k)
            continue
        tbl["check"] = self_check_table(tbl)
        tables.append(tbl)
    if not tables:
        return _fail(f"这个任务目录里没有任何数据（要的是 {'、'.join(KIND_CN[k] for k in kinds)}；零产出或预检失败）。",
                     task_dir=str(task_dir), attempt_dir=str(att))
    for k in skipped:
        print(f"· {KIND_CN[k]}：这个目录里没有{k}文件，跳过（会在口径页标「未采集」）")

    out = Path(args.out).expanduser() if args.out else (
        EXPORT_DIR / f"{task_dir.name}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx")
    meta = read_task_meta(task_dir, st)
    # 统一格式页：默认加；--no-unified 可关掉（只要原样 Excel 的人用）
    unified: List[Dict[str, Any]] = []
    if not getattr(args, "no_unified", False):
        for tbl in tables:
            u = build_unified_table(tbl)
            if u is not None:
                unified.append(u)
    platform = str(meta.get("平台") or "")
    short = ""
    m = re.search(r"[（(]([a-z]+)[）)]", platform)
    if m:
        short = m.group(1)
    elif platform:
        short = platform.strip().lower()
    write_export_xlsx(task_dir, att, tables, out, meta, kinds, unified, short)
    problems = verify_export(out, tables, unified)
    hard = [p for p in problems if not p.startswith("✓")]

    print(f"\n{SEP}\n【导出结果】{out}\n{SEP}")
    for tbl in tables:
        cn = KIND_CN.get(tbl["kind"], tbl["kind"])
        print(f"{cn}表：{len(tbl['rows'])} 行 × {len(tbl['cols'])} 列（sheet「{tbl.get('sheet')}」）")
        for ln in tbl["check"]["lines"]:
            print(f"    {ln}")
    for u in unified:
        cn = KIND_CN.get(u["kind"], u["kind"])
        print(f"{cn}统一格式表：{len(u['rows'])} 行 × {len(u['cols'])} 列"
              f"（sheet「{u['sheet']}」）")
        pairs = "；".join(f"{n}←{c}" for n, c, _ in u["mapping"] if c)
        print(f"    列名对照：{pairs or '（一列都没对上）'}")
        if u["unmapped"]:
            print(f"    对不上的列（留空，不猜）：{'、'.join(u['unmapped'])}")
        if u["residual"]:
            print(f"    没搬进统一页的原列 {len(u['residual'])} 个（原值都在原始页）")
    if hard:
        print("\n✗ 回读校验没全过：")
        for p in hard:
            print(f"    - {p}")
    else:
        print("\n✓ 回读校验通过：首行冻结 A2、自动筛选非空、长数字列为文本、"
              "sheet 名与行数对得上、统一页与原始页行数一一对应")
    print(f"文件：{out}")
    print(SEP)

    if jm:
        box["payload"] = {
            "ok": not hard,
            "status": "成功" if not hard else "部分完成",
            "reason": "" if not hard else "回读校验没全过",
            "file": str(out),
            "task_dir": str(task_dir),
            "attempt_dir": str(att),
            "sheets": [{"kind": t["kind"], "kind_cn": KIND_CN.get(t["kind"], t["kind"]),
                        "sheet": t.get("sheet", ""), "rows": len(t["rows"]),
                        "cols": len(t["cols"])} for t in tables],
            "unified_sheets": [{
                "kind": u["kind"], "sheet": u["sheet"], "rows": len(u["rows"]),
                "columns": u["cols"],
                "mapping": [{"column": n, "from": c} for n, c, _ in u["mapping"] if c],
                "unmapped": u["unmapped"],
            } for u in unified],
            "skipped_kinds": skipped,
            "problems": hard,
        }
    return 0 if not hard else 2


# ----------------------------------------------------------------------------
# 单任务主流程
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 跑前探针（--probe）：拿锁之后、正式开跑之前，用一次最小真实请求确认"票还能用"
# ----------------------------------------------------------------------------
PROBE_DIR = HOME / "_probe"          # 临时区，不碰 tasks\，也不进 state.json 历史
PROBE_TIMEOUT_MIN = 5                # 探针只需 1 条数据，冷启动 20~40 秒足够

# 为什么选"搜索泛词取 1 条"（方案 B）而不是"detail 抓固定内容"（方案 A）：
# A 最准，但内容 ID 会失效（作者删稿、转私密、平台改号），一小组"定海神针"ID 迟早
# 全部失灵；失灵时和"票失效"在现象上一模一样，探针反而会骗人，还得长期维护 ID 表。
# B 只依赖"这个平台有内容"——平台再冷门也总有热词，所以 B 失败只可能是
# 票/风控/网络，正好是探针想区分的那三种原因。代价是 B 不验"这条内容能不能取详情"，
# 但详情失败在正式跑里会立刻暴露，且可用 --type detail 的正式任务兜住。
PROBE_WORDS = {
    "dy": ["新闻", "生活"],
    "bili": ["生活", "游戏"],
    "ks": ["生活", "新闻"],
    "wb": ["新闻", "热搜"],
    "xhs": ["生活", "美食"],
    "tieba": ["原神", "游戏"],
    "zhihu": ["生活", "大学"],
}

# 本机验不了登录票有效性的平台（平台接口要签名，只能确认"票在"，不能确认"票还有效"）。
# 出处：edge_login_sync.probe_login 只对 bili/wb/tieba/zhihu 能真问一句，其余都落到
# "? cookie 在（该平台接口要签名，本机验不了有效性）"。
# 这几个平台一旦票过期，预检照样全绿，只能等真跑时才发现 → 长任务最该开探针。
LOGIN_UNVERIFIABLE = ("dy", "ks", "xhs")

# 「算长任务」的门槛：这次一共要抓多少条。低于这个量级，探针那 20~40 秒冷启动
# 反而不划算（小任务失败了重跑一次就完事）。
# 只看条数不看超时：超时是算出来的，基础值就有 10 分钟，拿它当门槛等于"永远算长任务"。
PROBE_AUTO_MIN_REQUESTED = 100


def auto_probe_decision(platform: str, requested: int) -> Tuple[bool, str]:
    """探针三态的自动判断。返回 (是否开探针, 为什么)。

    只在用户没表态（既没 --probe 也没 --no-probe）时用。
    """
    if platform not in LOGIN_UNVERIFIABLE:
        return False, ""
    if requested < PROBE_AUTO_MIN_REQUESTED:
        return False, ""
    return True, (f"自动开探针：{PLATFORM_CN.get(platform, platform)} 这台机器验不了登录票的有效性，"
                  f"这次又算长任务（共 {requested} 条）——"
                  f"先花半分钟取 1 条真数据，免得票早就过期还烧掉整晚。"
                  f"不想探可以加 --no-probe。")


def probe_ticket(vp: Dict[str, Any]) -> Tuple[bool, str, str]:
    """跑一次最小真实请求，判断票还能不能用。返回 (通过, 结论, 证据日志路径)。

    必须在**已持有运行锁**时调用（否则等于和别人抢浏览器调试端口）。
    复用 run_attempt（不复制一套子进程管理），临时目录跑完即删。
    """
    platform = vp["platform"]
    words = PROBE_WORDS.get(platform, ["新闻"])
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    pdir = PROBE_DIR / f"{platform}_{stamp}"

    # 继承正式任务的 headless：可见窗口和无头模式的反爬表现不同，
    # 探针必须和接下来真跑的方式一致，否则绿灯可能是假的。
    pvp = dict(vp)
    pvp.update({
        "name": f"probe_{platform}", "type": "search", "count": 1,
        "format": "jsonl", "comments": False, "media": False,
        "max_comments": 1, "specified_id": "", "creator_id": "",
        "passthrough": "", "timeout_min": PROBE_TIMEOUT_MIN, "requested": 1,
    })

    print(f"\n{SEP}\n【跑前探针】{PLATFORM_CN.get(platform, platform)}｜探测词 {'、'.join(words)}"
          f"｜模式 {'无头' if vp['headless'] else '可见窗口'}\n{SEP}")
    print("  目的：先确认真能拿到数据，避免票已失效还烧掉整晚。")

    log_path = ""
    for idx, w in enumerate(words, 1):
        pvp["keywords"] = [w]
        att = run_attempt(pvp, pdir, idx)
        res = collect_results(Path(att["attempt_dir"]))
        log_path = att["log_path"]
        got = res["contents"]

        if got > 0:
            shutil.rmtree(pdir, ignore_errors=True)   # 探针数据不交付，删掉免得占地方
            return True, f"探针：取到 {got} 条数据 ✓（票可用，探测词「{w}」）", log_path

        if att["timed_out"]:
            return (False,
                    f"探针超时（{PROBE_TIMEOUT_MIN} 分钟没跑完）→ 可能是浏览器起不来或网络卡死，"
                    f"不是票的问题；先单独跑一次 status 看残留浏览器", log_path)
        if att["qr_prompted"]:
            return (False,
                    "探针弹出扫码窗口 → 登录态已彻底过期（票里连会话都没有）。"
                    r"补票：agent_crawler\edge_login_sync.py --import <平台> --curl-file <文件>"
                    "（有 Edge 扩展导出更省事：--import-json <平台> latest）", log_path)

        extra = ""
        if idx < len(words):
            extra = f"；换探测词「{words[idx]}」再探一次"
            print(f"  · 探测词「{w}」没取到数据{extra}…")
        else:
            clue, _ = classify_reason(att["log_text"])
            empty_ev = detect_empty_result(att["log_text"], platform)
            if empty_ev:
                head = f"平台明确返回空（{empty_ev['text']}）"
            elif not clue.startswith("未知"):
                head = f"日志线索：{clue}"
            else:
                head = f"进程正常退出但没落盘数据（exit={att['exit_code']}）"
            return (False,
                    f"探针未取到数据 → 可能是登录票失效/被风控降级/网络问题：{head}。"
                    " 请先 login（或 edge_login_sync.py --import）再跑，别直接开整晚任务",
                    log_path)

    return False, "探针未取到数据（所有探测词都空）", log_path


def run_task(vp: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    global _LOCK
    result: Dict[str, Any] = {"params": vp, "status": "失败", "reason": "",
                              "attempt": 0, "max_attempts": 1 + int(vp.get("retries") or 0)}
    t0 = time.time()
    task_dir: Optional[Path] = None
    own_lock = False            # 本次是否由本任务抢锁（批量队列由外层统一持有）
    browser_scanned = False     # run_attempt 正常返回时已扫过残留浏览器，收尾不重复扫

    try:
        # 开跑前先撕掉过期纸条：上一个任务被强杀时留下的纸条不能误伤这一次
        clear_stale_cancel()
        # 0) 门口挂牌（拿不到直接劝退，不硬闯）
        if _LOCK is None or not _LOCK.held:
            _LOCK = RunLock()
            lock_notes = _LOCK.acquire(vp, wait_min=float(vp.get("wait_lock") or 0))
            own_lock = True
        else:
            _LOCK.update_task(vp)
            lock_notes = ["锁沿用中（批量队列）✓"]
        deep = bool(_LOCK.fresh)     # 刚抢到锁才做深度预检
        _LOCK.fresh = False

        # 预检
        print(f"\n{SEP}\n【预检】任务 {vp['name']} | 平台 {vp['platform']} | {now_str()}\n{SEP}")
        if vp.get("platform_note"):
            print(f"  ⚠ {vp['platform_note']}")
        if vp.get("probe_auto_reason"):
            print(f"  • {vp['probe_auto_reason']}")
        for note in lock_notes + preflight(vp, deep=deep):
            print("  • " + note)

        # 跑前探针：只在"刚抢到锁"时做（独立 run 就是这一次；batch 队列只在开头探一次，
        # 后面的任务沿用这次结论——同一把锁、同一份票，没必要每次重开浏览器）。
        if vp.get("probe"):
            if deep:
                ok, note, plog = probe_ticket(vp)
                print("  • " + note)
                if not ok:
                    result["probe_log"] = plog
                    # 失败证据留在 _probe\ 里（成功后已删），直接指给用户看
                    raise ParamError(f"{note}｜证据日志：{plog}")
            else:
                print("  • 探针：锁沿用中（队列开头已探过票），本轮跳过")

        task_dir = make_task_dir(vp)
        result["task_dir"] = str(task_dir)
        print(f"  • 任务目录: {task_dir}")

        requested = vp["requested"]
        max_attempts = 1 + vp["retries"]
        attempt = 0
        while attempt < max_attempts:
            # 两次尝试之间也要看纸条：喊停后不该自动重试，直接收工
            if cancel_targets_me():
                break
            attempt += 1
            att = run_attempt(vp, task_dir, attempt)
            browser_scanned = True
            res = collect_results(Path(att["attempt_dir"]))
            got = res["contents"]
            reason, retryable = "", False
            canceled = bool(att.get("canceled"))

            if canceled:
                reason = "已取消（人为喊停；已落盘的数据保留，不会补跑）"
            elif att["timed_out"]:
                reason = ("超时（停止信号送达后进程退出，jsonl 数据完整；Excel 汇总表仅完整运行的任务生成）" if att.get("stop_mode")
                          else "超时（进程 20 秒未响应停止信号，已强杀进程树）")
                retryable = True
            elif att["exit_code"] not in (0, None):
                reason, retryable = classify_reason(att["log_text"])
                if reason.startswith("未知"):
                    reason = f"进程异常退出（exit={att['exit_code']}），日志未见典型错误特征"
            elif att["qr_prompted"]:
                # bilibili 登录失败路径 sys.exit() 无参数 → exit=0，但属失败
                # 注意：agent_crawl.py 自己**没有** --import 参数，要跑的是同目录的 edge_login_sync.py
                reason = ("登录未完成（等满约 10 分钟没扫上码 / 登录态已过期）→ 补票请跑："
                          r".venv\Scripts\python.exe agent_crawler\edge_login_sync.py --import <平台> --curl-file <文件>"
                          "（有 Edge 扩展导出的话更省事：同一脚本 --import-json <平台> latest）")

            # 空结果判定：进程正常退出、一条没落盘、日志显示平台确实返回了空。
            # 「成功执行但平台没数据」和基础设施故障本质不同，单独判一段（见交接文档①）。
            empty_ev = None
            if (got == 0 and not att["timed_out"]
                    and att["exit_code"] in (0, None) and not att["qr_prompted"]):
                empty_ev = detect_empty_result(att["log_text"], vp["platform"])

            # 状态判定（数据优先：有产出即至少部分完成，绝不隐藏数据）
            if canceled:
                # 喊停时已经拿到数据：算「部分完成」（数据是真的），只把原因写明是被喊停的
                status = "部分完成" if got > 0 else "已取消"
            elif got >= requested and reason == "":
                status = "成功"
            elif got > 0:
                status = "部分完成"
                if reason == "":
                    reason = (f"平台实际返回 {got} 条 < 请求 {requested} 条"
                              f"（{len(vp['keywords'])}个关键词×{vp['count']}），"
                              f"常见原因：平台结果不足/风控限量/提前翻页结束")
                    clue, _ = classify_reason(att["log_text"])
                    if not clue.startswith("未知"):
                        reason += f"；日志线索：{clue}"
            else:
                status = "失败"
                if reason == "":
                    if empty_ev is not None:
                        # 平台就是没内容：原因里必须带关键词 + 原始日志那行，能直接行动
                        reason = empty_ev["text"]
                    else:
                        clue, _ = classify_reason(att["log_text"])
                        reason = clue if not clue.startswith("未知") else "无输出且无明显错误特征（请查日志尾部）"

            result.update({
                "status": status, "reason": reason, "attempt": attempt,
                "max_attempts": max_attempts,
                "contents": got, "comments": res["comments"],
                "requested": requested, "files": res["files"],
                "primary": res["primary"], "duration": time.time() - t0,
                "exit_code": att["exit_code"], "qr_prompted": att["qr_prompted"],
                "log_path": att["log_path"], "attempt_dir": att["attempt_dir"],
                "log_text": att["log_text"], "empty_result": bool(empty_ev),
            })

            if status in ("成功", "部分完成") or not retryable or attempt >= max_attempts:
                break
            print(f"\n  ↳ {reason} → 属基础设施类错误，自动重试（{attempt}/{max_attempts - 1}）…")

        # 一次都没跑就被喊停（纸条来得比第 1 次尝试还早）
        if attempt == 0 and cancel_targets_me():
            result.update({"status": "已取消",
                           "reason": "已取消（开跑前就收到喊停），一条数据也没采",
                           "duration": time.time() - t0, "requested": requested,
                           "contents": 0, "comments": 0})

        # 抽查
        if result.get("contents", 0) > 0 and result.get("attempt_dir"):
            result["spot_check"] = spot_check(Path(result["attempt_dir"]))

        # 成功后记忆默认值
        if result["status"] == "成功" and not vp["is_login_task"]:
            d = st["defaults"]
            d.update({"count": vp["count"], "format": vp["format"], "type": vp["type"],
                      "comments": vp["comments"], "max_comments": vp["max_comments"],
                      "media": vp["media"], "headless": vp["headless"]})

    except ParamError as e:
        result["status"] = "预检失败"
        result["reason"] = str(e)
        result["duration"] = time.time() - t0
        result["contents"] = 0
        result["requested"] = vp["requested"]
    except LockBusy as e:
        result["status"] = "预检失败"
        result["reason"] = f"本机已有任务在跑：{e}"
        result["duration"] = time.time() - t0
        result["contents"] = 0
        result["requested"] = vp["requested"]
    except KeyboardInterrupt:
        result["status"] = "失败"
        result["reason"] = "用户中断（Ctrl+C）"
        result["duration"] = time.time() - t0
    finally:
        # 浏览器安全网：run_attempt 正常返回时已经扫过并关过，这里只兜异常/中断的底
        if not browser_scanned:
            for pid, _cmd in ps_find_crawler_browsers(vp["platform"]):
                graceful_close_browser(pid)
        # 摘牌：只摘自己的
        if own_lock and _LOCK is not None:
            _LOCK.release()
            _LOCK = None

    # 写报告 + 历史
    if task_dir is not None:
        write_report(task_dir, result)
    st["history"].append(history_entry(result))
    st["history"] = st["history"][-100:]
    save_state(st)
    # 纸条用完了撕掉：不清的话，下次运行一开跑就会被一条过期纸条喊停
    if cancel_targets_me():
        clear_cancel()
    print_report(result)
    return result


def history_entry(r: Dict[str, Any]) -> Dict[str, Any]:
    vp = r["params"]
    return {
        "ts": now_str(), "name": vp["name"], "platform": vp["platform"],
        "type": vp["type"], "keywords": ",".join(vp["keywords"]),
        "count": vp["count"], "format": vp["format"],
        "status": r.get("status", "?"), "reason": r.get("reason", ""),
        "contents": r.get("contents", 0), "requested": r.get("requested", 0),
        "comments": r.get("comments", 0),
        "attempt": r.get("attempt", 0), "max_attempts": r.get("max_attempts", 1),
        "retries": max(0, int(r.get("attempt") or 0) - 1),
        "reason_kind": reason_kind(r.get("reason", "")),
        "task_dir": r.get("task_dir", ""), "duration_s": round(r.get("duration", 0)),
    }


def fmt_attempt(r: Dict[str, Any]) -> str:
    """把尝试次数说成人话：第几次跑成的、中间重试了几次。"""
    att = int(r.get("attempt") or 0)
    mx = int(r.get("max_attempts") or 1)
    if att <= 0:
        return "没跑起来（预检阶段就停了）"
    retried = max(0, att - 1)
    if retried == 0:
        return f"第 1 次就出结果（一次过，没重试；最多允许跑 {mx} 次）"
    tail = "（已到重试上限）" if att >= mx else ""
    return f"第 {att} 次才出结果｜中间重试了 {retried} 次{tail}（最多允许跑 {mx} 次）"


def fmt_params(vp: Dict[str, Any]) -> str:
    return (f"平台={vp['platform']} 类型={vp['type']} 关键词={'/'.join(vp['keywords'])} "
            f"数量={vp['count']}条/词 格式={vp['format']} "
            f"评论={'开(' + str(vp['max_comments']) + '条/内容)' if vp['comments'] else '关'} "
            f"媒体={'开' if vp['media'] else '关'} 无头={'开' if vp['headless'] else '关'}"
            f"{' 跑前探针=开' if vp.get('probe') else ''}")


STATUS_ICON = {"成功": "✅", "部分完成": "🟡", "失败": "❌", "预检失败": "⛔", "已取消": "🚫"}


def print_report(r: Dict[str, Any]) -> None:
    vp = r["params"]
    icon = STATUS_ICON.get(r["status"], "❓")
    print(f"\n{SEP}\n【任务报告】{vp['name']} | {vp['platform']} | {now_str()}\n{SEP}")
    print(f"状态     : {icon} {r['status']}")
    kind = reason_kind(r.get("reason", ""))
    if r.get("reason"):
        print(f"原因     : {r['reason']}")
    if kind:
        print(f"问题归类 : {kind_label(kind)}")
        print(f"该怎么办 : {kind_advice(kind)}")
    print(f"参数     : {fmt_params(vp)}")
    req = r.get("requested", 0)
    got = r.get("contents", 0)
    print(f"数量对账 : 内容 {got} 条（请求 {req} 条）"
          + (f"；评论 {r.get('comments', 0)} 条" if r.get("comments") else ""))
    print(f"尝试次数 : {fmt_attempt(r)}")
    sc = r.get("spot_check")
    if sc:
        print(f"数据抽查 : {'通过 ✓' if sc['ok'] else '存在问题 ✗'}")
        for s in sc["samples"]:
            print(f"    {s}")
    primary = r.get("primary")
    if primary:
        print(f"成果文件 : {primary['path']}")
    if r.get("task_dir"):
        print(f"任务目录 : {r['task_dir']}")
    if r.get("log_path"):
        print(f"运行日志 : {r['log_path']}")
    if r.get("duration"):
        print(f"耗时     : {human_duration(r['duration'])}")
    print(SEP)
    print(f"RESULT: name={vp['name']} status={r['status']} contents={got}/{req} "
          f"comments={r.get('comments', 0)} attempt={r.get('attempt', 0)}/{r.get('max_attempts', 1)} "
          f"file={primary['path'] if primary else 'NA'}")
    print(SEP)


def write_report(task_dir: Path, r: Dict[str, Any]) -> None:
    vp = r["params"]
    kind = reason_kind(r.get("reason", ""))
    lines = [
        f"# 任务报告：{vp['name']}",
        "",
        f"- 生成时间: {now_str()}",
        f"- 状态: {r['status']}" + (f"（{r['reason']}）" if r.get("reason") else ""),
        f"- 参数: {fmt_params(vp)}",
        f"- 数量对账: 内容 {r.get('contents', 0)} / 请求 {r.get('requested', 0)}；"
        f"评论 {r.get('comments', 0)}",
        f"- 尝试次数: {fmt_attempt(r)}",
        f"- 耗时: {human_duration(r.get('duration', 0))}",
        "",
    ]
    if kind:
        lines.extend([
            "## 这次是什么问题",
            "",
            f"- 归类: **{kind_label(kind)}**（`{kind}`）",
            f"- 该怎么办: {kind_advice(kind)}",
            "",
        ])
    if r.get("files"):
        lines.append("## 输出文件")
        for f in r["files"]:
            cnt = (f"记录数={f['records']}" if "records" in f else
                   f"sheets={f.get('sheets')}" if f["kind"] == "excel" else "")
            lines.append(f"- `{f['path']}` ({f['kind']}, {f['size']}B {cnt})")
        lines.append("")
    sc = r.get("spot_check")
    if sc:
        lines.append("## 完整性抽查")
        lines.extend(f"- {s}" for s in sc["samples"])
        lines.append(f"\n结论: {'通过' if sc['ok'] else '存在问题，请人工复核'}")
        lines.append("")
    if r.get("log_path"):
        lines.append(f"## 日志\n\n完整子进程输出见 `{r['log_path']}`。")
        lines.append("\n### 日志尾部\n```\n" + (tail(r.get("log_text", "")) if r.get("log_text") else "(预检阶段即失败，无运行日志)") + "\n```")
    (task_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    st = load_state()
    p = vars(args)
    jm = _json_mode(args)
    try:
        vp = validate_params(p, st)
    except ParamError as e:
        if jm:
            emit_json({"ok": False, "status": "预检失败", "reason": f"参数校验未通过：{e}"})
        else:
            print(f"⛔ 参数校验未通过：\n  {e}")
        return 1
    if getattr(args, "detach", False):
        return _detach_run(vp, jm)
    with _stdout_to_stderr(jm):
        r = run_task(vp, st)
    if jm:
        emit_json(result_payload(r))
    return {"成功": 0, "部分完成": 2, "已取消": 2}.get(r["status"], 1)


def _detach_run(vp: Dict[str, Any], jm: bool) -> int:
    return _detach_launch(vp["name"], vp["platform"], vp["keywords"], vp["requested"], jm)


def _detach_launch(name: str, platform: str, keywords: List[str], requested: int,
                   jm: bool, match_history: bool = True, noun: str = "任务") -> int:
    """把任务交给一个脱离父进程的"后台工人"，主进程立刻返回。

    为什么要脱离：长任务（几千上万条）得跑几十分钟，调用方（AI/脚本/终端）如果一直
    等着，要么超时被掐、要么把这个窗口占死。脱离后，关掉终端也不影响它跑完。
    代价：后台工人不再往屏幕上说话 → 进度用 status 看，喊停用 cancel。

    match_history=False 用于批量队列 / 续跑：它们不靠"历史多了一条"判断跑完，
    只看后台进程还在不在。noun 只影响提示文字里怎么称呼这个活儿。
    """
    # 门口先看一眼：明确有人持锁就别白拉起一个后台进程
    lk = read_lock()
    if lk is not None:
        holder = int(lk.get("pid") or 0) if isinstance(lk, dict) else 0
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            age = 0.0
        if holder and _pid_alive(holder) and age < LOCK_STALE_SEC:
            msg = f"本机已有任务在跑，不能并发：{describe_lock(lk)}"
            if jm:
                emit_json({"ok": False, "status": "预检失败", "reason": msg,
                           "task": name, "platform": platform})
            else:
                print(f"⛔ {msg}\n  ↳ 等它跑完再来；要是想停掉那个任务，用 cancel 子命令")
            return 1

    DETACH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DETACH_LOG_DIR / f"{sanitize_name(name)}_{platform}_{stamp}.log"
    # 记下"开跑前已有几条历史"，稍后靠它判断后台是不是已经跑完了（别拿同名老记录冒充）
    hist_before = len(load_state()["history"])
    # 用当前命令行的原样参数重跑一遍，只摘掉 --detach（后台工人自己是前台跑法）
    argv = [a for a in sys.argv[1:] if a != "--detach"]
    cmd = [str(VENV_PY), str(Path(__file__).resolve())] + argv

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUNBUFFERED", "1")

    # 后台工人的启动方式，三种标志组合按「最独立」到「最保守」依次试：
    #  • CREATE_NO_WINDOW          给它一个看不见的独立控制台 → 调用方的窗口关掉也不会连坐；
    #  • CREATE_NEW_PROCESS_GROUP  让它能收 CTRL_BREAK → 喊停时走优雅收尾，不硬杀浏览器；
    #  • CREATE_BREAKAWAY_FROM_JOB 如果调用方被装进了「作业对象」（IDE/终端常见），
    #    请求跳出那个作业，这样调用方一退出就不会把后台工人一起带走。
    #    有些作业对象不允许跳出（会抛拒绝访问），那种情况自动降级，不影响功能。
    base_flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        base_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    attempts = ([base_flags | breakaway] if breakaway else []) + [base_flags, 0]

    proc = None
    err: Optional[Exception] = None
    for flags in attempts:
        try:
            # 屏幕输出重定向进日志文件：父进程退出后管道会断，所以不能用 PIPE
            with open(log_path, "wb") as lf:
                proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                        stdout=lf, stderr=subprocess.STDOUT, env=env,
                                        creationflags=flags, close_fds=True)
            break
        except Exception as e:
            err = e
            proc = None
    if proc is None:
        msg = f"后台任务没起来：{type(err).__name__}: {err}"
        if jm:
            emit_json({"ok": False, "status": "预检失败", "reason": msg})
        else:
            print(f"⛔ {msg}")
        return 1

    # 盯一段：等牌子出现（说明真的开跑了）或进程消失（说明起来就挂了）
    deadline = time.time() + 12
    alive, locked_by_me = True, False
    while time.time() < deadline:
        time.sleep(1)
        alive = _pid_alive(proc.pid)
        lk2 = read_lock()
        locked_by_me = bool(lk2) and int((lk2 or {}).get("pid") or 0) == proc.pid
        if locked_by_me or not alive:
            break
    # 短任务可能已经跑完了：历史比开跑前多了一条且正是它，才算"已完成"
    st_now = load_state()
    new_entries = st_now["history"][hist_before:] if len(st_now["history"]) > hist_before else []
    done = next((h for h in reversed(new_entries)
                 if h.get("task_dir") and Path(str(h["task_dir"])).name.startswith(name)), None) \
        if match_history else None
    # 批量队列本身不写 history：只要进程不在了就当它跑完了（后台工人跑完自己会退出）
    finished = (not alive) and (done is not None or not match_history)

    if alive:
        status_txt = "后台已启动"
        note = ("后台跑着，可以关掉这个窗口；进度用 status 看，要停就 cancel。"
                if locked_by_me else
                "后台进程已经在跑，正在做开跑前的检查（约十几秒），稍后 status 能看到进度。")
    elif finished:
        status_txt = ("已完成（任务短，后台很快就跑完了）" if match_history
                      else f"已完成（{noun}跑完了）")
        note = (f"任务已经跑完，结果：{done.get('status')}，内容 {done.get('contents')} 条。"
                if done is not None else
                f"{noun}已经跑完，去看屏幕输出核对结果：{log_path}")
    else:
        status_txt = "启动即退出"
        note = f"它刚启动就退出了，去看日志：{log_path}"

    payload = {
        "ok": alive or finished,
        "status": status_txt,
        "task": name,
        "platform": platform,
        "keywords": keywords,
        "requested": requested,
        "pid": proc.pid,
        "locked": locked_by_me,
        "log": str(log_path),
        "how_to_check": (f"status --task {name}" if match_history
                         else ("status（后台队列整条看锁和进度就行，别加 --task）" if noun == "队列"
                               else f"status --task {name}（看补抓进度）")),
        "how_to_cancel": "cancel",
        "note": note,
    }
    if finished:
        if done is not None:
            payload.update({"contents": done.get("contents"), "result_status": done.get("status")})
        else:
            payload["result_status"] = "完成"
    if not alive and not finished:
        payload["reason"] = f"后台进程启动后立刻退出，日志：{log_path}"
    if jm:
        emit_json(payload)
    else:
        icon = "✅" if alive else ("✅" if finished else "⛔")
        print(f"{icon} {status_txt}（第 {proc.pid} 号进程）")
        print(f"   任务: {name}｜平台 {platform}｜请求 {requested} 条")
        print("   " + note)
        if alive:
            print(f"   进度: agent_crawl.py status --task {name}")
            print(f"   喊停: agent_crawl.py cancel")
            print(f"   屏幕输出: {log_path}")
    return 0 if (alive or finished) else 1


def cmd_version(args: argparse.Namespace) -> int:
    """查版本：装了哪一版、这版改了什么、跑在哪个 Python 下。"""
    jm = _json_mode(args)
    payload = {
        "ok": True,
        "version": AGENT_VERSION,
        "note": AGENT_VERSION_NOTE,
        "python": sys.executable,
        "home": str(HOME),
        "mcp_server": str(HOME / "mcp_server.py"),
        "mcp_server_exists": (HOME / "mcp_server.py").is_file(),
    }
    if jm:
        emit_json(payload)
        return 0
    print(f"插件版本：{AGENT_VERSION}")
    print(f"这版内容：{AGENT_VERSION_NOTE}")
    print(f"装在：{HOME}")
    print(f"Python：{sys.executable}")
    print(f"MCP 工具层：{'有' if payload['mcp_server_exists'] else '没有（mcp_server.py 缺失）'}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """喊停正在跑的任务（不硬杀：递一张纸条，让它自己优雅收尾）。"""
    jm = _json_mode(args)
    lk = read_lock()
    if lk is None:
        if jm:
            emit_json({"ok": False, "status": "无任务在跑", "reason": "当前没有任务在跑，不用取消"})
        else:
            print("ℹ️  当前没有任务在跑，不用取消。")
        return 1
    holder = int(lk.get("pid") or 0) if isinstance(lk, dict) else 0
    if not holder or not _pid_alive(holder):
        # 牌子还在、人已经没了：这就是残留，顺手清掉（喊停的人本来就想让它别占着）
        cleaned = False
        try:
            LOCK_FILE.unlink()
            cleaned = True
        except Exception:
            pass
        clear_cancel()
        msg = (f"那个进程已经不在了（{describe_lock(lk)}），说明它是被强行关掉的，不是还在跑。\n"
               + ("  ↳ 残留牌子已帮你清掉，现在可以重新开跑。"
                  if cleaned else f"  ↳ 残留牌子清不掉，请手动删除：{LOCK_FILE}"))
        if jm:
            emit_json({"ok": True, "status": "无任务在跑", "reason": msg,
                       "lock_cleaned": cleaned, "lock_file": str(LOCK_FILE)})
        else:
            print(f"ℹ️  {msg}")
        return 0

    note = {"pid": holder, "task": str((lk or {}).get("task") or ""),
            "platform": str((lk or {}).get("platform") or ""),
            "requested_at": now_str(), "by_pid": os.getpid()}
    try:
        CANCEL_FILE.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        msg = f"喊停纸条写不下去（{e}），请手动结束进程 PID {holder}"
        if jm:
            emit_json({"ok": False, "status": "喊停失败", "reason": msg})
        else:
            print(f"⛔ {msg}")
        return 1

    wait_sec = max(0, int(getattr(args, "wait", 30) or 0))
    t_end = time.time() + wait_sec
    stopped = False
    while time.time() < t_end:
        time.sleep(2)
        if read_lock() is None or not _pid_alive(holder):
            stopped = True
            break

    payload = {
        "ok": True,
        "status": "已停止" if stopped else "已喊停，正在收尾",
        "task": note["task"], "platform": note["platform"], "pid": holder,
        "waited_s": wait_sec,
        "note": ("它已经停下来了，已落盘的数据都在任务目录里，没跑完的部分不会补。"
                 if stopped else
                 "纸条已递进去，它会在下一次检查（约 2 秒）收到并开始收尾（发停止信号→关浏览器→写报告→摘牌）。"
                 f"再等 {wait_sec} 秒还没停，多半是在等浏览器关窗口，可以用 status 再看一次。"),
    }
    if stopped:
        clear_cancel()
    if jm:
        emit_json(payload)
    else:
        print(f"{'✅' if stopped else '⏳'} {payload['status']}"
              + (f"｜任务「{note['task']}」" if note["task"] else "")
              + f"｜PID {holder}")
        print("   " + payload["note"])
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    """不真跑，只算给你看：大概能拿多少条、要等多久、该不该换词。

    依据只有两样：本机历史任务的真成绩 + 封装层的超时公式。绝不编数字：
    没有同类历史就明说"估不准"，给的是保守的粗估。
    """
    st = load_state()
    jm = _json_mode(args)
    try:
        vp = validate_params(vars(args), st)
    except ParamError as e:
        if jm:
            emit_json({"ok": False, "status": "参数校验未通过", "reason": str(e)})
        else:
            print(f"⛔ 参数校验未通过：\n  {e}")
        return 1

    est = build_estimate(vp, st)
    if jm:
        emit_json(est)
        return 0

    print(f"{SEP}\n【预估】{vp['name']}｜{vp['platform']}｜{now_str()}\n{SEP}")
    print(f"要看的东西: {fmt_params(vp)}")
    print(f"请求条数  : 内容 {est['requested']} 条"
          + (f"；评论最多 {est['max_comments_total']} 条" if vp["comments"] else "（不抓评论）"))
    print(f"大概耗时  : {est['eta_text']}")
    print(f"把握大小  : {est['confidence_label']}（{est['basis']}）")
    if est.get("history_expect"):
        print(f"历史上    : {est['history_expect']}")
    print(f"超时上限  : {vp['timeout_min']} 分钟（到点自动收工，已落盘数据保留）")
    print("提醒      :")
    for line in est["cautions"]:
        print(f"  · {line}")
    print(SEP)
    return 0


def build_estimate(vp: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    """预估的核心计算。只读 state.json 与磁盘，不起浏览器、不发网络请求。"""
    requested = int(vp["requested"])
    max_com_total = requested * int(vp["max_comments"]) if vp["comments"] else 0

    # 同类历史：同平台 + 同类型 + 真拿到数据 + 有耗时记录
    same = [h for h in st["history"]
            if h.get("platform") == vp["platform"] and h.get("type") == vp["type"]
            and h.get("status") in ("成功", "部分完成")
            and int(h.get("contents") or 0) > 0 and float(h.get("duration_s") or 0) > 0]
    # 同平台（不限类型）当补充样本
    loose = [h for h in st["history"]
             if h.get("platform") == vp["platform"]
             and h.get("status") in ("成功", "部分完成")
             and int(h.get("contents") or 0) > 0 and float(h.get("duration_s") or 0) > 0]

    cautions: List[str] = []
    history_expect = ""
    if vp.get("platform_note"):
        cautions.append(vp["platform_note"])

    if same or loose:
        pool = same or loose
        n = len(pool)
        sec_per_item = sum(float(h["duration_s"]) for h in pool) / max(1, sum(int(h["contents"]) for h in pool))
        est_sec = sec_per_item * requested
        # 平台达成率：历史上要 100 条实际给几条
        got = sum(int(h.get("contents") or 0) for h in pool)
        want = sum(int(h.get("requested") or 0) for h in pool)
        ratio = (got / want) if want else 1.0
        basis = (f"按本机 {n} 次同平台实测（平均每条内容 {sec_per_item:.1f} 秒，含评论在内）反推"
                 if same else
                 f"按本机 {n} 次同平台其它类型任务的实测速度反推，参考价值略低")
        confidence = "高" if (same and n >= 3) else ("中" if (same or n >= 2) else "低")
        if ratio < 0.95 and want:
            history_expect = (f"你这种要法，历史上平均只拿到 {round(ratio * 100)}%"
                              f"（要 {want} 条，实得 {got} 条）")
            cautions.append(f"平台上不保证给够：请求 {requested} 条，按历史大概率只出 "
                            f"约 {int(requested * ratio)} 条。想凑够就多要一些，或换更宽的词。")
    else:
        # 一条历史都没有：给保守粗估（内容 3 秒/条 + 评论 1.5 秒/条，冷启动 40 秒）
        sec_per_item = 3.0
        est_sec = 40 + requested * sec_per_item + max_com_total * 1.5
        basis = "本机没有该平台的实测记录，只能按保守经验粗估，不保证"
        confidence = "低"
        cautions.append("这个估算没有本机实测支撑，实际可能差一倍以上。")

    if vp["comments"] and max_com_total > 0:
        cautions.append(f"评论是大头：最多 {max_com_total} 条评论，评论阶段往往比采内容慢很多。"
                        f"只想快速试水就把 --max-comments 调小。")
    if requested >= 500:
        cautions.append("要量超过 500 条：单次长时间跑，平台中途可能限流。"
                        "建议拆成几个小任务（batch），而不是一次要几千条。")
    if vp["retries"] > 0:
        cautions.append(f"允许自动重试 {vp['retries']} 次：万一遇到网络问题，实际耗时会比估算长。")

    # 登录态（只看本地有没有票，不发网络请求，免得预估还要等半天）
    ok_local, login_msg = _login_quick(vp["platform"])
    if not ok_local:
        cautions.append(f"登录：{login_msg} → 真跑前必须先补票，否则跑到一半就失败。")

    # 现在是不是有任务占着
    lk = read_lock()
    if lk is not None:
        cautions.append(f"现在本机有任务在跑（{describe_lock(lk)}），"
                        f"你得等它跑完才能开始，估算的耗时不包含这段等待。")

    if est_sec > vp["timeout_min"] * 60:
        cautions.append(f"⚠️ 估算耗时超过本次超时上限 {vp['timeout_min']} 分钟，"
                        f"到点会被自动收工。想跑完就加 --timeout-min。")

    eta_text = f"约 {human_duration(est_sec)}"
    return {
        "ok": True,
        "status": "预估完成",
        "name": vp["name"], "platform": vp["platform"], "type": vp["type"],
        "platform_note": vp.get("platform_note", ""),
        "keywords": vp["keywords"],
        "requested": requested,
        "comments_on": bool(vp["comments"]),
        "max_comments_total": max_com_total,
        "eta_seconds": round(est_sec),
        "eta_text": eta_text,
        "sec_per_item": round(sec_per_item, 2),
        "confidence": confidence,
        "confidence_label": confidence + "把握",
        "basis": basis,
        "history_expect": history_expect,
        "timeout_min": vp["timeout_min"],
        "cautions": cautions,
        "note": "预估仅供参考，不保证：平台限流、登录态、网络都会改变实际耗时。",
    }


def _login_quick(platform: str) -> Tuple[bool, str]:
    """极轻量登录检查：只看本地有没有登录数据目录和 Cookies 文件，不发任何网络请求。

    为什么不复用 check_login_state：那个会拿票去平台接口问一句（要联网、要几秒），
    而"预估"这种事不该等那么久，也不该因为没网就报错。
    """
    base = ROOT / "browser_data"
    for c in (base / f"cdp_{platform}_user_data_dir", base / f"{platform}_user_data_dir"):
        if c.is_dir():
            d = c / "Default"
            if d.is_dir() and ((d / "Cookies").is_file() or (d / "Network" / "Cookies").is_file()):
                return True, f"本地有登录数据（{c.name}），票是否还有效要真跑才知道"
            return False, f"{c.name} 里没找到 Cookies，登录态大概率失效"
    return False, f"没找到 {platform} 的登录数据目录，从没登录过"


# ----------------------------------------------------------------------------
# 断点续跑（resume）：按关键词续跑 + ID 去重
# ----------------------------------------------------------------------------
# 为什么只能「按关键词」一个个补：
#   原码的 --start 是**全局单值**（config.START_PAGE 只有一份），一次运行里所有关键词
#   共用同一个起始页。上次跑到一半，各关键词停在不同页，只能一个词一个词单独补抓，
#   每个词各用各的起始页。
#
# 为什么必须按 ID 去重：
#   --start 是「把抓取窗口整体往后平移」，不是「精准跳到第 N 页」。平台每次返回的
#   边界会差一两条（内容被删、置顶变化、分页抖动），断点页附近必然重采到旧数据。
#   重采到的行属于旧数据，不能算新数据，必须按内容 ID 丢掉。
#
# 为什么 Excel 直接拒绝：
#   Excel 是整份覆盖写，两份 xlsx 合并要么覆盖、要么错位，容易出静默错误。
#   宁可让用户换 jsonl/json/csv，也不做这种不可靠的合并。
CONTENT_ID_FIELDS = {
    "dy": "aweme_id", "bili": "video_id", "ks": "video_id",
    "wb": "note_id", "tieba": "note_id", "xhs": "note_id",
    "zhihu": "content_id",
}
COMMENT_ID_FIELD = "comment_id"

# 每页条数（照抄原码各平台自己的 limit_count）——只在日志里没有页码行时用来
# 「保守推算翻了几页」。这是推算不是事实，结论里必须写明依据强弱。
PAGE_SIZE = {"dy": 10, "bili": 20, "ks": 20, "wb": 10,
             "tieba": 10, "zhihu": 20, "xhs": 20}


def _resume_find_log(task_dir: Path, attempt_dir: Path) -> Optional[Path]:
    """找这次尝试对应的运行日志 run_attempt_N.log。"""
    num = re.sub(r"\D", "", attempt_dir.name) or ""
    cand = task_dir / f"run_attempt_{num}.log"
    if cand.is_file():
        return cand
    logs = sorted(task_dir.glob("run_attempt_*.log"))
    return logs[-1] if logs else None


def _resume_page_from_log(log_text: str, keyword: str) -> Tuple[Optional[int], str]:
    """从日志里找这个关键词最后抓到第几页。返回 (最后页, 依据说明)。

    依据强弱分三档，必须如实告知：
      1) 日志有「keyword: X, page: N」 → 精确（dy/bili/wb/xhs 有这行）
      2) 只有「Skip page: N」 → 拿不到抓取页，不算依据（ks/tieba/zhihu 就是这样）
      3) 什么都没有 → 无依据
    """
    last: Optional[int] = None
    for line in log_text.splitlines():
        if "page" not in line.lower():
            continue
        if "skip" in line.lower():
            continue
        kw = _keyword_from_line(line)
        if not kw or kw != keyword:
            continue
        m = re.search(r"page[:：]\s*(\d+)", line)
        if not m:
            continue
        last = max(last or 0, int(m.group(1)))
    if last is not None:
        return last, "日志页码（精确）"
    if re.search(r"Skip page[:：]?\s*\d+", log_text):
        return None, "日志只有 Skip page（这个平台不打印抓取页码）"
    return None, "日志里没有页码信息"


def _resume_page_from_rows(n_rows: int, platform: str) -> Optional[int]:
    """拿已抓条数 ÷ 每页条数，保守推算翻了几页。"""
    size = PAGE_SIZE.get(platform) or 0
    if size <= 0 or n_rows <= 0:
        return None
    return -(-n_rows // size)


def build_resume_plan(task_dir: Path, st: Dict[str, Any],
                      want_attempt: Optional[int] = None,
                      count_override: Optional[int] = None,
                      from_scratch: bool = False) -> Dict[str, Any]:
    """读旧任务，算出「每个关键词抓到哪一步、该从第几页接着跑」。

    只读磁盘，不联网、不起浏览器。所有不确定的地方都写成 notes 明说，
    绝不假装知道断点在哪。
    """
    out: Dict[str, Any] = {"ok": False, "error": "", "notes": [], "plan": [],
                           "rows": [], "comment_rows": [], "platform": "",
                           "name": task_dir.name, "count": 0, "fmt": "jsonl",
                           "params": {}}
    att = pick_attempt(task_dir, want_attempt)
    if att is None:
        out["error"] = f"任务目录里没有 attempt_* 子目录，没法找旧数据：{task_dir}"
        return out

    # 旧参数：params.json 优先，其次 state.json 历史
    old: Dict[str, Any] = {}
    pj = task_dir / "params.json"
    if pj.is_file():
        try:
            old = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    hist = next((h for h in reversed(st.get("history") or [])
                 if str(h.get("task_dir") or "")
                 and Path(str(h["task_dir"])).name == task_dir.name), {})
    out["params"] = old
    platform = str(old.get("platform") or hist.get("platform") or "").strip()
    if not platform:
        out["error"] = ("看不出这个任务是哪个平台（params.json 和 state.json 历史都没有），"
                        "没法续跑。请直接重新跑一个新任务。")
        return out
    out["platform"] = platform
    out["name"] = str(old.get("name") or hist.get("name") or task_dir.name)

    old_type = str(old.get("type") or hist.get("type") or "search").strip().lower()
    if old_type in ("detail", "creator"):
        out["error"] = (f"这是 {old_type} 类型任务（按 ID 抓，不是按关键词翻页），"
                        "没有「抓到第几页」这回事，没法按关键词续跑。\n"
                        "  ↳ 直接再跑一次同样的命令即可：平台会按 ID 覆盖写，不会真的重复。")
        return out

    keys = old.get("keywords") or [k for k in str(hist.get("keywords") or "").split(",") if k.strip()]
    keys = [str(k).strip() for k in keys if str(k).strip()]
    if not keys:
        out["error"] = "看不出旧任务用了哪些关键词（params.json 和 state.json 历史都没有）。"
        return out

    # 旧数据文件：只认 contents；有 Excel 就拒绝
    srcs = _kind_sources(att, "contents")
    if not srcs:
        out["error"] = (f"这次尝试（{att.name}）里没找到内容数据文件，"
                        "不知道上次抓到哪一步，没法续跑。")
        return out
    fmts = {p.suffix.lower() for p in srcs}
    if ".xlsx" in fmts:
        out["error"] = ("这个任务的数据是 Excel。Excel 是整份覆盖写，两份合并容易出错，"
                        "续跑只支持 jsonl/json/csv。\n"
                        "  ↳ 换格式重跑一次：run -p ... -f jsonl，之后再续跑；"
                        "原来的 Excel 留着看，不会动它。")
        return out
    fmt = "csv" if ".csv" in fmts else ("json" if (".json" in fmts and ".jsonl" not in fmts) else "jsonl")
    out["fmt"] = fmt

    rows: List[Dict[str, Any]] = []
    for p in srcs:
        rows.extend(read_data_rows(p, "contents"))
    rows = [r for r in rows if "__解析失败的行__" not in r]
    out["rows"] = rows
    if rows and not any("source_keyword" in r for r in rows):
        out["error"] = ("旧数据里没有 source_keyword 字段，分不清每个关键词各自抓到哪一步，"
                        "没法按关键词续跑。\n  ↳ 直接重新跑一个新任务即可。")
        return out
    if not rows:
        out["notes"].append("旧任务一条内容都没落盘 → 所有关键词都从头补抓（不是续跑）。")

    # 每个关键词已经有多少条（同一 ID 只算一次）
    idf = CONTENT_ID_FIELDS.get(platform) or ""
    if not idf:
        out["notes"].append(f"平台 {platform} 没有登记内容 ID 字段，这次没法去重（会按原样合并）。")
    by_kw: Dict[str, int] = {}
    seen_ids = set()
    for r in rows:
        kw = str(r.get("source_keyword") or "").strip()
        rid = str(r.get(idf) or "") if idf else ""
        if rid:
            marker = (kw, rid)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
        by_kw[kw] = by_kw.get(kw, 0) + 1

    try:
        count = int(count_override or old.get("count") or hist.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        out["error"] = ("看不出旧任务每个关键词要多少条（params.json 里没有，--count 也没给），"
                        "没法判断哪些词已经抓够了。请补 --count。")
        return out
    out["count"] = count

    log_path = _resume_find_log(task_dir, att)
    log_text = ""
    if log_path is not None:
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
    if not log_text:
        out["notes"].append("找不到旧任务的运行日志 → 断点页只能按已抓条数保守推算。")

    for kw in keys:
        got = by_kw.get(kw, 0)
        last_page, basis = _resume_page_from_log(log_text, kw)
        if last_page is None:
            est = _resume_page_from_rows(got, platform)
            if est:
                last_page = est
                basis = (f"按已抓 {got} 条 ÷ 每页 {PAGE_SIZE.get(platform)} 条保守推算"
                         f"（{basis}）")
        # 平台明确说过这个词没更多了：再补也是白跑
        exhausted = bool(re.search(r"No more \w+ for '" + re.escape(kw) + "'", log_text))
        done = (got >= count) and not from_scratch
        if done:
            action = "已完成"
        elif exhausted:
            action = "平台已无更多"
        else:
            action = "补抓"
        start = 0
        if action == "补抓" and not from_scratch and last_page is not None:
            start = last_page + 1
        if action == "补抓" and start == 0 and not from_scratch:
            basis += "；推不出断点页 → 从第 1 页重抓，靠 ID 去重兜底"
        out["plan"].append({
            "keyword": kw, "got": got, "need": count, "action": action,
            "last_page": last_page, "page_basis": basis, "start_page": start,
        })

    out["ok"] = True
    return out


def _merge_rows(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]],
                id_field: str) -> Tuple[List[Dict[str, Any]], int]:
    """旧 + 新 按 ID 去重合并（旧数据优先保留原顺序）。返回 (合并结果, 丢掉几条重复)。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    dropped = 0
    for r in list(old_rows) + list(new_rows):
        key = str(r.get(id_field) or "") if id_field else ""
        if key:
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out.append(r)
    return out, dropped


def _write_rows(path: Path, rows: List[Dict[str, Any]]) -> None:
    """把行列表按扩展名写成 jsonl / json / csv（utf-8，不丢字段）。"""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return
    if suffix == ".json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        return
    cols: List[str] = []
    seen_cols = set()
    for r in rows:
        for k in r:
            if k not in seen_cols:
                seen_cols.add(k)
                cols.append(k)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else
                            r[k] if isinstance(r[k], (str, int, float, bool))
                            else json.dumps(r[k], ensure_ascii=False, default=str))
                        for k in cols})


def cmd_resume(args: argparse.Namespace) -> int:
    st = load_state()
    jm = _json_mode(args)
    task_dir, tried = resolve_task_dir(str(getattr(args, "task", "") or ""), st)
    if task_dir is None:
        msg = ("找不到要续跑的任务目录。找过的地方：\n  - " + "\n  - ".join(tried)
               + "\n  ↳ 用 status 看一下任务名，--task 给名字片段也行。")
        if jm:
            emit_json({"ok": False, "status": "预检失败", "reason": msg})
        else:
            print(f"⛔ {msg}")
        return 1

    with _stdout_to_stderr(jm):
        plan = build_resume_plan(task_dir, st, getattr(args, "attempt", None),
                                 getattr(args, "count", None),
                                 bool(getattr(args, "from_scratch", False)))
        if not plan["ok"]:
            if jm:
                emit_json({"ok": False, "status": "预检失败", "reason": plan["error"],
                           "task_dir": str(task_dir)})
            else:
                print(f"{SEP}\n【续跑】{task_dir.name}\n{SEP}")
                print(f"⛔ 没法续跑：\n  {plan['error']}")
            return 1

        old_params = plan["params"]
        platform = plan["platform"]
        fmt = plan["fmt"]
        idf = CONTENT_ID_FIELDS.get(platform) or ""
        todo = [x for x in plan["plan"] if x["action"] == "补抓"]

        # 后台跑：续跑可能好几小时，甩给后台工人，主进程立刻返回（进度用 status 看）。
        # 先算清账再决定要不要甩：没什么可抓的就别白拉一个后台进程。
        # 后台工人会原样重跑这条命令（只摘掉 --detach），到那边会重新算一遍账。
        if todo and getattr(args, "detach", False):
            return _detach_launch(sanitize_name(f"{plan['name']}_续跑"), platform,
                                  [x["keyword"] for x in todo],
                                  len(todo) * int(plan["count"] or 0), jm,
                                  match_history=False, noun="续跑")

        print(f"{SEP}\n【续跑】{plan['name']}｜平台 {platform}｜{now_str()}\n{SEP}")
        print(f"旧任务目录: {task_dir}")
        print(f"每个关键词要: {plan['count']} 条；旧数据格式 {fmt}")
        for n in plan["notes"]:
            print(f"  ⚠ {n}")
        print("逐词账本:")
        for x in plan["plan"]:
            print(f"  · 「{x['keyword']}」已有 {x['got']}/{x['need']} 条 → {x['action']}"
                  + (f"，从第 {x['start_page']} 页接着跑" if x["action"] == "补抓" and x["start_page"]
                     else "")
                  + (f"；未知页码" if x["action"] == "补抓" and not x["start_page"] and not getattr(args, "from_scratch", False) else "")
                  + f"｜依据：{x['page_basis']}")

        if not todo:
            print("\n所有关键词都已经够数或平台已无更多，不用再跑。")
            print("  ↳ 想强制重跑加 --from-scratch。")

        # 逐关键词串行补抓（每次只传一个关键词 + 它自己的起始页）
        new_rows: List[Dict[str, Any]] = []
        runs: List[Dict[str, Any]] = []
        canceled = False
        for item in todo:
            if cancel_targets_me():
                canceled = True
                print("\n  发现喊停纸条 → 不再开始新的关键词。")
                break
            p = {
                "platform": platform,
                "keywords": item["keyword"],
                "count": plan["count"],
                "format": fmt,
                "type": "search",
                "comments": old_params.get("comments"),
                "max_comments": old_params.get("max_comments") or None,
                "media": old_params.get("media"),
                "headless": old_params.get("headless"),
                "auto_import": None,
                "retries": None,
                "wait_lock": getattr(args, "wait_lock", None),
                "timeout_min": getattr(args, "timeout_min", None),
                "probe": getattr(args, "probe", None),
                "start_page": item["start_page"],
                "name": plan["name"],
            }
            try:
                vp = validate_params(p, st)
            except ParamError as e:
                print(f"⛔ 关键词「{item['keyword']}」参数不合法：{e}")
                runs.append({"keyword": item["keyword"], "status": "预检失败",
                             "reason": str(e), "contents": 0})
                continue
            print(f"\n>>> 补抓「{item['keyword']}」：从第 "
                  f"{item['start_page'] or 1} 页起，目标 {plan['count']} 条")
            r = run_task(vp, st)
            got_new = 0
            ad = r.get("attempt_dir")
            if ad and Path(str(ad)).is_dir():
                for pth in _kind_sources(Path(str(ad)), "contents"):
                    grp = read_data_rows(pth, "contents")
                    got_new += len([g for g in grp if "__解析失败的行__" not in g])
                    new_rows.extend(g for g in grp if "__解析失败的行__" not in g)
            runs.append({"keyword": item["keyword"], "status": r.get("status", ""),
                         "reason": r.get("reason", ""), "contents": r.get("contents", 0),
                         "new_rows": got_new, "task_dir": r.get("task_dir", ""),
                         "attempt_dir": ad or "", "start_page": item["start_page"]})

        # 合并（旧 + 新，按内容 ID 去重）
        merged, dropped = _merge_rows(plan["rows"], new_rows, idf)

        # 评论也合并一份（有就合，没有就跳过）
        old_cm: List[Dict[str, Any]] = []
        # 旧评论只从旧 attempt 里拿；新评论从各关键词的新 attempt 里拿
        old_att = pick_attempt(task_dir, getattr(args, "attempt", None))
        if old_att is not None:
            for pth in _kind_sources(old_att, "comments"):
                old_cm.extend(read_data_rows(pth, "comments"))
        new_cm: List[Dict[str, Any]] = []
        for run in runs:
            ad = run.get("attempt_dir")
            if not ad:
                continue
            for pth in _kind_sources(Path(ad), "comments"):
                new_cm.extend(read_data_rows(pth, "comments"))
        merged_cm, dropped_cm = _merge_rows(old_cm, new_cm, COMMENT_ID_FIELD)

        # 落一份合并档（新目录，不覆盖任何旧文件）
        out_dir = make_task_dir({"name": sanitize_name(f"{plan['name']}_续跑"),
                                 "platform": platform, "is_login_task": False})
        stamp = datetime.now().strftime("%Y-%m-%d")
        files: List[str] = []
        cp = out_dir / f"resume_contents_{stamp}.{fmt}"
        _write_rows(cp, merged)
        files.append(str(cp))
        cc = None
        if merged_cm:
            cc = out_dir / f"resume_comments_{stamp}.{fmt}"
            _write_rows(cc, merged_cm)
            files.append(str(cc))
        summary = {
            "ts": now_str(), "from_task": str(task_dir), "out_dir": str(out_dir),
            "platform": platform, "count_per_keyword": plan["count"],
            "old_contents": len(plan["rows"]), "new_contents": len(new_rows),
            "merged_contents": len(merged), "duplicates_dropped": dropped,
            "merged_comments": len(merged_cm), "comment_duplicates_dropped": dropped_cm,
            "per_keyword": plan["plan"], "runs": runs, "canceled": canceled,
            "files": files,
        }
        try:
            (out_dir / "resume_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except OSError:
            pass

        # 结论：有新增数据就至少算「部分完成」，别假装全成功
        ok_runs = [r for r in runs if r["status"] in ("成功", "部分完成")]
        gained = max(0, len(merged) - len(plan["rows"]))
        if not todo:
            status = "无事可做"
        elif canceled:
            status = "已取消"
        elif len(ok_runs) == len(runs) and gained > 0:
            status = "续跑完成"
        elif gained > 0:
            status = "部分完成"
        else:
            status = "失败"

        print(f"\n{SEP}\n【续跑结果】\n{SEP}")
        print(f"状态     : {status}")
        print(f"内容对账 : 旧 {len(plan['rows'])} 条 + 新抓 {len(new_rows)} 条 → 去重 {dropped} 条 → 合并 {len(merged)} 条")
        if merged_cm:
            print(f"评论对账 : 旧 {len(old_cm)} 条 + 新 {len(new_cm)} 条 → 去重 {dropped_cm} 条 → 合并 {len(merged_cm)} 条")
        for run in runs:
            print(f"  · 「{run['keyword']}」{run['status']}"
                  + (f"（{run['reason']}）" if run.get("reason") else "")
                  + f"，新数据 {run.get('new_rows', 0)} 条")
        print(f"合并文件 : {cp}")
        if cc:
            print(f"合并评论 : {cc}")
        print(f"输出目录 : {out_dir}")
        if dropped:
            print(f"说明     : 丢掉 {dropped} 条是断点页附近重采到的旧数据（按 {'ID ' + idf if idf else '文件名'} 判重），不是漏数据。")
        print(SEP)

        if jm:
            emit_json({"ok": status in ("续跑完成", "部分完成", "无事可做"), "status": status,
                       "platform": platform, "from_task": str(task_dir),
                       "out_dir": str(out_dir), "count_per_keyword": plan["count"],
                       "old_contents": len(plan["rows"]), "new_contents": len(new_rows),
                       "merged_contents": len(merged), "duplicates_dropped": dropped,
                       "merged_comments": len(merged_cm), "files": files,
                       "per_keyword": plan["plan"], "runs": runs, "notes": plan["notes"],
                       "canceled": canceled})
    return {"续跑完成": 0, "部分完成": 2, "无事可做": 0, "已取消": 2}.get(status, 1)


def cmd_batch(args: argparse.Namespace) -> int:
    st = load_state()
    jm = _json_mode(args)
    path = Path(args.file)

    def _fail(msg: str) -> int:
        if jm:
            emit_json({"ok": False, "status": "预检失败", "reason": msg, "total": 0,
                       "ok_count": 0, "tasks": []})
        else:
            print(f"⛔ {msg}")
        return 1

    if not path.is_file():
        return _fail(f"批量文件不存在：{path}")
    try:
        # utf-8-sig：Windows 记事本存的文件开头可能带一个看不见的 BOM，用它读能兼容两种
        tasks = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return _fail(f"批量文件解析失败：{e}")
    if not isinstance(tasks, list) or not tasks:
        return _fail("批量文件应为非空 JSON 数组，如：[{\"platform\":\"bili\",\"keywords\":\"考研\",\"count\":20}, …]")
    if getattr(args, "detach", False):
        # 队列整段甩到后台：队列可能跑几个小时，前台死等没意义
        return _detach_launch(f"批量队列{len(tasks)}个", "batch", [], 0, jm,
                              match_history=False, noun="队列")
    global _LOCK
    results: List[Dict[str, Any]] = []
    n_ok = 0
    with _stdout_to_stderr(jm):
        # 整个队列一次性持锁，避免任务间隙被别的会话抢进来
        _LOCK = RunLock()
        try:
            lock_notes = _LOCK.acquire({"name": f"批量队列({len(tasks)}个)", "platform": "batch",
                                        "keywords": []}, wait_min=0)
        except LockBusy as e:
            return _fail(str(e))
        print(f"批量队列：共 {len(tasks)} 个任务，顺序执行（任务间隔 10 秒）")
        for note in lock_notes:
            print("  • " + note)
        print()
        try:
            for i, t in enumerate(tasks, 1):
                print(f"\n{'#' * 64}\n# 任务 {i}/{len(tasks)}\n{'#' * 64}")
                try:
                    vp = validate_params({**t, "name": t.get("name")}, st)
                except ParamError as e:
                    print(f"⛔ 参数校验未通过：{e}")
                    # 带上 params，汇总时才看得出是哪个平台的哪一条出了问题（不然只显示一个 ?）
                    results.append({"params": {"name": t.get("name") or "（没写名字）",
                                               "platform": str(t.get("platform", "?")),
                                               "requested": int(t.get("count", 0) or 0)},
                                    "status": "预检失败", "reason": str(e),
                                    "contents": 0, "comments": 0, "attempt": 0})
                    continue
                r = run_task(vp, st)
                results.append(r)
                if i < len(tasks):
                    time.sleep(10)
        finally:
            if _LOCK is not None:
                _LOCK.release()
                _LOCK = None
        print(f"\n{SEP}\n【批量汇总】\n{SEP}")
        for r in results:
            vp = r.get("params", {})
            icon = STATUS_ICON.get(r["status"], "❓")
            att = int(r.get("attempt") or 0)
            print(f"  {icon} {vp.get('name', r.get('name', '?'))}: {r['status']}"
                  + (f"（{r['reason']}）" if r.get("reason") else "")
                  + f" | 内容 {r.get('contents', 0)}/{r.get('requested', 0)} 条"
                  + (f" | 第 {att} 次出结果（重试 {att - 1} 次）" if att > 1 else " | 一次过"))
        n_ok = sum(1 for r in results if r["status"] in ("成功", "部分完成"))
        print(f"\n合计: {n_ok}/{len(results)} 个任务产出数据")
        # 没拿到数据的，按「三类问题」分开数，别混成一句"失败了几个"
        buckets: Dict[str, int] = {}
        for r in results:
            if r["status"] in ("成功", "部分完成"):
                continue
            k = reason_kind(r.get("reason", "")) or "network"
            buckets[k] = buckets.get(k, 0) + 1
        if buckets:
            print("没拿到数据的，按问题归一次类：")
            for k in ("not_logged_in", "no_data", "network", "other"):
                if buckets.get(k):
                    print(f"  · {kind_label(k)}：{buckets[k]} 个 → {kind_advice(k)}")
        print(SEP)
    if jm:
        payloads = [result_payload(r) for r in results]
        fail_kinds: Dict[str, int] = {}
        for p in payloads:
            if not p["ok"]:
                k = p["reason_kind"] or "network"
                fail_kinds[k] = fail_kinds.get(k, 0) + 1
        emit_json({"ok": n_ok == len(payloads), "status": "完成",
                   "total": len(payloads), "ok_count": n_ok,
                   "failed_by_kind": fail_kinds, "tasks": payloads})
    return 0 if n_ok == len(results) else (2 if n_ok else 1)


def scrub_login_identity(msg: str) -> str:
    """把登录态提示里的账号昵称/uid/目录名抹掉，只留"已登录/没登录"这种结论。

    为什么：这段文字会被 AI 客户端读进上下文、也可能被贴进对话。账号昵称和 uid 属于
    登录信息，本机目录名属于本机路径，都不该外流。
    """
    s = re.sub(r"已登录[：:]\s*[^（(｜|]*([（(]uid\s*\d+[）)])?", "已登录", msg)
    s = re.sub(r"｜cdp_[a-z]+_user_data_dir", "", s)
    s = re.sub(r"｜[a-z]+_user_data_dir", "", s)
    s = re.sub(r"已找到登录数据目录\s*\S+", "已找到登录数据目录", s)
    return s.strip()


def cmd_login(args: argparse.Namespace) -> int:
    st = load_state()
    jm = _json_mode(args)
    check_only = bool(getattr(args, "check_only", False))
    raw = str(args.platform).strip()
    platform = PLATFORM_ALIASES.get(raw.lower(), PLATFORM_ALIASES.get(raw))

    def _out(ok: bool, status: str, msg: str, need_user: bool = False,
             extra: Optional[Dict[str, Any]] = None) -> None:
        msg = scrub_login_identity(msg)
        if jm:
            d = {"ok": ok, "platform": platform or raw, "logged_in": status == "已登录",
                 "status": status, "message": msg, "need_user_action": need_user,
                 "checked_only": check_only}
            if platform and PLATFORM_NOTES.get(platform):
                d["platform_note"] = PLATFORM_NOTES[platform]
            if extra:
                d.update(extra)
            emit_json(d)
        else:
            print(("✅ " if ok else ("ℹ️  " if status == "需要扫码" else "⛔ ")) + msg)

    if platform is None:
        _out(False, "参数错误", f"不支持的平台「{raw}」。支持：bili/dy/ks/wb/tieba/zhihu（xhs 已不推荐）")
        return 1
    ok, msg = check_login_state(platform)
    if ok:
        _out(True, "已登录", f"{platform} 登录态正常（{msg}）。无需再扫码，直接 run 即可。")
        return 0
    if check_only:
        _out(False, "需要扫码", f"{platform} {msg}。要补票得有人扫码：请用户在浏览器里登录好，"
                                f"或跑一次 login（会弹二维码，10 分钟内手机扫一下）。",
             need_user=True)
        return 2
    if jm:
        # --json + 真登录：会弹浏览器等人扫码，不适合 AI 同步等；让它走 check-only
        _out(False, "需要扫码", f"{platform} {msg}。加 --json 时不弹浏览器，请改用 --check-only 或让用户手动登录。",
             need_user=True)
        return 2
    print(f"ℹ️  {platform} {msg}")
    print(f"即将启动登录流程：会弹出浏览器并显示二维码，请在 10 分钟内用手机 App 扫码"
          f"（扫完会自动继续，不用等）。\n")
    vp = validate_params({
        "platform": platform, "keywords": "login", "count": 1, "format": "jsonl",
        "type": "search", "comments": False, "media": False, "headless": False,
        "is_login_task": True,
    }, st)
    r = run_task(vp, st)
    if r["status"] in ("成功", "部分完成"):
        ok2, msg2 = check_login_state(platform)
        print(f"\n{'✅ 登录成功，登录态已保存：' + msg2 if ok2 else '⚠️ 进程已结束，但未检测到登录数据，请重试扫码'}")
        return 0 if ok2 else 1
    print(f"\n⛔ 登录未完成：{r['reason']}")
    print("  ↳ 三条补票路，挑一条（都不是 agent_crawl.py 的参数，要跑同目录的 edge_login_sync.py）：")
    print("     1) 桌面 Edge 点「爬虫登录票导出」扩展按钮，然后：")
    print("        .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --import-json all latest")
    print("     2) F12 → 网络 → 右键『复制为 cURL』存成 txt，然后：")
    print(f"        .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --import {platform} --curl-file 文件.txt")
    print(f"     3) 再扫一次码：.venv\\Scripts\\python.exe agent_crawler\\agent_crawl.py login --platform {platform}")
    return 1


def _jsonl_lines(p: Path) -> int:
    """快速数 jsonl 行数（二进制数换行，不解析内容；大数据量也不卡）。"""
    n = 0
    try:
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                n += chunk.count(b"\n")
    except OSError:
        return 0
    return n


def estimate_progress(lk: Dict[str, Any]) -> Optional[str]:
    """给"正在跑的那个任务"估个进度。只读磁盘，不碰 run.lock、不加锁。

    思路：从锁上拿到任务名 → 找到它的任务目录和当前 attempt → 数已落盘的 jsonl 行数，
    配上 params.json 里的"请求条数"算百分比，按已跑时间线性外推剩余时间。
    样本太少（不到 3 条 / 不到 90 秒）一律不估——那时候的速率全是冷启动噪声。
    """
    if not isinstance(lk, dict):
        return None
    task = str(lk.get("task") or "").strip()
    if not task or task.startswith("批量队列"):
        return None                      # 队列里任务名一直在换，估出来是错的
    cands = [p for p in TASKS_ROOT.glob(task + "_*") if p.is_dir()]
    if not cands:
        return None
    task_dir = max(cands, key=lambda p: p.stat().st_mtime)
    try:
        pj = json.loads((task_dir / "params.json").read_text(encoding="utf-8"))
    except Exception:
        return None                      # 老任务目录没留参数，宁可不估也不错估
    requested = int(pj.get("requested") or 0)
    if requested <= 0:
        return None
    attempts = [p for p in task_dir.glob("attempt_*") if p.is_dir()]
    if not attempts:
        return "刚开始：任务目录已建，还没开始落盘"
    attempt_dir = max(attempts, key=lambda p: int(p.name.split("_")[-1] or 0))
    got = com = 0
    first_ts = 0.0
    for p in attempt_dir.rglob("*.jsonl"):
        kind = classify_file(p)
        if kind not in ("contents", "comments"):
            continue
        n = _jsonl_lines(p)
        try:
            born = p.stat().st_ctime      # Windows 上是创建时间：第一条数据落盘的时刻
        except OSError:
            born = 0.0
        if kind == "contents":
            got += n
            if born and (not first_ts or born < first_ts):
                first_ts = born
        else:
            com += n
    try:
        started = float(lk.get("started_ts") or LOCK_FILE.stat().st_mtime)
    except OSError:
        return None
    now = time.time()
    elapsed = max(0.0, now - started)
    # 速率只从"第一条数据落盘"算起：浏览器冷启动那 30~60 秒不是采集速度，
    # 算进去会把剩多久估成两倍以上。
    span = (now - first_ts) if first_ts else elapsed
    pct = min(100, round(got * 100.0 / requested))
    if got >= requested:
        # 数据已经采够了（收尾/采评论阶段）：这时候跟用户谈"速率"没意义，先说清状态
        if pj.get("comments") and com == 0:
            return (f"进度：内容已够 {got}/{requested} 条，正在采评论"
                    f"（已跑 {human_duration(elapsed)}）")
        tail = f"，评论 {com} 条" if com else ""
        return f"进度：内容已够 {got}/{requested} 条（{pct}%）{tail}，正在收尾"
    if elapsed < PROGRESS_MIN_ELAPSED:
        return (f"进度：已跑 {human_duration(elapsed)}，落盘 {got}/{requested} 条（{pct}%）"
                f"｜样本太少，等跑过 1 分半再估剩余时间")
    if got < PROGRESS_MIN_SAMPLES or span < PROGRESS_MIN_ELAPSED / 2:
        return (f"进度：已跑 {human_duration(elapsed)}，落盘 {got}/{requested} 条"
                f"｜数据刚起来（{human_duration(span)}），还估不出速率")
    rate = got / span                     # 条/秒（只算真正在采集的这段时间）
    eta = (requested - got) / rate if rate > 0 else 0
    return (f"进度估算：内容 {got}/{requested} 条（{pct}%），已跑 {human_duration(elapsed)}，"
            f"按当前速度约还需 {human_duration(eta)}（线性估算，仅供参考）")


def _count_kind_fast(attempt_dir: Path, kind: str) -> int:
    """数一次尝试里某类数据有多少条。只用文件名 + 行数，不读 Excel，快。

    同一批数据可能同时有 jsonl 和 csv，按格式分组取最大值，避免双份重复计数。
    """
    by_fmt: Dict[str, int] = {}
    for p in _kind_sources(attempt_dir, kind):
        fmt = p.suffix.lower().lstrip(".")
        if fmt in ("xlsx", "xls"):
            n = max(0, (count_excel(p) or {}).get(kind) or 0)
        else:
            n = max(0, count_records(p))
        if n:
            by_fmt[fmt] = by_fmt.get(fmt, 0) + n
    return max(by_fmt.values(), default=0)


def task_detail(task: str, st: Dict[str, Any], lk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把一个任务的底细全翻出来：参数、每次尝试各拿到几条、现在到第几条。

    为什么要有：跑长任务/批量时，最常问的是「这个任务到底跑到哪儿了」。
    只读磁盘，不加锁，不动任务目录里的任何东西。
    """
    task_dir, tried = resolve_task_dir(task, st)
    if task_dir is None:
        return {"ok": False, "reason": "找不到任务目录", "tried": tried, "query": task}

    pj: Dict[str, Any] = {}
    try:
        pj = json.loads((task_dir / "params.json").read_text(encoding="utf-8"))
    except Exception:
        pj = {}
    requested = int(pj.get("requested") or 0)

    attempts: List[Dict[str, Any]] = []
    for d in sorted((p for p in task_dir.glob("attempt_*") if p.is_dir()),
                    key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        n = int(re.sub(r"\D", "", d.name) or 0)
        contents = _count_kind_fast(d, "contents")
        comments = _count_kind_fast(d, "comments")
        mtime = 0.0
        try:
            mtime = d.stat().st_mtime
        except OSError:
            pass
        # 日志在任务目录下的 run_attempt_N.log（不在 attempt_N\ 里面）
        log = task_dir / f"run_attempt_{n}.log"
        attempts.append({
            "attempt": n,
            "dir": str(d),
            "contents": contents,
            "comments": comments,
            "log_path": str(log) if log.exists() else "",
            "last_write": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "",
            "has_data": contents > 0 or comments > 0,
        })

    # 现在到第几条：以"编号最大那次"为准（重试过的话前面的都是废的）
    latest = attempts[-1] if attempts else None
    got = latest["contents"] if latest else 0
    com = latest["comments"] if latest else 0
    pct = min(100, round(got * 100.0 / requested)) if requested else 0

    # 是不是此刻正在跑（锁上登记的任务名 / 目录路径对上）
    running = False
    lock_task = str((lk or {}).get("task") or "") if isinstance(lk, dict) else ""
    if lock_task and not lock_task.startswith("批量队列"):
        running = (lock_task == pj.get("name")) or bool(lk and str(lk.get("task_dir") or "") == str(task_dir))
        if not running:
            cands = [p.name for p in TASKS_ROOT.glob(lock_task + "_*") if p.is_dir()] if TASKS_ROOT.is_dir() else []
            running = task_dir.name in cands
    # 光看锁上的名字不够：进程被强杀、崩溃、关机时锁文件会留在原地，
    # 于是死掉的任务会一直显示"在跑"，等状态的人永远等不到结果。必须确认 PID 还活着。
    died = False
    if running:
        holder_pid = int((lk or {}).get("pid") or 0)
        if not holder_pid or not _pid_alive(holder_pid):
            try:
                age_sec = time.time() - (task_dir / "params.json").stat().st_mtime
            except OSError:
                age_sec = 1e9
            if age_sec > 25:            # 刚点火的头几秒进程还在起跑，别急着判死
                running = False
                died = True
    progress = estimate_progress(lk) if (running and isinstance(lk, dict)) else ""

    hist = [h for h in (st.get("history") or [])
            if str(h.get("task_dir") or "") == str(task_dir)]
    tail = hist[-1] if hist else {}

    # 老任务可能没进 state.json（早期版本），但 report.md 里写了状态和参数——从那儿补
    rep = task_dir / "report.md"
    rep_status, rep_reason, rep_params = "", "", ""
    rep_got, rep_req = -1, -1
    if rep.exists():
        try:
            for ln in rep.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
                s = ln.strip()
                if s.startswith("- 状态:") and not rep_status:
                    v = s.split(":", 1)[1].strip()
                    m = re.match(r"^(.*?)（(.*)）$", v)
                    if m:
                        rep_status, rep_reason = m.group(1).strip(), m.group(2).strip()
                    else:
                        rep_status = v
                elif s.startswith("- 参数:") and not rep_params:
                    rep_params = s.split(":", 1)[1].strip()
                elif s.startswith("- 数量对账:") and rep_got < 0:
                    m = re.search(r"内容\s*(\d+)\s*/\s*请求\s*(\d+)", s)
                    if m:
                        rep_got, rep_req = int(m.group(1)), int(m.group(2))
        except OSError:
            pass
    status = str(tail.get("status") or rep_status or "")
    reason = str(tail.get("reason") or rep_reason or "")
    if died and not status:
        # 进程没了、也没有任何收尾结论 → 是被强杀/崩溃/关机带走的，不能算"还在跑"
        status = "中断"
        reason = "进程已不在（被强制结束、崩溃或关机），没有写出结论"
    if requested <= 0 and rep_req > 0:
        requested = rep_req                     # 参数文件缺了就用报告里记的"请求条数"
        pct = min(100, round(got * 100.0 / requested)) if requested else 0

    return {
        "ok": True,
        "query": task,
        "task_dir": str(task_dir),
        "name": str(pj.get("name") or task_dir.name),
        "platform": str(pj.get("platform") or ""),
        "type": str(pj.get("type") or ""),
        "keywords": list(pj.get("keywords") or []),
        "count_per_keyword": int(pj.get("count") or 0),
        "format": str(pj.get("format") or ""),
        "want_comments": bool(pj.get("comments")),
        "requested": requested,
        "running": running,
        "progress": progress,
        "attempt_count": len(attempts),
        "retries_done": max(0, len(attempts) - 1),
        "latest_attempt": latest["attempt"] if latest else 0,
        "contents": got,
        "comments": com,
        "percent": pct,
        "finished": bool(status) and not running,
        "status": status,
        "reason": reason,
        "reason_kind": tail.get("reason_kind") or reason_kind(reason),
        "params_from_report": rep_params,
        "attempts": attempts,
        "report": str(task_dir / "report.md") if (task_dir / "report.md").exists() else "",
        "tried": tried,
    }


def print_task_detail(d: Dict[str, Any]) -> None:
    """把 task_detail 的结果讲成人话。"""
    if not d.get("ok"):
        print(f"⛔ 找不到任务目录：{d.get('query')}。我按顺序找过这些地方：")
        for t in d.get("tried") or []:
            print(f"    - {t}")
        print('  ↳ 最稳的写法：--task "<任务名>"，或直接给 tasks\\ 下的绝对路径')
        return
    print(f"{SEP}\n【任务详情】{d['name']}\n{SEP}")
    print(f"目录     : {d['task_dir']}")
    params = f"{d['platform']}" if d.get("platform") else ""
    if not params:
        params = (f"（任务目录没留参数文件；报告里原话：{d['params_from_report']}）"
                  if d.get("params_from_report") else "（这个任务目录没留参数，参数看不到）")
    if d.get("type"):
        params += f"｜{d['type']}"
    if d.get("keywords"):
        params += "｜关键词 " + "、".join(d["keywords"])
    if d.get("count_per_keyword"):
        params += f"｜每词 {d['count_per_keyword']} 条（合计要 {d['requested']} 条）"
    if d.get("format"):
        params += f"｜格式 {d['format']}"
    if d.get("want_comments"):
        params += "｜带评论"
    print(f"当初怎么跑 : {params}")

    got, req = d["contents"], d["requested"]
    if d["attempt_count"] == 0:
        print("跑到哪了 : 还没开始落数据（任务目录建好了，但一次尝试都没产生）")
    else:
        line = f"跑到哪了 : 第 {d['latest_attempt']} 次尝试，内容 {got}"
        line += f"/{req} 条（{d['percent']}%）" if req else " 条"
        if d.get("want_comments"):
            line += f"，评论 {d['comments']} 条"
        print(line)
        if d["running"]:
            print(f"现在状态 : 正在跑{'（估算见下）' if d.get('progress') else ''}")
            if d.get("progress"):
                print("           " + d["progress"])
        elif d.get("status"):
            k = d.get("reason_kind") or ""
            print(f"现在状态 : {d['status']}" + (f"｜{kind_label(k)}" if k else "")
                  + (f"（{d['reason']}）" if d.get("reason") else ""))
            if k:
                print(f"该怎么办 : {kind_advice(k)}")
        else:
            print("现在状态 : 已结束，但历史里没有这条记录（可能是手工跑的中途中断）")

    if d["attempt_count"] > 1:
        print(f"\n重试情况 : 一共尝试 {d['attempt_count']} 次（重试 {d['retries_done']} 次）")
    elif d["attempt_count"] == 1:
        print("\n重试情况 : 只跑了 1 次，没重试")

    if d["attempts"]:
        print("\n每次尝试各拿到多少：")
        for a in d["attempts"]:
            mark = "✓" if a["has_data"] else "✗"
            extra = f"评论 {a['comments']} 条" if a["comments"] else ""
            print(f"  {mark} 第 {a['attempt']} 次：内容 {a['contents']} 条"
                  + (f"｜{extra}" if extra else "")
                  + (f"｜最后写入 {a['last_write']}" if a["last_write"] else ""))
            if not a["has_data"]:
                print("      ↳ 这次一条没落盘（日志里有原因，见下）")
        empty = [a for a in d["attempts"] if not a["has_data"]]
        if empty:
            print(f"  日志位置（空的那些）：{empty[-1]['log_path'] or '（没留日志）'}")

    if d.get("report"):
        print(f"\n文字报告 : {d['report']}")
    print(SEP)


def cmd_status(args: argparse.Namespace) -> int:
    st = load_state()
    jm = _json_mode(args)
    procs = ps_find_crawler_procs(exclude_pids=_own_process_chain())
    groups = merge_proc_chains(procs) if procs else []
    browsers = ps_find_crawler_browsers()
    lk = read_lock()

    # 只查某一个任务：--task 给了就只看它，不灌一屏环境信息
    if getattr(args, "task", None):
        d = task_detail(args.task, st, lk)
        if jm:
            emit_json(d)
        else:
            print_task_detail(d)
        return 0 if d.get("ok") else 1

    lock_info: Dict[str, Any] = {"held": lk is not None}
    progress_text = ""
    if lk is not None:
        holder = int(lk.get("pid") or 0) if isinstance(lk, dict) else 0
        try:
            age_min = (time.time() - LOCK_FILE.stat().st_mtime) / 60
        except OSError:
            age_min = -1
        alive = _pid_alive(holder) if holder else False
        lock_info.update({"pid": holder, "alive": alive,
                          "age_min": round(age_min, 1),
                          "stale": age_min > LOCK_STALE_SEC / 60,
                          "task": str((lk or {}).get("task") or "") if isinstance(lk, dict) else "",
                          "describe": describe_lock(lk)})
        if alive:
            progress_text = estimate_progress(lk) or ""
    hist = st["history"][-10:][::-1]
    pending_cancel = read_cancel() is not None

    if jm:
        emit_json({
            "ok": True,
            "version": AGENT_VERSION,
            "now": now_str(),
            "running_groups": [
                {"root_pid": g["root"], "label": g["label"],
                 "members": [{"pid": pid, "cmd": cmd} for pid, cmd in g["members"]]}
                for g in groups
            ],
            "running_proc_count": len(procs),
            "leftover_browsers": [{"pid": pid, "cmd": cmd} for pid, cmd in browsers],
            "lock": lock_info,
            "progress": progress_text,
            "cancel_pending": pending_cancel,
            "history": [
                {"ts": h["ts"], "name": h["name"], "status": h["status"],
                 "contents": h["contents"], "requested": h["requested"],
                 "attempt": int(h.get("attempt") or 0),
                 "retries": max(0, int(h.get("attempt") or 0) - 1),
                 "reason": h.get("reason", ""), "task_dir": h.get("task_dir", ""),
                 "reason_kind": h.get("reason_kind") or reason_kind(h.get("reason", "")),
                 "reason_kind_label": kind_label(h.get("reason_kind") or reason_kind(h.get("reason", "")))}
                for h in hist
            ],
        })
        return 0

    print(f"{SEP}\n【当前环境】{now_str()}｜插件版本 {AGENT_VERSION}\n{SEP}")
    if procs:
        print(f"⚠️  有 {len(groups)} 个爬虫任务正在运行"
              f"（共 {len(procs)} 个进程；同一条启动链已归并，不是并发 {len(procs)} 个）：")
        for i, g in enumerate(groups, 1):
            print(f"    [{i}] 头进程 PID {g['root']}"
                  f"{'  平台/子命令: ' + g['label'] if g['label'] else ''}")
            for pid, cmd in g["members"]:
                print(f"        PID {pid}: {cmd[:140]}")
    else:
        print("  爬虫进程: 无（空闲）✓")
    if browsers:
        print(f"⚠️  有 {len(browsers)} 个残留爬虫浏览器：")
        for pid, cmd in browsers:
            m = re.search(r"user-data-dir=(\S+)", cmd)
            print(f"    PID {pid}: {m.group(1) if m else cmd[:100]}")
    else:
        print("  残留浏览器: 无 ✓")

    # 门口牌子：谁在跑
    if lk is None:
        print("  运行锁: 无人持有（可开跑）✓")
    else:
        print(f"  运行锁: 占用中 → {lock_info.get('describe', '')}")
        print(f"          进程{'还活着' if lock_info.get('alive') else '已不在'}"
              f"｜牌子 {lock_info.get('age_min', -1):.1f} 分钟没刷新"
              + ("（超过 30 分钟会被判失效）" if lock_info.get("stale") else ""))
        if not lock_info.get("alive"):
            print(f"          ↳ 像是残留，可手动删除 {LOCK_FILE}")
        elif progress_text:
            print("          ↳ " + progress_text)
    if pending_cancel:
        print("  喊停纸条: 已递出，等它下一次检查时收尾")

    print(f"\n【最近任务】（默认值: count={st['defaults']['count']} format={st['defaults']['format']} …）")
    if not hist:
        print("  （暂无历史）")
    for h in hist:
        icon = STATUS_ICON.get(h["status"], "❓")
        att = int(h.get("attempt") or 0)
        k = h.get("reason_kind") or reason_kind(h.get("reason", ""))
        print(f"  {icon} {h['ts']} {h['name']}: {h['status']} "
              f"内容 {h['contents']}/{h['requested']} 条"
              + (f"｜重试 {att - 1} 次" if att > 1 else "")
              + (f"｜{kind_label(k)}" if k else ""))
    print(SEP)
    return 0


def cmd_defaults(args: argparse.Namespace) -> int:
    st = load_state()
    jm = _json_mode(args)

    def _fail(msg: str) -> int:
        if jm:
            emit_json({"ok": False, "status": "参数错误", "reason": msg,
                       "available": sorted(DEFAULT_STATE["defaults"])})
        else:
            print(f"⛔ {msg}")
        return 1

    changed: Dict[str, Any] = {}
    if args.set:
        try:
            kv = dict(x.split("=", 1) for x in args.set)
        except Exception:
            return _fail("--set 格式应为 key=value，可多个，如 --set count=30 format=excel")
        for k, v in kv.items():
            if k not in DEFAULT_STATE["defaults"]:
                return _fail(f"未知默认项「{k}」。可用：{', '.join(DEFAULT_STATE['defaults'])}")
            cur = st["defaults"][k]
            try:
                st["defaults"][k] = type(cur)(json.loads(v)) if isinstance(cur, bool) else type(cur)(v)
            except Exception as e:
                return _fail(f"设置 {k}={v} 失败：{e}")
            changed[k] = st["defaults"][k]
        save_state(st)
    if jm:
        emit_json({"ok": True, "status": "已保存" if changed else "当前默认参数",
                   "defaults": st["defaults"], "changed": changed})
        return 0
    print(f"当前默认参数：")
    for k, v in st["defaults"].items():
        print(f"  {k} = {v}")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def add_run_flags(sp: argparse.ArgumentParser, with_detach: bool = True) -> None:
    sp.add_argument("-p", "--platform", required=True,
                    help="平台：bili/dy/ks/wb/tieba/zhihu（推荐）；xhs 反爬最狠，已不推荐、不保证。支持中文别名")
    sp.add_argument("-k", "--keywords", required=True, help="关键词，多个用英文逗号分隔（上限10个）")
    sp.add_argument("-n", "--count", type=int, default=None, help=f"每个关键词爬取条数（默认记忆值）")
    sp.add_argument("-f", "--format", default=None, help="输出格式：json/jsonl/csv/excel（默认记忆值）")
    sp.add_argument("-t", "--type", default=None, help="任务类型：search/detail/creator（默认记忆值）")
    sp.add_argument("--specified-id", dest="specified_id", default="", help="detail 类型：内容ID，逗号分隔")
    sp.add_argument("--creator-id", dest="creator_id", default="", help="creator 类型：创作者ID，逗号分隔")
    sp.add_argument("--name", default=None, help="任务名（默认取第一个关键词；目录名=任务名_平台_日期）")
    cm = sp.add_mutually_exclusive_group()
    cm.add_argument("--comments", dest="comments", action="store_true", default=None, help="抓取评论（默认记忆值）")
    cm.add_argument("--no-comments", dest="comments", action="store_false", help="不抓评论")
    sp.add_argument("--max-comments", dest="max_comments", type=int, default=None, help="每条内容的评论数（默认记忆值）")
    sp.add_argument("--media", dest="media", action="store_true", default=None, help="下载媒体文件")
    sp.add_argument("--no-media", dest="media", action="store_false", help="不下载媒体（默认）")
    sp.add_argument("--headless", dest="headless", action="store_true", default=None, help="无头模式")
    sp.add_argument("--no-headless", dest="headless", action="store_false", help="可见浏览器窗口（默认，反爬更稳）")
    sp.add_argument("--auto-import", dest="auto_import", action="store_true", default=None,
                    help="预检没过时自动导入扩展最新导出再试（默认开）")
    sp.add_argument("--no-auto-import", dest="auto_import", action="store_false",
                    help="关掉自动导入，预检失败就直接报错")
    sp.add_argument("--retries", type=int, default=None, help="基础设施类错误自动重试次数 0~3（默认记忆值）")
    sp.add_argument("--wait-lock", dest="wait_lock", type=float, default=None,
                    help="别人在跑时最多等几分钟（默认 0=直接退出，不抢）")
    sp.add_argument("--timeout-min", dest="timeout_min", type=int, default=None,
                    help="超时分钟数（默认自动：10+数量/10，开评论按 数量×每条评论上限/60 追加，上限180）")
    sp.add_argument("--passthrough", default="", help="透传给 main.py 的额外参数（高级用法）")
    sp.add_argument("--probe", dest="probe", action="store_true", default=None,
                    help="开跑前先探一次票（用泛词取 1 条真实数据；多花 20~40 秒冷启动）。"
                         "不给就是自动：dy/ks/xhs 这类本机验不了票的平台 + 长任务会自动开")
    sp.add_argument("--no-probe", dest="probe", action="store_false", default=None,
                    help="强制不开探针（哪怕自动判断觉得该开）")
    sp.add_argument("--json", action="store_true", default=False,
                    help="给 AI/程序读：stdout 只输出一段固定结构的 JSON，人类看的进度改道到 stderr")
    if with_detach:
        sp.add_argument("--detach", action="store_true", default=False,
                        help="后台跑：立刻回一句「已启动」就撒手，任务脱钩继续跑（关窗口也不影响）；"
                             "进度用 status 看，喊停用 cancel")


def main() -> int:
    _stdout_utf8()
    if Path(sys.executable).resolve() != VENV_PY.resolve() and VENV_PY.is_file():
        # 自动切换到项目 venv 解释器，保证依赖可用
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve())] + sys.argv[1:])

    ap = argparse.ArgumentParser(prog="agent_crawl", description="MediaCrawler 参数化封装层（方案B）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_run = sub.add_parser("run", help="单任务爬取")
    add_run_flags(sp_run)

    sp_batch = sub.add_parser("batch", help="批量任务队列")
    sp_batch.add_argument("--file", required=True, help="JSON 数组文件，每项与 run 参数同名")
    sp_batch.add_argument("--detach", action="store_true", default=False,
                          help="整条队列甩到后台跑，立刻返回（队列可能几小时，前台死等没意义）")
    sp_batch.add_argument("--json", action="store_true", default=False,
                          help="stdout 只输出一段汇总 JSON（每个任务一条记录），人类看的进度改道到 stderr")

    sp_login = sub.add_parser("login", help="触发平台扫码登录")
    sp_login.add_argument("-p", "--platform", required=True, help="平台")
    sp_login.add_argument("--check-only", action="store_true", default=False,
                          help="只查登录态、不弹浏览器（AI 该用这个）")
    sp_login.add_argument("--json", action="store_true", default=False,
                          help="stdout 只输出一段固定结构的 JSON")

    sp_status = sub.add_parser("status", help="查看环境与最近任务")
    sp_status.add_argument("--task", default=None,
                           help="只看某个任务：给任务名（可只给片段）或 tasks\\ 下的路径")
    sp_status.add_argument("--json", action="store_true", default=False,
                           help="stdout 只输出一段固定结构的 JSON（环境/锁/进度/最近任务）")

    sp_def = sub.add_parser("defaults", help="查看/设置默认参数")
    sp_def.add_argument("--set", nargs="*", default=[], help="如 --set count=30 format=excel")
    sp_def.add_argument("--json", action="store_true", default=False,
                        help="stdout 只输出一段固定结构的 JSON")

    sp_est = sub.add_parser("estimate", help="只估算不真跑：大概多少条、要等多久、要不要换词")
    add_run_flags(sp_est, with_detach=False)

    sp_res = sub.add_parser("resume", help="断点续跑：接着旧任务，按关键词补抓 + 按 ID 去重合并")
    sp_res.add_argument("--task", required=True,
                        help="要续跑的旧任务：tasks\\ 下的目录名（可只给片段）/ 绝对路径 / 历史里的 task_dir")
    sp_res.add_argument("--attempt", type=int, default=None,
                        help="指定读第几次尝试的数据（默认取编号最大、且真落了数据的那次）")
    sp_res.add_argument("-n", "--count", dest="count", type=int, default=None,
                        help="每个关键词的目标条数（默认沿用旧任务 params.json 里的值）")
    sp_res.add_argument("--from-scratch", dest="from_scratch", action="store_true",
                        default=False,
                        help="不管抓到哪一步，所有关键词都从第 1 页重抓（仍按 ID 去重）")
    sp_res.add_argument("--probe", dest="probe", action="store_true", default=None,
                        help="每个词开跑前先探一次票（不给就是自动：验不了票的平台 + 长任务会自动开）")
    sp_res.add_argument("--no-probe", dest="probe", action="store_false", default=None,
                        help="强制不开探针")
    sp_res.add_argument("--wait-lock", dest="wait_lock", type=float, default=None,
                        help="别人在跑时最多等几分钟（默认 0=直接退出）")
    sp_res.add_argument("--timeout-min", dest="timeout_min", type=int, default=None,
                        help="每个关键词的超时分钟数（默认自动）")
    sp_res.add_argument("--detach", action="store_true", default=False,
                        help="续跑甩到后台跑，立刻返回（续跑可能几小时，前台死等没意义）")
    sp_res.add_argument("--json", action="store_true", default=False,
                        help="stdout 只输出一段固定结构的 JSON，人类看的进度改道到 stderr")

    sp_cancel = sub.add_parser("cancel", help="喊停正在跑的任务（优雅收尾，数据保留）")
    sp_cancel.add_argument("--wait", type=int, default=30,
                           help="喊停后最多等几秒看它有没有真停下（默认 30 秒；填 0 就递完纸条立刻返回）")
    sp_cancel.add_argument("--json", action="store_true", default=False,
                           help="stdout 只输出一段固定结构的 JSON")

    sp_ver = sub.add_parser("version", help="查插件版本（装了哪一版、这版加了什么）")
    sp_ver.add_argument("--json", action="store_true", default=False,
                        help="stdout 只输出一段固定结构的 JSON")

    sp_exp = sub.add_parser("export", help="把已采任务目录导出成交付用 xlsx（只搬不改）")
    sp_exp.add_argument("--task", required=True,
                        help="任务名或任务目录：tasks\\ 下的目录名（可只给片段）/ 绝对路径 / state.json 里的历史 task_dir")
    sp_exp.add_argument("--out", default=None, help="输出 xlsx 路径（默认 agent_crawler\\exports\\<任务名>_<时间>.xlsx）")
    sp_exp.add_argument("--kinds", default="contents,comments",
                        help="导哪几类，逗号分隔：contents,comments,creators（默认 contents,comments）")
    sp_exp.add_argument("--attempt", type=int, default=None,
                        help="指定第几次尝试（默认取编号最大、且真落了数据的那次）")
    sp_exp.add_argument("--no-unified", action="store_true", default=False,
                        help="不加「统一格式」页（只要原样 Excel 时用；默认会加）")
    sp_exp.add_argument("--json", action="store_true", default=False,
                        help="stdout 只输出一段固定结构的 JSON（文件路径/sheet 行数/校验问题）")

    args = ap.parse_args()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "batch":
        return cmd_batch(args)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "defaults":
        return cmd_defaults(args)
    if args.cmd == "estimate":
        return cmd_estimate(args)
    if args.cmd == "resume":
        return cmd_resume(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "version":
        return cmd_version(args)
    if args.cmd == "export":
        return cmd_export(args)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⛔ 已中断")
        sys.exit(130)
