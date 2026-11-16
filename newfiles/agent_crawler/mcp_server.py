r"""MediaCrawler 的 MCP 工具层：给 AI 一张「标准插头」。

它是 agent_crawl.py 外面薄薄一层壳 —— 只负责把 AI 的工具调用翻译成命令行，再把
命令行吐出的 --json 原样交回去。真正干活、判断成功失败的逻辑全在 agent_crawl.py，
这里不重复、不篡改、不加工。

为什么用纯标准库：这台机器的 venv 里没有 mcp 包，装一个包要联网、要动依赖，而
MCP 的 stdio 协议本身就是「一行一条 JSON」，用标准库几十行就能说清楚。少一个依赖，
交付给别人时也少一个坑。

启动方式（给 AI 平台填的配置，装好后路径会自动填成本机位置）：
    command: %MC_ROOT%\\.venv\\Scripts\\python.exe
    args:    ["%MC_ROOT%\\agent_crawler\\mcp_server.py"]

自测（不进 AI 平台也能验）：
    python mcp_server.py --selftest       # 打印工具清单和一次真实调用的结果
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(__file__).resolve().parent
ROOT = HOME.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
AGENT_PY = HOME / "agent_crawl.py"
SERVER_NAME = "Hermes"
# 与 agent_crawl.py 的 AGENT_VERSION 保持一致：AI 平台在 initialize 时能看到这版号，
# 用户排查"我装的到底是哪一版"时也有个对照。改版本时两处一起改。
SERVER_VERSION = "1.11.1"
PROTOCOL_VERSION = "2024-11-05"

# 单次工具调用最多等多久（秒）。起跑一律走后台，所以这里只需覆盖"看状态/预估/导出"。
CALL_TIMEOUT_SEC = 300
# 明确不碰的东西：登录态、cookie、采集数据、.bak。
# 工具层只调 agent_crawl.py 的子命令，不直接读写这些文件。


def _py() -> str:
    """优先用项目 venv 的解释器（依赖齐全）；没有就用当前解释器。"""
    return str(VENV_PY) if VENV_PY.is_file() else sys.executable


def run_agent(args: List[str], timeout: int = CALL_TIMEOUT_SEC) -> Dict[str, Any]:
    """调一次 agent_crawl.py，返回 {ok, data|raw, stderr, exit_code}。

    约定：一律加 --json，所以 stdout 是能直接解析的 JSON；人看的进度在 stderr。
    """
    cmd = [_py(), str(AGENT_PY)] + args
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None,
                "error": f"命令超过 {timeout} 秒没返回。长任务请用 detach=true 走后台。"}
    except Exception as e:
        return {"ok": False, "exit_code": None, "error": f"{type(e).__name__}: {e}"}

    out = (p.stdout or "").strip()
    data: Optional[Dict[str, Any]] = None
    if out:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
    if data is None:
        return {"ok": False, "exit_code": p.returncode,
                "error": "命令没有输出可解析的 JSON",
                "stdout_tail": out[-800:], "stderr_tail": (p.stderr or "")[-800:]}
    return {"ok": p.returncode == 0, "exit_code": p.returncode,
            "data": data, "stderr_tail": (p.stderr or "")[-400:]}


# ----------------------------------------------------------------------------
# 工具定义（name / description / inputSchema）
# description 是给 AI 看的说明书：什么场景用、什么不要用，都写在这里。
# ----------------------------------------------------------------------------
_PLATFORM_SCHEMA = {
    "type": "string",
    "description": ("平台：bili(B站)/zhihu(知乎)/wb(微博)/tieba(贴吧)/dy(抖音)/ks(快手)，可写中文。"
                    "xhs(小红书) 反爬最狠、本项目不推荐也不保证能跑通，别向用户承诺"),
}


def _run_props(detach: bool = True) -> Dict[str, Any]:
    return {
        "platform": _PLATFORM_SCHEMA,
        "keywords": {"type": "string", "description": "关键词，多个用英文逗号分隔（上限 10 个）"},
        "count": {"type": "integer", "description": "每个关键词要多少条（默认取记忆值；上限见 state.json 的 count_cap，默认 2000）"},
        "type": {"type": "string", "enum": ["search", "detail", "creator"],
                 "description": "任务类型：search=按关键词搜、detail=指定内容ID、creator=指定创作者，默认 search"},
        "specified_id": {"type": "string", "description": "type=detail 时必填：内容ID，多个逗号分隔"},
        "creator_id": {"type": "string", "description": "type=creator 时必填：创作者ID，多个逗号分隔"},
        "name": {"type": "string", "description": "任务名，默认取第一个关键词。后续查进度/喊停/导出都用它"},
        "comments": {"type": "boolean", "description": "是否抓评论，默认取记忆值"},
        "max_comments": {"type": "integer", "description": "每条内容最多抓多少条评论（1~20000）"},
        "format": {"type": "string", "enum": ["jsonl", "json", "csv", "excel"],
                   "description": "落盘格式，默认取记忆值"},
        "retries": {"type": "integer", "description": "基础设施类错误（网络/超时）自动重试次数 0~3"},
        "timeout_min": {"type": "integer", "description": "超时分钟数，不填走自动公式（10+数量/10，上限 180）"},
        "headless": {"type": "boolean", "description": "无头模式。默认可见窗口反爬更稳，别乱开"},
        "probe": {"type": "boolean",
                  "description": ("开跑前先探一次票（用泛词取 1 条真实数据，多花 20~40 秒冷启动）。"
                                  "不填=自动：dy/ks/xhs 这类本机验不了票的平台 + 长任务（≥100 条）会自动开；"
                                  "true=一定探；false=一定不探")},
        "detach": {"type": "boolean",
                   "description": ("true=后台跑，立刻返回任务信息，之后用 crawl_status 看进度、crawl_cancel 喊停（推荐，长任务必须用）；"
                                   "false=前台等它跑完才返回，只在条数很少（几十条）时用")},
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "crawl_start",
        "description": (
            "发起一次爬取任务。默认后台跑（detach=true），立刻返回任务名/进程号，不阻塞。"
            "**重要：它只负责『点火』，不会等跑完。** 调用后必须紧接着调 crawl_wait(task=...) 等结果，"
            "否则用户只会看到一句『已开始运行』，不知道到底成没成。"
            "发之前建议先调 crawl_estimate 看要多久、大概能出多少条。"
            "注意：同一台机器同时只能跑一个任务，撞上了会直接报「本机已有任务在跑」。"
            "注意：爬取需要该平台已登录，没登录会失败并归类为 not_logged_in，此时不要反复重试，先让用户登录。"
        ),
        "inputSchema": {"type": "object", "properties": _run_props(),
                        "required": ["platform", "keywords"]},
    },
    {
        "name": "crawl_status",
        "description": (
            "查看当前环境和一个任务的进度。不带 task 看整体（谁在跑、有没有残留浏览器、最近任务）；"
            "带 task 看单个任务：跑到第几次、落盘多少条、大概还要多久、上次为什么失败。"
            "想知道「跑完了没 / 成了没 / 要不要登录」就调它，别去读日志文件。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string",
                     "description": "任务名（可只给片段）或任务目录路径。不填=看整体环境"}},
            "required": []},
    },
    {
        "name": "crawl_wait",
        "description": (
            "盯着一个任务，等它跑完再返回结果（最多等 timeout 秒，默认 300）。"
            "**crawl_start 之后应该紧接着调它**，这样用户能一次看到『成了没、拿了几条、文件在哪』，"
            "而不是只看到一句『已开始运行』。"
            "返回里 finished=true 表示真跑完了（成功/失败/取消都算），false 表示还没跑完（到时间了先返回一次）。"
            "没跑完时可以再调一次继续等。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "任务名（可只给片段）或任务目录路径"},
            "timeout": {"type": "integer",
                        "description": "最多等几秒，默认 300（5 分钟），上限 540"},
            "interval": {"type": "integer",
                         "description": "每隔几秒查一次，默认 5 秒"}},
            "required": ["task"]},
    },
    {
        "name": "crawl_cancel",
        "description": (
            "喊停正在跑的任务。不是硬杀：会给它递一张纸条，它自己优雅收尾（停止爬取→关浏览器→写报告）。"
            "已经落盘的数据会保留，没跑完的部分不会补。"
            "调用后最多等 wait 秒确认是否真停了。没有任务在跑时返回 ok=false。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "wait": {"type": "integer", "description": "最多等几秒看它是否停下，默认 30，0=递完纸条立刻返回"}},
            "required": []},
    },
    {
        "name": "crawl_estimate",
        "description": (
            "只估算，不真跑：这个关键词大概能出多少条、要等多久、有没有坑（没登录、要量超上限、"
            "现在有任务占着）。依据是本机历史实测成绩 + 超时公式，没有历史时会明说「估不准」。"
            "不确定要不要真跑、或者要跟用户报时间，就先调它。"
        ),
        "inputSchema": {"type": "object", "properties": _run_props(),
                        "required": ["platform", "keywords"]},
    },
    {
        "name": "crawl_export",
        "description": (
            "把已经采完的任务导出成一个 xlsx 交付文件（多 sheet：原样数据 + 统一格式页）。"
            "只搬不改，不会重新爬。用户说「给我个 Excel / 表格」时用。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "任务名或任务目录，可只给片段"},
            "out": {"type": "string", "description": "导出到哪，不填默认 agent_crawler\\exports\\ 下带时间戳的 xlsx"},
            "kinds": {"type": "string",
                      "description": "导哪几类，逗号分隔：contents,comments,creators，默认 contents,comments"}},
            "required": ["task"]},
    },
    {
        "name": "crawl_batch",
        "description": (
            "一次排一条队列：多个「平台+关键词」组合按顺序跑。每个任务之间不会并发，"
            "整台机器同一时刻仍然只有一个任务在跑。"
            "默认整条队列甩到后台（detach=true），立刻返回，之后用 crawl_status 看进度、crawl_cancel 喊停。"
            "适合用户说「这几个词都帮我爬一遍」的场景；只爬一个词就用 crawl_start，别用这个。"
            "注意：队列里任何平台没登录，那个任务就会失败并归类 not_logged_in，其它任务照跑。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "tasks": {
                "type": "array",
                "description": "任务列表，每一项的字段和 crawl_start 一样（platform/keywords 必填）",
                "items": {"type": "object", "properties": _run_props(detach=False)},
            },
            "detach": {"type": "boolean",
                       "description": "true=整条队列后台跑（推荐）；false=前台等整条队列跑完才返回"}},
            "required": ["tasks"]},
    },
    {
        "name": "crawl_login_check",
        "description": (
            "查某个平台登录态还在不在。只读本地登录票，不启浏览器、不发请求。"
            "返回 logged_in=true 说明可以直接爬；false 说明要有人去补票（扫码/导扩展票）。"
            "注意：dy(抖音)/ks(快手)/xhs 这三个接口要签名，本机只能看「票文件在不在」，"
            "票看起来在也可能被平台判过期，真正的答案只有跑起来才知道。"
            "它不会替你登录——登录必须有人在场，你只能把 need_user_action 告诉用户。"
        ),
        "inputSchema": {"type": "object", "properties": {"platform": _PLATFORM_SCHEMA},
                        "required": ["platform"]},
    },
    {
        "name": "crawl_resume",
        "description": (
            "断点续跑：接着一个没跑完（或没跑够）的旧任务继续抓，然后把新旧数据按内容 ID 去重合并。"
            "只读磁盘先算账：每个关键词已经有多少条、上次抓到第几页，够数的词直接跳过，不白跑。"
            "产出是一个新目录（<原名>_续跑），旧任务目录一条不动。"
            "只支持 jsonl/json/csv；旧任务用的是 Excel 会被直接拒绝（整份覆盖写，合并不安全）。"
            "只支持 type=search；detail/creator 不支持续跑。"
            "续跑是逐词串行跑的，可能很久，建议先用 crawl_estimate 估一下，或者直接 detach=true 甩后台。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string",
                     "description": "要续跑的旧任务：任务名（可只给片段）、任务目录绝对路径，或 status 里看到的 task_dir"},
            "attempt": {"type": "integer",
                        "description": "指定读第几次尝试的数据；不填=取编号最大、且真落了数据的那次"},
            "count": {"type": "integer",
                      "description": "每个关键词的目标条数；不填=沿用旧任务当时的设置"},
            "from_scratch": {"type": "boolean",
                             "description": "true=不管上次抓到哪，所有关键词都从第 1 页重抓（仍按 ID 去重）"},
            "probe": {"type": "boolean",
                      "description": "每个词开跑前先探一次票；不填=自动（验不了票的平台 + 长任务会自动开）"},
            "detach": {"type": "boolean",
                       "description": "true=后台跑立刻返回（推荐，续跑常常很久）；false=前台等它跑完"},
            "wait_lock": {"type": "integer", "description": "别人在跑时最多等几分钟，不填=0 直接退出"},
            "timeout_min": {"type": "integer", "description": "每个关键词的超时分钟数，不填走自动公式"}},
            "required": ["task"]},
    },
    {
        "name": "crawl_defaults",
        "description": (
            "查看或修改本机默认参数（不填 set 就是查看）。"
            "这会影响之后所有没显式指定的任务，比如把 count 默认值从 20 改成 30。"
            "用户说「以后都按这个来」时才改；只是想这次多爬点，请直接在 crawl_start 里传 count。"
        ),
        "inputSchema": {"type": "object", "properties": {
            "set": {"type": "array", "items": {"type": "string"},
                    "description": "要改的项，形如 [\"count=30\",\"format=excel\"]。不填=只看不改"}},
            "required": []},
    },
]


# ----------------------------------------------------------------------------
# 工具 → 命令行的翻译
# ----------------------------------------------------------------------------
def _build_run_args(a: Dict[str, Any], sub: str, with_detach: bool) -> List[str]:
    args = [sub, "-p", str(a["platform"]), "-k", str(a["keywords"])]
    simple = [("count", "--count"), ("type", "--type"), ("specified_id", "--specified-id"),
              ("creator_id", "--creator-id"), ("name", "--name"),
              ("max_comments", "--max-comments"), ("format", "--format"),
              ("retries", "--retries"), ("timeout_min", "--timeout-min")]
    for key, flag in simple:
        if a.get(key) is not None and a.get(key) != "":
            args += [flag, str(a[key])]
    if a.get("comments") is not None:
        args.append("--comments" if a["comments"] else "--no-comments")
    if a.get("headless") is not None:
        args.append("--headless" if a["headless"] else "--no-headless")
    # 探针三态：只有 AI 明确表态才翻译成开关，不表态就交给命令行自动判断。
    if a.get("probe") is not None:
        args.append("--probe" if a["probe"] else "--no-probe")
    if with_detach and a.get("detach", True):
        args.append("--detach")
    args.append("--json")
    return args


def call_tool(name: str, a: Dict[str, Any]) -> Dict[str, Any]:
    if name == "crawl_start":
        return run_agent(_build_run_args(a, "run", with_detach=True))
    if name == "crawl_estimate":
        return run_agent(_build_run_args(a, "estimate", with_detach=False))
    if name == "crawl_status":
        args = ["status", "--json"]
        if a.get("task"):
            args += ["--task", str(a["task"])]
        return run_agent(args)
    if name == "crawl_wait":
        return _call_wait(a)
    if name == "crawl_cancel":
        wait = a.get("wait")
        wait = 30 if wait is None else max(0, min(int(wait), 600))
        return run_agent(["cancel", "--wait", str(wait), "--json"], timeout=wait + 120)
    if name == "crawl_export":
        args = ["export", "--task", str(a["task"]), "--json"]
        if a.get("out"):
            args += ["--out", str(a["out"])]
        if a.get("kinds"):
            args += ["--kinds", str(a["kinds"])]
        return run_agent(args, timeout=600)
    if name == "crawl_batch":
        return _call_batch(a)
    if name == "crawl_resume":
        return _call_resume(a)
    if name == "crawl_login_check":
        return run_agent(["login", "-p", str(a["platform"]), "--check-only", "--json"], timeout=120)
    if name == "crawl_defaults":
        args = ["defaults", "--json"]
        vals = a.get("set") or []
        if vals:
            args += ["--set"] + [str(v) for v in vals]
        return run_agent(args, timeout=120)
    return {"ok": False, "error": f"未知工具：{name}"}


def _call_wait(a: Dict[str, Any]) -> Dict[str, Any]:
    """盯一个任务直到跑完，再把最终结果交回去。

    为什么要这个：crawl_start 是「点火即返回」，AI 拿到的只是「已启动」。对话就断在这儿，
    用户看到一句「已开始运行」之后就没下文了，只能自己再问一遍。这个工具把「等」这件事
    搬到工具调用里头：AI 阻塞等，跑完一次性拿到结论，用户就看到一句完整的话。

    实现上不碰任务目录、不读日志，只反复调 status --task 看 finished 这个字段。
    """
    task = str(a.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "task 必填：要等哪个任务"}

    timeout = a.get("timeout")
    timeout = 300 if timeout is None else max(1, min(int(timeout), 540))
    interval = a.get("interval")
    interval = 5 if interval is None else max(1, min(int(interval), 60))

    started = time.time()
    polls = 0
    while True:
        res = run_agent(["status", "--task", task, "--json"], timeout=120)
        polls += 1
        d = res.get("data") or {}
        if "error" in res and "data" not in res:
            return {"ok": False, "error": res["error"], "polls": polls}
        if not d.get("ok"):
            # 找不到任务目录：刚开始那几秒目录可能还没建出来，给一点宽限
            if time.time() - started < 20:
                time.sleep(interval)
                continue
            return d
        if d.get("finished"):
            return d
        if time.time() - started >= timeout:
            out = dict(d)
            out["timeout_reached"] = True
            out["waited_sec"] = round(time.time() - started, 1)
            out["polls"] = polls
            return out
        time.sleep(interval)


def _call_resume(a: Dict[str, Any]) -> Dict[str, Any]:
    """续跑：命令行一个参数一个参数拼，别让 AI 记开关名。"""
    task = str(a.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "task 必填：要续跑哪个旧任务"}
    args = ["resume", "--task", task]
    if a.get("attempt") is not None:
        args += ["--attempt", str(int(a["attempt"]))]
    if a.get("count") is not None:
        args += ["--count", str(int(a["count"]))]
    if a.get("from_scratch"):
        args.append("--from-scratch")
    if a.get("wait_lock") is not None:
        args += ["--wait-lock", str(float(a["wait_lock"]))]
    if a.get("timeout_min") is not None:
        args += ["--timeout-min", str(int(a["timeout_min"]))]
    if a.get("probe") is not None:
        args.append("--probe" if a["probe"] else "--no-probe")
    # 续跑默认甩后台：一次续跑常常几十分钟到几小时，前台等着只会超时。
    if a.get("detach", True):
        args.append("--detach")
    args.append("--json")
    return run_agent(args, timeout=600)


def _call_batch(a: Dict[str, Any]) -> Dict[str, Any]:
    """批量：命令行要一个 JSON 文件，这里现写一个，调完按情况处理。

    前台跑：调完立刻删（不留垃圾）。
    后台跑：**不能删** —— 后台工人还在读这个文件。改成一个带时间戳、看得懂的文件名留在
    agent_crawler\\ 下，方便用户事后核对队列内容；文件很小，也不含登录信息。
    """
    tasks = a.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return {"ok": False, "error": "tasks 必须是非空数组，每项至少要有 platform 和 keywords"}
    detach = bool(a.get("detach", True))
    tmp_path = ""
    keep = False
    try:
        if detach:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            tmp_path = str(HOME / f"batch_{len(tasks)}个_{stamp}.json")
            keep = True
        else:
            fd, tmp_path = tempfile.mkstemp(prefix="_mc_batch_", suffix=".json", dir=str(HOME))
            os.close(fd)  # 下面用 open() 重写，不重写会被占住
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        args = ["batch", "--file", tmp_path, "--json"]
        if detach:
            args.append("--detach")
        res = run_agent(args, timeout=600)
        data = res.get("data")
        if isinstance(data, dict):
            if keep:
                data["queue_file"] = tmp_path
                data["note_extra"] = f"队列清单留在 {tmp_path}，想核对/重跑可以再用 --file 指向它"
            else:
                data.pop("log", None)
        return res
    finally:
        if tmp_path and not keep:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# MCP stdio 协议（一行一条 JSON-RPC 2.0）
# ----------------------------------------------------------------------------
def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(id_: Any, result: Dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _tool_text(res: Dict[str, Any], name: str) -> str:
    """把命令结果变成 AI 好读的一段文本：先说人话结论，再给原始 JSON。"""
    if "error" in res and "data" not in res:
        return f"没调通：{res['error']}\n{json.dumps(res, ensure_ascii=False, indent=2)}"
    d = res.get("data") or {}
    head = ""
    if name == "crawl_start":
        head = (f"任务「{d.get('task')}」{d.get('status')}"
                f"（平台 {d.get('platform')}，要 {d.get('requested')} 条）\n"
                f"{d.get('note') or ''}\n"
                f"看进度：crawl_status(task=\"{d.get('task')}\")｜喊停：crawl_cancel()")
    elif name == "crawl_status":
        if d.get("status"):
            head = f"状态：{d.get('status')}"
        else:
            lk = d.get("lock") or {}
            head = ("有任务在跑：" + str(lk.get("describe")) if lk.get("held")
                    else "有任务在跑" if d.get("running_proc_count") else "当前没有任务在跑")
        if d.get("progress"):
            head += f"\n{d['progress']}"
        if d.get("leftover_browsers"):
            head += f"\n注意：有 {len(d['leftover_browsers'])} 个残留浏览器"
    elif name == "crawl_wait":
        if d.get("finished"):
            st = d.get("status") or ""
            head = (f"任务「{d.get('name')}」跑完了：{st}\n"
                    f"拿到 {d.get('contents')} 条 / 要 {d.get('requested')} 条"
                    + (f"；原因：{d.get('reason')}" if d.get("reason") else "")
                    + (f"\n报告：{d.get('report')}" if d.get("report") else ""))
        elif d.get("timeout_reached"):
            head = (f"任务「{d.get('name')}」还没跑完（已经等了 {d.get('waited_sec')} 秒）："
                    f"目前 {d.get('contents')} / {d.get('requested')} 条\n"
                    f"要结果就再调一次 crawl_wait(task=\"{d.get('name')}\")")
        else:
            head = f"等待失败：{d.get('reason') or d.get('status') or '未知'}"
    elif name == "crawl_cancel":
        head = f"喊停结果：{d.get('status')}\n{d.get('note') or ''}"
    elif name == "crawl_estimate":
        head = (f"预估耗时 {d.get('eta_text')}（{d.get('confidence_label')}）\n"
                f"依据：{d.get('basis')}")
    elif name == "crawl_export":
        head = f"导出结果：{d.get('status') or ('成功' if res['ok'] else '失败')}\n{d.get('path') or ''}"
    elif name == "crawl_batch":
        head = (f"批量队列：{d.get('status')}\n"
                f"{d.get('note') or ''}\n"
                f"看进度：crawl_status()｜喊停：crawl_cancel()"
                + (f"\n队列清单：{d.get('queue_file')}" if d.get("queue_file") else ""))
    elif name == "crawl_resume":
        head = (f"续跑：{d.get('status')}\n"
                f"内容对账：旧 {d.get('old_contents')} 条 + 新 {d.get('new_contents')} 条"
                f" → 去重 {d.get('duplicates_dropped')} 条 → 合并 {d.get('merged_contents')} 条\n"
                f"输出目录：{d.get('out_dir') or ''}\n"
                f"看进度：crawl_status(task=\"{d.get('task') or ''}\")"
                if d.get("out_dir") else
                f"续跑：{d.get('status')}\n{d.get('note') or ''}\n"
                f"看进度：crawl_status()｜喊停：crawl_cancel()")
    elif name == "crawl_login_check":
        if d.get("logged_in"):
            head = f"{d.get('platform')} 登录态正常，可以直接爬。"
        else:
            head = (f"{d.get('platform')} 要补票：{d.get('message')}\n"
                    f"⚠ 这一步必须有人在场（扫码/装扩展），你不能自己完成，只能告诉用户。")
    elif name == "crawl_defaults":
        head = f"默认参数{('（已保存 ' + ', '.join(d.get('changed') or []) + '）') if d.get('changed') else ''}"
    return (head + "\n\n完整数据：\n" + json.dumps(d, ensure_ascii=False, indent=2)).strip()


def handle(msg: Dict[str, Any]) -> None:
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return  # 通知，不用回
    if method == "ping":
        _result(id_, {})
        return
    if method == "tools/list":
        _result(id_, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            _error(id_, -32602, f"未知工具：{name}")
            return
        try:
            res = call_tool(name, args)
        except Exception as e:  # 工具里的异常不能把服务弄死
            _error(id_, -32603, f"工具执行异常：{type(e).__name__}: {e}")
            return
        _result(id_, {
            "content": [{"type": "text", "text": _tool_text(res, name)}],
            "isError": not res.get("ok", False),
        })
        return
    if id_ is not None:
        _error(id_, -32601, f"不支持的方法：{method}")


def serve() -> int:
    # AI 平台是通过管道（不是控制台）跟我们说话的。Windows 上 Python 这时默认用系统
    # 编码（简体中文机器是 GBK），于是「美食」这种词进来到手就是乱码、出去也发不出去。
    # 这里强制两头都按 UTF-8，中文关键词和中文描述才能原样往返。
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _error(None, -32700, "不是合法 JSON")
            continue
        if isinstance(msg, list):  # 批量请求
            for m in msg:
                handle(m)
        else:
            handle(msg)
    return 0


def selftest() -> int:
    print(f"工具数：{len(TOOLS)}")
    for t in TOOLS:
        print(f"  · {t['name']}: {t['description'][:40]}…")
    print("\n--- 真调一次 crawl_estimate ---")
    res = call_tool("crawl_estimate", {"platform": "zhihu", "keywords": "美食", "count": 20, "comments": True})
    print(_tool_text(res, "crawl_estimate")[:900])
    print("\n--- 真调一次 crawl_status ---")
    res = call_tool("crawl_status", {})
    d = res.get("data") or {}
    held = bool((d.get("lock") or {}).get("held"))
    print("ok =", res.get("ok"), "| 有锁 =", held,
          "| 进程数 =", d.get("running_proc_count"),
          "| 残留浏览器 =", len(d.get("leftover_browsers") or []))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mcp_server", description="MediaCrawler 的 MCP 工具层（stdio）")
    ap.add_argument("--selftest", action="store_true", help="不进 AI 平台也能验：打印工具清单 + 真调一次")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
