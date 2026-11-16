"""脱网自动化测试：只测「不联网」的那半边。

测什么（全部为纯本地逻辑，不起浏览器、不发请求、不读真实登录态）：
  1. validate_params        参数校验与别名/默认值/超时公式
  2. classify_reason        日志特征 → 失败原因 + 是否可自动重试
  3. reason_kind            失败原因 → 四个桶（not_logged_in/no_data/network/other）
  4. detect_empty_result    各平台「真返回 0 条」特征 + 跨平台不误判 + 微博弱证据
  5. classify_file/count_records/collect_results   落盘文件识别与计数（含去双计）
  6. spot_check / write_report                     抽查与报告生成
  7. result_payload                                --json 固定字段
  8. run_task                                     状态判定（假 attempt 驱动）
  9. build_estimate / cmd_version / cmd_defaults / cmd_status
 10. mcp_server                                   工具清单与命令行翻译（离线）

跑法：
    .venv\\Scripts\\python.exe -m pytest agent_crawler\\tests\\test_offline.py -q

所有用例都把 STATE_FILE / TASKS_ROOT / LOCK_FILE / CANCEL_FILE 指到临时目录，
不会碰真实的 state.json（历史条数）、tasks\\ 目录、运行锁。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

# 让 pytest 能直接 import 同级的 agent_crawl / mcp_server（tests 目录下没有包结构）
_HERE = Path(__file__).resolve().parent
_AGENT_DIR = _HERE.parent
for _p in (str(_AGENT_DIR), str(_AGENT_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_crawl as ac  # noqa: E402


# ----------------------------------------------------------------------------
# 公共夹具：把所有可写路径隔离到 tmp_path
# ----------------------------------------------------------------------------
@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """隔离真实状态：state.json / tasks\\ / run.lock / cancel.request / logs\\ 全进临时目录。"""
    monkeypatch.setattr(ac, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ac, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(ac, "LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(ac, "CANCEL_FILE", tmp_path / "cancel.request")
    monkeypatch.setattr(ac, "DETACH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ac, "_LOCK", None)
    return tmp_path


def fresh_state():
    """DEFAULT_STATE 的深拷贝（不读真实 state.json）。"""
    return json.loads(json.dumps(ac.DEFAULT_STATE))


def ns(**kw):
    """造一个 argparse.Namespace，缺省的 --json 等自动补 False/None。"""
    base = {"json": False}
    base.update(kw)
    return argparse.Namespace(**base)


# ----------------------------------------------------------------------------
# 1. validate_params
# ----------------------------------------------------------------------------
def test_platform_required_and_unknown():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="缺少平台参数"):
        ac.validate_params({"platform": "", "keywords": "x"}, st)
    with pytest.raises(ac.ParamError, match="不支持的平台"):
        ac.validate_params({"platform": "tiktok", "keywords": "x"}, st)


@pytest.mark.parametrize("raw,expect", [
    ("bili", "bili"), ("B站", "bili"), ("哔哩哔哩", "bili"),
    ("抖音", "dy"), ("douyin", "dy"),
    ("快手", "ks"), ("kuaishou", "ks"),
    ("wb", "wb"), ("微博", "wb"),
    ("贴吧", "tieba"), ("zhihu", "zhihu"), ("知乎", "zhihu"),
    ("小红书", "xhs"), ("xiaohongshu", "xhs"),
    ("XY", None),  # 大写的未知平台
])
def test_platform_aliases(raw, expect):
    st = fresh_state()
    if expect is None:
        with pytest.raises(ac.ParamError):
            ac.validate_params({"platform": raw, "keywords": "露营"}, st)
        return
    vp = ac.validate_params({"platform": raw, "keywords": "露营"}, st)
    assert vp["platform"] == expect


def test_xhs_has_platform_note_others_dont():
    st = fresh_state()
    assert ac.validate_params({"platform": "小红书", "keywords": "露营"}, st)["platform_note"]
    assert ac.validate_params({"platform": "bili", "keywords": "露营"}, st)["platform_note"] == ""


def test_keywords_rules():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="缺少关键词"):
        ac.validate_params({"platform": "bili", "keywords": "   "}, st)
    with pytest.raises(ac.ParamError, match="关键词解析为空"):
        ac.validate_params({"platform": "bili", "keywords": " , , ,"}, st)
    with pytest.raises(ac.ParamError, match="关键词过多"):
        ac.validate_params({"platform": "bili", "keywords": ",".join(f"k{i}" for i in range(11))}, st)
    vp = ac.validate_params({"platform": "bili", "keywords": " a , b ,, c "}, st)
    assert vp["keywords"] == ["a", "b", "c"]


def test_count_rules():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="必须是正整数"):
        ac.validate_params({"platform": "bili", "keywords": "a", "count": "abc"}, st)
    with pytest.raises(ac.ParamError, match="必须 ≥ 1"):
        ac.validate_params({"platform": "bili", "keywords": "a", "count": 0}, st)
    cap = st["defaults"]["count_cap"]
    with pytest.raises(ac.ParamError, match="超过上限"):
        ac.validate_params({"platform": "bili", "keywords": "a", "count": cap + 1}, st)
    assert ac.validate_params({"platform": "bili", "keywords": "a", "count": cap}, st)["count"] == cap
    # 不传 count 时走 state.json 记忆值
    assert ac.validate_params({"platform": "bili", "keywords": "a"}, st)["count"] == st["defaults"]["count"]


def test_format_and_type_rules():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="不支持的格式"):
        ac.validate_params({"platform": "bili", "keywords": "a", "format": "xml"}, st)
    assert ac.validate_params({"platform": "bili", "keywords": "a", "format": "表格"}, st)["format"] == "excel"
    assert ac.validate_params({"platform": "bili", "keywords": "a", "format": "XLSX"}, st)["format"] == "excel"
    with pytest.raises(ac.ParamError, match="不支持的任务类型"):
        ac.validate_params({"platform": "bili", "keywords": "a", "type": "video"}, st)
    assert ac.validate_params({"platform": "bili", "keywords": "a", "type": "搜索"}, st)["type"] == "search"


def test_detail_and_creator_need_ids():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="--specified_id"):
        ac.validate_params({"platform": "bili", "keywords": "a", "type": "detail"}, st)
    with pytest.raises(ac.ParamError, match="--creator_id"):
        ac.validate_params({"platform": "bili", "keywords": "a", "type": "creator"}, st)
    vp = ac.validate_params({"platform": "bili", "keywords": "a", "type": "detail",
                             "specified_id": "1,2,3"}, st)
    assert vp["requested"] == 3  # detail 的请求量 = ID 个数，不是 count
    vp2 = ac.validate_params({"platform": "bili", "keywords": "a", "type": "creator",
                              "creator_id": "u1, u2"}, st)
    assert vp2["requested"] == 2


def test_max_comments_and_retries_ranges():
    st = fresh_state()
    with pytest.raises(ac.ParamError, match="取值范围 1~20000"):
        ac.validate_params({"platform": "bili", "keywords": "a", "max_comments": 0}, st)
    with pytest.raises(ac.ParamError, match="取值范围 1~20000"):
        ac.validate_params({"platform": "bili", "keywords": "a", "max_comments": 20001}, st)
    with pytest.raises(ac.ParamError, match="必须是 0~3"):
        ac.validate_params({"platform": "bili", "keywords": "a", "retries": "x"}, st)
    with pytest.raises(ac.ParamError, match="取值范围 0~3"):
        ac.validate_params({"platform": "bili", "keywords": "a", "retries": 4}, st)


def test_requested_and_timeout_formula():
    st = fresh_state()
    # search：每词数量 × 词数
    vp = ac.validate_params({"platform": "bili", "keywords": "a,b", "count": 10,
                             "comments": True, "max_comments": 10}, st)
    assert vp["requested"] == 20
    # 自动超时 = base 10 + ceil(count/10) + ceil(requested*max_comments/60)
    assert vp["timeout_min"] == 10 + 1 + 4  # 10+1+ceil(200/60)=16
    # 关评论后不再追加评论时长
    vp2 = ac.validate_params({"platform": "bili", "keywords": "a,b", "count": 10,
                              "comments": False}, st)
    assert vp2["timeout_min"] == 11
    # 显式指定走显式值，只做下限 1 / 上限 timeout_max_min
    assert ac.validate_params({"platform": "bili", "keywords": "a", "timeout_min": 1}, st)["timeout_min"] == 1
    assert ac.validate_params({"platform": "bili", "keywords": "a", "timeout_min": 9999}, st)["timeout_min"] == 180


def test_name_defaults_and_sanitize():
    st = fresh_state()
    vp = ac.validate_params({"platform": "bili", "keywords": 'a/b:c*d?e"f<g>h|i'}, st)
    assert vp["name"] == "a_b_c_d_e_f_g_h_i"
    assert ac.sanitize_name("   ") == "task"
    assert len(ac.sanitize_name("x" * 100)) == 40


def test_validate_params_output_fields_complete():
    st = fresh_state()
    vp = ac.validate_params({"platform": "bili", "keywords": "a"}, st)
    for k in ("platform", "keywords", "count", "format", "platform_note", "type",
              "specified_id", "creator_id", "name", "comments", "max_comments",
              "media", "headless", "retries", "auto_import", "timeout_min",
              "passthrough", "is_login_task", "probe", "wait_lock", "requested"):
        assert k in vp, f"validate_params 少了字段 {k}"


# ----------------------------------------------------------------------------
# 2. classify_reason
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("log,expect_reason,expect_retry", [
    ("Waiting for scan code login, timeout", "登录未完成（未扫码/二维码超时）", False),
    ("HTTP 403 risk_control triggered", "平台风控/反爬拦截", False),
    ('waiting for locator("//p[text()=\'登录\']")', "快手登录自检未通过（自检接口不认票，爬虫转去点登录按钮后超时）", False),
    ("TimeoutError: page.goto timed out", "网络异常", True),
    ("Failed to launch browser", "浏览器异常/启动失败", True),
    ("everything is fine", "未知（详见日志尾部）", False),
])
def test_classify_reason_rules(log, expect_reason, expect_retry):
    reason, retryable = ac.classify_reason(log)
    assert reason == expect_reason
    assert retryable is expect_retry


def test_classify_reason_priority_login_before_network():
    # 同时出现登录与超时特征时，按 REASON_RULES 顺序先认登录（更具体、处理动作不同）
    reason, retryable = ac.classify_reason("Waiting for scan code login ... TimeoutError")
    assert reason.startswith("登录未完成") and retryable is False


# ----------------------------------------------------------------------------
# 3. reason_kind
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("reason,kind", [
    ("", ""),
    ("参数校验未通过：数量必须 ≥ 1", "other"),
    ("本机已有任务在跑：xxx", "other"),
    ("用户中断（Ctrl+C）", "other"),
    ("已取消（人为喊停；已落盘的数据保留，不会补跑）", "other"),
    ("平台返回 0 条（抖音搜索接口返回空数组，关键词「a」）", "no_data"),
    ("平台实际返回 5 条 < 请求 20 条", "no_data"),
    ("平台 bili 未登录：未发现 Cookies", "not_logged_in"),
    ("登录未完成（等满约 10 分钟没扫上码 / 登录态已过期）", "not_logged_in"),
    ("快手登录自检未通过（自检接口不认票）", "not_logged_in"),
    ("超时（进程 20 秒未响应停止信号，已强杀进程树）", "network"),
    ("平台风控/反爬拦截", "network"),
    ("浏览器异常/启动失败", "network"),
    ("完全认不出的原因", "network"),
])
def test_reason_kind(reason, kind):
    assert ac.reason_kind(reason) == kind


def test_kind_label_and_advice():
    assert ac.kind_label("not_logged_in") == "没登录"
    assert ac.kind_label("") == ""
    assert ac.kind_label("不存在") == ""
    assert "补票" in ac.kind_advice("not_logged_in")
    assert ac.kind_advice("") == ""


# ----------------------------------------------------------------------------
# 4. detect_empty_result
# ----------------------------------------------------------------------------
def test_detect_empty_dy_all_empty():
    log = ("INFO search douyin keyword: 露营, page: 1 is empty,[]\n"
           "INFO search douyin keyword: 飞盘, page: 1 is empty,[]\n")
    ev = ac.detect_empty_result(log, "dy")
    assert ev is not None
    assert ev["all_empty"] is True
    assert ev["keyword"]
    assert ev["text"].startswith("平台返回 0 条")


def test_detect_empty_partial_not_all_empty():
    log = ("INFO search douyin keyword: 露营, page: 1 is empty,[]\n"
           "INFO search douyin keyword: 飞盘, page: 1\n")
    ev = ac.detect_empty_result(log, "dy")
    assert ev is not None and ev["all_empty"] is False


@pytest.mark.parametrize("platform,log", [
    ("bili", "No more videos for keyword 'x', moving to next keyword"),
    ("bili", "No more videos for keyword 'x' on 2026-01-01, moving to next day"),
    ("xhs", "[XiaoHongShuCrawler.search] No more content"),
    ("zhihu", "No more content!"),
    ("tieba", "Search note list is empty"),
    ("ks", "got 0 videos"),
])
def test_detect_empty_each_platform(platform, log):
    ev = ac.detect_empty_result(log, platform)
    assert ev is not None, f"{platform} 的空结果特征没命中"
    assert ev["text"].startswith("平台返回 0 条")


def test_detect_empty_cross_platform_not_matched():
    # dy 的特征不能拿去判 bili：宁可认不出，也不要瞎归类
    assert ac.detect_empty_result("search douyin keyword: x, page: 1 is empty,[]", "bili") is None


def test_detect_empty_tieba_keyword_backtrack():
    # 贴吧空标记行本身不带关键词，要往上回溯到搜索行
    log = ("INFO search keyword: 露营, page: 1\n"
           "INFO Search note list is empty\n")
    ev = ac.detect_empty_result(log, "tieba")
    assert ev is not None and ev["keyword"] == "露营"


def test_detect_empty_weibo_weak_evidence():
    log = "[WeiboCrawler.search] search weibo keyword: 露营, page: 1"
    ev = ac.detect_empty_result(log, "wb")
    assert ev is not None
    assert ev["all_empty"] is True
    assert ev["keyword"] == "露营"
    # 没有搜索行就不能乱判
    assert ac.detect_empty_result("[WeiboCrawler.search] started", "wb") is None


def test_detect_empty_nothing_returns_none():
    assert ac.detect_empty_result("all good, 10 items saved", "dy") is None


# ----------------------------------------------------------------------------
# 5. 文件识别与计数
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name,kind", [
    ("bili_20261115.xlsx", "excel"),
    ("x_contents_20261115.jsonl", "contents"),
    ("x_comments_20261115.jsonl", "comments"),
    ("x_creators_20261115.jsonl", "creators"),
    ("x_profiles_1.json", "creators"),
    ("x_dynamics_1.json", "creators"),
    ("readme.txt", "other"),
])
def test_classify_file(name, kind):
    assert ac.classify_file(Path(name)) == kind


def test_count_records(tmp_path):
    jl = tmp_path / "a_contents_1.jsonl"
    jl.write_text('{"a":1}\n\n{"a":2}\n{"a":3}\n', encoding="utf-8")
    assert ac.count_records(jl) == 3

    j = tmp_path / "b_contents_1.json"
    j.write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
    assert ac.count_records(j) == 2

    j2 = tmp_path / "c.json"
    j2.write_text(json.dumps({"x": [1, 2], "y": [3], "z": "no"}), encoding="utf-8")
    assert ac.count_records(j2) == 3

    j3 = tmp_path / "d.json"
    j3.write_text(json.dumps({"only": "string"}), encoding="utf-8")
    assert ac.count_records(j3) == 1

    c = tmp_path / "e_contents_1.csv"
    c.write_text("id,title\n1,a\n2,b\n", encoding="utf-8")
    assert ac.count_records(c) == 2

    bad = tmp_path / "f.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ac.count_records(bad) == -1

    assert ac.count_records(tmp_path / "unknown.txt") == -1


def _make_xlsx(path: Path, sheets: dict):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    wb.save(path)


def test_count_excel(tmp_path):
    p = tmp_path / "out.xlsx"
    _make_xlsx(p, {"Contents": [["id", "title"], [1, "a"], [2, "b"], [3, "c"]],
                   "Comments": [["id", "text"], [1, "hi"]]})
    got = ac.count_excel(p)
    assert got["contents"] == 3
    assert got["comments"] == 1


def test_collect_results_no_double_count(tmp_path):
    d = tmp_path / "attempt_1"
    d.mkdir()
    (d / "k_contents_1.jsonl").write_text('{"id":1}\n{"id":2}\n{"id":3}\n', encoding="utf-8")
    (d / "k_comments_1.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    _make_xlsx(d / "out.xlsx", {"Contents": [["id"], [1], [2], [3]]})
    res = ac.collect_results(d)
    # jsonl 与 excel 是同一批数据的不同视图：按格式分组取最大值，不能相加成 6
    assert res["contents"] == 3
    assert res["comments"] == 1
    assert res["primary"]["kind"] == "excel"  # 主文件优先 excel


def test_collect_results_primary_falls_back_to_contents(tmp_path):
    d = tmp_path / "attempt_1"
    d.mkdir()
    (d / "k_contents_1.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    res = ac.collect_results(d)
    assert res["primary"]["kind"] == "contents"
    assert res["contents"] == 1


# ----------------------------------------------------------------------------
# 6. spot_check / write_report
# ----------------------------------------------------------------------------
def test_spot_check_pass_and_fail(tmp_path):
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    (ok_dir / "k_contents_1.jsonl").write_text(
        json.dumps({"title": "标题", "nickname": "作者", "link": "https://x/1"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    r = ac.spot_check(ok_dir)
    assert r["ok"] is True and r["samples"]

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "k_contents_1.jsonl").write_text(
        json.dumps({"title": "", "content": "", "nickname": ""}) + "\n", encoding="utf-8")
    r2 = ac.spot_check(bad_dir)
    assert r2["ok"] is False

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    r3 = ac.spot_check(empty_dir)
    assert r3["ok"] is False and "无法抽查" in r3["detail"]


def test_write_report_contains_key_sections(tmp_path):
    vp = {"name": "测试", "platform": "bili", "type": "search", "keywords": ["a"],
          "count": 1, "format": "jsonl", "comments": False, "max_comments": 10,
          "media": False, "headless": False, "probe": False}
    r = {"params": vp, "status": "失败", "reason": "网络异常", "contents": 0,
         "requested": 1, "comments": 0, "attempt": 1, "max_attempts": 2,
         "duration": 65, "files": [], "log_path": str(tmp_path / "x.log"),
         "log_text": "line1\nTimeoutError", "spot_check": None}
    ac.write_report(tmp_path, r)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "# 任务报告：测试" in text
    assert "## 这次是什么问题" in text
    assert "网络或接口出问题" in text
    assert "尝试次数: 第 1 次" in text
    assert "TimeoutError" in text


def test_write_report_without_log(tmp_path):
    vp = {"name": "t", "platform": "bili", "type": "search", "keywords": ["a"],
          "count": 1, "format": "jsonl", "comments": False, "max_comments": 10,
          "media": False, "headless": False, "probe": False}
    r = {"params": vp, "status": "预检失败", "reason": "平台 bili 未登录",
         "contents": 0, "requested": 1, "comments": 0, "attempt": 0,
         "max_attempts": 1, "duration": 1}
    ac.write_report(tmp_path, r)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## 这次是什么问题" in text
    assert "没登录" in text


# ----------------------------------------------------------------------------
# 7. result_payload
# ----------------------------------------------------------------------------
def test_result_payload_fields():
    vp = {"name": "n", "platform": "dy", "type": "search", "keywords": ["a", "b"],
          "count": 5, "format": "jsonl", "platform_note": "", "requested": 10}
    r = {"params": vp, "status": "部分完成", "reason": "平台实际返回 3 条", "attempt": 2,
         "max_attempts": 2, "requested": 10, "contents": 3, "comments": 1,
         "files": [{"path": "p", "name": "a_contents_1.jsonl", "kind": "contents",
                    "size": 12, "records": 3}],
         "primary": {"path": "p"}, "spot_check": {"ok": True, "samples": ["s1"]},
         "duration": 12.34, "task_dir": "T", "log_path": "L",
         "empty_result": False, "exit_code": 0, "qr_prompted": False}
    p = ac.result_payload(r)
    for k in ("ok", "status", "reason", "reason_kind", "reason_kind_label", "advice",
              "name", "platform", "platform_note", "type", "keywords",
              "count_per_keyword", "format", "requested", "contents", "comments",
              "attempt", "retries", "max_attempts", "empty_result", "exit_code",
              "qr_prompted", "duration_s", "task_dir", "report", "log_path",
              "primary_file", "files", "spot_check"):
        assert k in p, f"result_payload 少了字段 {k}"
    assert p["ok"] is True                     # 部分完成也算 ok
    assert p["retries"] == 1                   # attempt 2 → 重试过 1 次
    assert p["reason_kind"] == "no_data"
    assert p["reason_kind_label"] == "平台没内容"
    assert p["duration_s"] == 12.3
    assert p["report"].endswith("report.md")
    assert p["files"][0]["records"] == 3
    assert p["spot_check"] == {"ok": True, "samples": ["s1"]}


def test_result_payload_failed_not_ok():
    vp = {"name": "n", "platform": "dy", "type": "search", "keywords": ["a"],
          "count": 1, "format": "jsonl", "requested": 1}
    p = ac.result_payload({"params": vp, "status": "失败", "reason": "网络异常",
                           "attempt": 1, "max_attempts": 1, "requested": 1})
    assert p["ok"] is False
    assert p["reason_kind"] == "network"
    assert p["contents"] == 0 and p["duration_s"] == 0.0


# ----------------------------------------------------------------------------
# 8. run_task 状态判定（假 attempt 驱动，全程离线）
# ----------------------------------------------------------------------------
class FakeLock:
    """替身锁：不写文件、不做 PowerShell 扫描。"""

    def __init__(self):
        self.held = False
        self.fresh = True
        self.data = {}

    def acquire(self, vp, wait_min=0.0):
        self.held = True
        self.fresh = True
        self.data = {"pid": 1, "task": vp.get("name", "")}
        return ["假锁已挂牌 ✓"]

    def update_task(self, vp):
        pass

    def release(self):
        self.held = False
        self.fresh = False


def _patch_run_env(monkeypatch):
    monkeypatch.setattr(ac, "RunLock", FakeLock)
    monkeypatch.setattr(ac, "preflight", lambda vp, deep=True: ["假预检 ✓"])
    monkeypatch.setattr(ac, "ps_find_crawler_browsers", lambda platform=None: [])
    monkeypatch.setattr(ac, "cancel_targets_me", lambda: False)
    monkeypatch.setattr(ac, "clear_stale_cancel", lambda: False)


def _fake_attempt_factory(script):
    """script: 每次调用依次取出一个「这次尝试的表现」描述。"""
    calls = {"n": 0}

    def fake(vp, task_dir, attempt):
        i = calls["n"]
        calls["n"] += 1
        spec = script[min(i, len(script) - 1)]
        attempt_dir = Path(task_dir) / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        if spec.get("rows"):
            lines = [json.dumps({"title": f"t{j}", "nickname": "u",
                                 "link": "https://x"}, ensure_ascii=False)
                     for j in range(spec["rows"])]
            (attempt_dir / "k_contents_1.jsonl").write_text("\n".join(lines) + "\n",
                                                            encoding="utf-8")
        log_path = Path(task_dir) / f"run_attempt_{attempt}.log"
        log_path.write_text(spec.get("log", ""), encoding="utf-8")
        return {"exit_code": spec.get("exit_code", 0),
                "timed_out": spec.get("timed_out", False),
                "interrupted": False,
                "canceled": spec.get("canceled", False),
                "stop_mode": spec.get("stop_mode"),
                "log_text": spec.get("log", ""),
                "duration": 1.0,
                "attempt_dir": str(attempt_dir),
                "qr_prompted": spec.get("qr_prompted", False),
                "log_path": str(log_path)}

    return fake


def _base_params(**kw):
    p = {"platform": "bili", "keywords": "a", "count": 2, "comments": False, "retries": 0}
    p.update(kw)
    return p


def test_run_task_success(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 2}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "成功"
    assert r["contents"] == 2 and r["attempt"] == 1
    assert "spot_check" in r and r["spot_check"]["ok"] is True
    assert Path(r["task_dir"], "report.md").is_file()
    assert len(st["history"]) == 1
    assert (iso / "state.json").is_file()  # 历史已落盘（落在临时目录）


def test_run_task_partial(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 1}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "部分完成"
    assert "平台实际返回 1 条" in r["reason"]
    assert ac.reason_kind(r["reason"]) == "no_data"


def test_run_task_empty_result(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    log = "INFO search douyin keyword: 露营, page: 1 is empty,[]\n"
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 0, "log": log}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(platform="dy", keywords="露营"), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "失败"
    assert r["empty_result"] is True
    assert "平台返回 0 条" in r["reason"]
    assert ac.reason_kind(r["reason"]) == "no_data"
    assert r["attempt"] == 1  # 空结果不该自动重试


def test_run_task_timeout_then_retry(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([
        {"rows": 0, "timed_out": True, "stop_mode": True, "exit_code": None},
        {"rows": 2},
    ]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(retries=1), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "成功"
    assert r["attempt"] == 2
    assert r["max_attempts"] == 2


def test_run_task_canceled_without_data(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 0, "canceled": True}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "已取消"
    assert ac.reason_kind(r["reason"]) == "other"


def test_run_task_canceled_with_data(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 1, "canceled": True}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "部分完成"
    assert r["contents"] == 1


def test_run_task_not_logged_in(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([
        {"rows": 0, "exit_code": 1, "log": "Waiting for scan code login ... timeout"}]))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "失败"
    assert ac.reason_kind(r["reason"]) == "not_logged_in"
    assert r["attempt"] == 1  # 登录类不可自动重试


def test_run_task_preflight_failure(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt",
                        lambda vp, td, a: pytest.fail("预检失败时不该真的跑"))
    monkeypatch.setattr(ac, "preflight",
                        lambda vp, deep=True: (_ for _ in ()).throw(ac.ParamError("平台 bili 未登录：未发现 Cookies")))
    st = fresh_state()
    vp = ac.validate_params(_base_params(), st)
    r = ac.run_task(vp, st)
    assert r["status"] == "预检失败"
    assert ac.reason_kind(r["reason"]) == "not_logged_in"
    assert len(st["history"]) == 1


def test_run_task_history_trimmed_to_100(iso, monkeypatch):
    _patch_run_env(monkeypatch)
    monkeypatch.setattr(ac, "run_attempt", _fake_attempt_factory([{"rows": 2}]))
    st = fresh_state()
    st["history"] = [{"ts": "x", "name": "old", "platform": "bili", "type": "search",
                      "status": "成功", "contents": 1, "requested": 1}] * 100
    vp = ac.validate_params(_base_params(), st)
    ac.run_task(vp, st)
    assert len(st["history"]) == 100


# ----------------------------------------------------------------------------
# 9. estimate / version / defaults / status
# ----------------------------------------------------------------------------
def test_build_estimate_without_history():
    st = fresh_state()
    vp = ac.validate_params({"platform": "bili", "keywords": "a", "count": 10,
                             "comments": False}, st)
    est = ac.build_estimate(vp, st)
    for k in ("ok", "status", "name", "platform", "type", "platform_note", "keywords",
              "requested", "comments_on", "max_comments_total", "eta_seconds",
              "eta_text", "sec_per_item", "confidence", "confidence_label", "basis",
              "history_expect", "timeout_min", "cautions", "note"):
        assert k in est, f"build_estimate 少了字段 {k}"
    assert est["ok"] is True and est["status"] == "预估完成"
    assert est["confidence"] == "低"
    assert est["requested"] == 10
    # 无历史粗估：40 + 10*3.0 + 0
    assert est["eta_seconds"] == 70
    assert any("没有本机实测" in c for c in est["cautions"])


def test_build_estimate_with_history():
    st = fresh_state()
    st["history"] = [{"platform": "bili", "type": "search", "status": "成功",
                      "contents": 20, "requested": 20, "duration_s": 60},
                     {"platform": "bili", "type": "search", "status": "成功",
                      "contents": 20, "requested": 20, "duration_s": 60},
                     {"platform": "bili", "type": "search", "status": "成功",
                      "contents": 20, "requested": 20, "duration_s": 60}]
    vp = ac.validate_params({"platform": "bili", "keywords": "a", "count": 20,
                             "comments": False}, st)
    est = ac.build_estimate(vp, st)
    assert est["confidence"] == "高"          # 同类型 3 次以上
    assert abs(est["sec_per_item"] - 3.0) < 0.01
    assert est["eta_seconds"] == 60           # 3 秒/条 × 20 条
    assert "本机 3 次同平台实测" in est["basis"]


def test_cmd_version_json(iso, monkeypatch):
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    rc = ac.cmd_version(ns(json=True))
    assert rc == 0
    assert box["ok"] is True
    assert box["version"] == ac.AGENT_VERSION
    assert box["mcp_server_exists"] is True
    assert box["note"]


def test_cmd_defaults_view_and_set(iso, monkeypatch):
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    rc = ac.cmd_defaults(ns(json=True, set=[]))
    assert rc == 0 and box["ok"] is True
    assert box["status"] == "当前默认参数" and box["changed"] == {}

    rc = ac.cmd_defaults(ns(json=True, set=["count=30", "format=excel"]))
    assert rc == 0 and box["status"] == "已保存"
    assert box["changed"] == {"count": 30, "format": "excel"}
    saved = json.loads((iso / "state.json").read_text(encoding="utf-8"))
    assert saved["defaults"]["count"] == 30

    rc = ac.cmd_defaults(ns(json=True, set=["nope=1"]))
    assert rc == 1
    assert box["ok"] is False and box["status"] == "参数错误"
    assert "count" in box["available"]


def test_cmd_defaults_bool_value(iso, monkeypatch):
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    ac.cmd_defaults(ns(json=True, set=["headless=true"]))
    assert box["changed"] == {"headless": True}


def test_cmd_status_json(iso, monkeypatch):
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    monkeypatch.setattr(ac, "ps_find_crawler_procs", lambda exclude_pids=None: [])
    monkeypatch.setattr(ac, "ps_find_crawler_browsers", lambda platform=None: [])
    st = fresh_state()
    st["history"] = [{"ts": "2026-11-14 10:00:00", "name": "t1", "platform": "bili",
                      "type": "search", "status": "失败", "reason": "网络异常",
                      "contents": 0, "requested": 10, "attempt": 2, "task_dir": "D",
                      "duration_s": 30}]
    monkeypatch.setattr(ac, "load_state", lambda: st)
    rc = ac.cmd_status(ns(json=True, task=None))
    assert rc == 0
    for k in ("ok", "version", "now", "running_groups", "running_proc_count",
              "leftover_browsers", "lock", "progress", "cancel_pending", "history"):
        assert k in box, f"cmd_status 少了字段 {k}"
    assert box["lock"]["held"] is False
    assert box["cancel_pending"] is False
    h = box["history"][0]
    assert h["name"] == "t1" and h["reason_kind"] == "network"
    assert h["reason_kind_label"] == "网络或接口出问题"
    assert h["retries"] == 1


# ----------------------------------------------------------------------------
# 9b. 进程识别：只读命令不算「有人在跑」
# ----------------------------------------------------------------------------
_PY = r'"C:\MediaCrawler\.venv\Scripts\python.exe" '
_AG = r'"C:\MediaCrawler\agent_crawler\agent_crawl.py" '


@pytest.mark.parametrize("cmd", [
    _PY + _AG + "status --json",
    _PY + _AG + "status --task 第二次验收 --json",
    _PY + _AG + "defaults --json",
    _PY + _AG + "version --json",
    _PY + _AG + "export --task t1",
    _PY + _AG + "estimate -p bili -k AI --count 10 --json",
])
def test_proc_is_busy_readonly_false(cmd):
    """只读命令（查进度/查默认值/查版本/导出/估算）不占浏览器，不能算冲突。"""
    assert ac._proc_is_busy(cmd) is False


@pytest.mark.parametrize("cmd", [
    _PY + _AG + "run -p bili -k AI --count 10 --detach --json",
    _PY + _AG + "batch --file q.json --detach",
    _PY + _AG + "login -p bili",
    _PY + _AG + "resume --task t1 --json",
    _PY + _AG + "cancel --task t1",
    _PY + r'"C:\MediaCrawler\main.py" --platform bili --type search',
])
def test_proc_is_busy_working_true(cmd):
    """真干活的命令（含原始 main.py）照旧算冲突。"""
    assert ac._proc_is_busy(cmd) is True


def test_proc_is_busy_unknown_is_conservative():
    """命令行残缺、看不出子命令时宁可多拦一次，也不能放过去。"""
    assert ac._proc_is_busy(_PY + _AG) is True


def test_proc_label_splits_subcommand():
    """标签要能认出平台和子命令，预检报错时才看得懂是谁在跑。"""
    assert ac._proc_label(_PY + _AG + "run -p bili -k AI") == "bili run"
    assert ac._proc_label(_PY + _AG + "status --json") == "status"
    assert ac._proc_label(_PY + r'"C:\MediaCrawler\main.py" -p dy') == "dy main.py"


def _preflight_vp(**kw):
    vp = {"platform": "bili", "is_login_task": False, "auto_import": False}
    vp.update(kw)
    return vp


def _preflight_setup(iso, monkeypatch, procs):
    """把预检的外部依赖全钉死，只留「进程判定」这一条真实逻辑。"""
    monkeypatch.setattr(ac, "_own_process_chain", lambda: set())
    monkeypatch.setattr(ac, "ps_scan_environment",
                        lambda platform=None, exclude_pids=None: (procs, []))
    monkeypatch.setattr(ac, "merge_proc_chains", lambda ps: [
        {"root": int(p), "label": ac._proc_label(c), "members": [(p, c)]} for p, c in ps])
    monkeypatch.setattr(ac, "check_login_state", lambda platform: (True, "已登录"))


def test_preflight_ignores_readonly_status(iso, monkeypatch):
    """真机踩到的那种情况：只有自己刚跑的 status 在，点火不能被拦。"""
    _preflight_setup(iso, monkeypatch, [("1234", _PY + _AG + "status --json")])
    notes = ac.preflight(_preflight_vp(), deep=True)
    assert "无爬虫进程冲突 ✓" in notes


def test_preflight_blocks_real_run(iso, monkeypatch):
    """真有任务在跑时必须照旧拦下——这条防线不能被改松。"""
    _preflight_setup(iso, monkeypatch, [("1234", _PY + _AG + "run -p bili -k AI")])
    with pytest.raises(ac.ParamError) as ei:
        ac.preflight(_preflight_vp(), deep=True)
    assert "已有 MediaCrawler 任务正在运行" in str(ei.value)


def test_preflight_mixed_readonly_and_run(iso, monkeypatch):
    """只读和真跑混在一起：只读的被滤掉，真跑的仍然拦。"""
    _preflight_setup(iso, monkeypatch, [
        ("1111", _PY + _AG + "status --json"),
        ("2222", _PY + _AG + "run -p bili -k AI"),
    ])
    with pytest.raises(ac.ParamError) as ei:
        ac.preflight(_preflight_vp(), deep=True)
    msg = str(ei.value)
    assert "1 个任务 / 共 1 个进程" in msg
    assert "2222" in msg and "1111" not in msg


# ----------------------------------------------------------------------------
# 10. mcp_server 离线检查
# ----------------------------------------------------------------------------
def test_mcp_server_tools():
    import mcp_server as ms
    assert ms.SERVER_VERSION == ac.AGENT_VERSION
    assert ms.SERVER_NAME == "Hermes"
    names = [t["name"] for t in ms.TOOLS]
    assert len(names) == 10
    assert names == ["crawl_start", "crawl_status", "crawl_wait", "crawl_cancel", "crawl_estimate",
                     "crawl_export", "crawl_batch", "crawl_login_check", "crawl_resume",
                     "crawl_defaults"]
    for t in ms.TOOLS:
        assert t["description"].strip()
        assert t["inputSchema"]["type"] == "object"


def test_mcp_server_build_run_args():
    import mcp_server as ms
    args = ms._build_run_args({"platform": "bili", "keywords": "a,b", "count": 10,
                               "comments": False, "detach": True}, "run", with_detach=True)
    assert args[0] == "run"
    assert "-p" in args and args[args.index("-p") + 1] == "bili"
    assert "-k" in args and args[args.index("-k") + 1] == "a,b"
    assert "--count" in args and args[args.index("--count") + 1] == "10"
    assert "--no-comments" in args
    assert "--detach" in args
    assert args[-1] == "--json"

    # estimate 不带 --detach
    args2 = ms._build_run_args({"platform": "dy", "keywords": "x", "detach": True},
                               "estimate", with_detach=False)
    assert args2[0] == "estimate" and "--detach" not in args2

    # detach=False 时前端明确要前台跑
    args3 = ms._build_run_args({"platform": "dy", "keywords": "x", "detach": False},
                               "run", with_detach=True)
    assert "--detach" not in args3


def test_mcp_server_call_tool_unknown():
    import mcp_server as ms
    res = ms.call_tool("no_such_tool", {})
    assert res["ok"] is False and "未知工具" in res["error"]


def test_mcp_server_probe_tristate(monkeypatch):
    """探针三态：不表态就不加开关，表态才翻译成 --probe / --no-probe。"""
    import mcp_server as ms
    base = {"platform": "dy", "keywords": "x"}

    args = ms._build_run_args(dict(base), "run", with_detach=False)
    assert "--probe" not in args and "--no-probe" not in args

    args = ms._build_run_args(dict(base, probe=True), "run", with_detach=False)
    assert "--probe" in args and "--no-probe" not in args

    args = ms._build_run_args(dict(base, probe=False), "run", with_detach=False)
    assert "--no-probe" in args and "--probe" not in args

    # crawl_start 的入参里要有 probe 这一项，AI 才知道能填
    props = next(t for t in ms.TOOLS if t["name"] == "crawl_start")["inputSchema"]["properties"]
    assert "probe" in props


def test_mcp_server_resume_args(monkeypatch):
    """crawl_resume 的入参要翻译成 resume 子命令的正确开关。"""
    import mcp_server as ms
    seen = {}

    def fake_run_agent(args, timeout=300):
        seen["args"] = args
        return {"ok": True, "data": {"status": "续跑完成", "out_dir": "x"}}

    monkeypatch.setattr(ms, "run_agent", fake_run_agent)

    res = ms.call_tool("crawl_resume", {"task": "考研_bili", "count": 80,
                                        "from_scratch": True, "probe": False,
                                        "detach": False})
    assert res["ok"] is True
    a = seen["args"]
    assert a[0] == "resume"
    assert "--task" in a and a[a.index("--task") + 1] == "考研_bili"
    assert "--count" in a and a[a.index("--count") + 1] == "80"
    assert "--from-scratch" in a
    assert "--no-probe" in a and "--probe" not in a
    assert "--detach" not in a
    assert a[-1] == "--json"

    # 默认应该甩后台（续跑常常很久）
    ms.call_tool("crawl_resume", {"task": "考研_bili"})
    assert "--detach" in seen["args"]

    # 没给 task 要当场拦下，别去调命令行
    seen.clear()
    res = ms.call_tool("crawl_resume", {})
    assert res["ok"] is False and "task" in res["error"]
    assert seen == {}


# ----------------------------------------------------------------------------
# 11. 断点续跑（resume）：按关键词续跑 + ID 去重
# ----------------------------------------------------------------------------
def _mk_resume_task(tmp_path, platform="bili", keywords=("考研",), count=40,
                    rows=None, log="", fmt="jsonl", name="old"):
    """造一个「上次跑到一半」的任务目录：params.json + attempt_1 + 数据 + 日志。"""
    td = tmp_path / "tasks" / f"{name}_{platform}"
    att = td / "attempt_1"
    att.mkdir(parents=True)
    (td / "params.json").write_text(json.dumps({
        "name": name, "platform": platform, "type": "search",
        "keywords": list(keywords), "count": count,
        "requested": count * len(keywords), "comments": False,
        "max_comments": 0, "media": False, "headless": True,
    }, ensure_ascii=False), encoding="utf-8")
    if rows is not None:
        if fmt == "jsonl":
            p = att / "k_contents_1.jsonl"
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                         encoding="utf-8")
        elif fmt == "xlsx":
            _make_xlsx(att / "k_contents_1.xlsx",
                       {"Contents": [list(rows[0].keys())] + [list(r.values()) for r in rows]})
    if log:
        (td / "run_attempt_1.log").write_text(log, encoding="utf-8")
    return td


def test_resume_plan_skips_finished_keyword(tmp_path):
    rows = [{"video_id": f"v{i}", "source_keyword": "考研"} for i in range(40)]
    td = _mk_resume_task(tmp_path, rows=rows,
                         log="search bilibili keyword: 考研, page: 2\n")
    plan = ac.build_resume_plan(td, fresh_state())
    assert plan["ok"] is True
    item = plan["plan"][0]
    assert item["got"] == 40
    assert item["action"] == "已完成"          # 够数了，不再白跑
    assert [x for x in plan["plan"] if x["action"] == "补抓"] == []


def test_resume_plan_page_hint_from_log(tmp_path):
    rows = [{"video_id": f"v{i}", "source_keyword": "考研"} for i in range(20)]
    log = ("search bilibili keyword: 考研, page: 1\n"
           "search bilibili keyword: 考研, page: 2\n")
    td = _mk_resume_task(tmp_path, rows=rows, log=log)
    plan = ac.build_resume_plan(td, fresh_state())
    item = plan["plan"][0]
    assert item["action"] == "补抓"
    assert item["last_page"] == 2
    assert item["start_page"] == 3              # 从下一页接着跑
    assert "日志页码" in item["page_basis"]


def test_resume_plan_page_hint_fallback_by_rows(tmp_path):
    # ks 日志只有 Skip page，没有「关键词+页码」行 → 只能按条数保守推算
    rows = [{"video_id": f"v{i}", "source_keyword": "露营"} for i in range(40)]
    td = _mk_resume_task(tmp_path, platform="ks", keywords=("露营",), count=80,
                         rows=rows, log="Skip page: 1\nSkip page: 2\n")
    plan = ac.build_resume_plan(td, fresh_state())
    item = plan["plan"][0]
    assert item["action"] == "补抓"
    assert item["last_page"] == 2               # 40 ÷ 20 = 2 页
    assert item["start_page"] == 3
    assert "保守推算" in item["page_basis"]


def test_resume_plan_rejects_excel(tmp_path):
    rows = [{"video_id": "v1", "source_keyword": "考研"}]
    td = _mk_resume_task(tmp_path, rows=rows, fmt="xlsx")
    plan = ac.build_resume_plan(td, fresh_state())
    assert plan["ok"] is False
    assert "Excel" in plan["error"]
    assert "jsonl" in plan["error"]


def test_resume_plan_no_params_json_uses_history(tmp_path):
    td = tmp_path / "tasks" / "bare_bili"
    (td / "attempt_1").mkdir(parents=True)
    (td / "attempt_1" / "k_contents_1.jsonl").write_text(
        json.dumps({"video_id": "v1", "source_keyword": "考研"}) + "\n", encoding="utf-8")
    st = fresh_state()
    st["history"] = [{"name": "bare", "platform": "bili", "type": "search",
                      "keywords": "考研", "count": 20, "task_dir": str(td)}]
    plan = ac.build_resume_plan(td, st)
    assert plan["ok"] is True
    assert plan["platform"] == "bili"
    assert plan["count"] == 20


def test_resume_merge_dedup_by_id():
    old = [{"video_id": "a", "t": 1}, {"video_id": "b", "t": 2}]
    new = [{"video_id": "b", "t": 2}, {"video_id": "c", "t": 3}]
    merged, dropped = ac._merge_rows(old, new, "video_id")
    assert [r["video_id"] for r in merged] == ["a", "b", "c"]
    assert dropped == 1                          # 只有 b 是断点页附近重采到的
    # 旧数据顺序优先保留
    assert merged[0] is old[0]


def test_resume_merge_without_id_field_keeps_all():
    merged, dropped = ac._merge_rows([{"t": 1}], [{"t": 1}], "")
    assert len(merged) == 2 and dropped == 0


def test_cmd_resume_merges_and_writes(tmp_path, iso, monkeypatch):
    old_rows = [{"video_id": f"old{i}", "source_keyword": "考研"} for i in range(20)]
    td = _mk_resume_task(tmp_path, rows=old_rows,
                         log="search bilibili keyword: 考研, page: 1\n")
    new_rows = [
        {"video_id": "old19", "source_keyword": "考研"},   # 与旧数据重叠 → 该被丢掉
        {"video_id": "new1", "source_keyword": "考研"},
        {"video_id": "new2", "source_keyword": "考研"},
    ]

    def fake_run(vp, st):
        d = ac.make_task_dir(vp)
        att = d / "attempt_1"
        att.mkdir(parents=True)
        (att / "k_contents_1.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in new_rows) + "\n",
            encoding="utf-8")
        assert vp["start_page"] == 2          # 从断点页的下一页开跑
        return {"status": "成功", "reason": "", "contents": 3, "attempt": 1,
                "attempt_dir": str(att), "task_dir": str(d)}

    monkeypatch.setattr(ac, "run_task", fake_run)
    monkeypatch.setattr(ac, "cancel_targets_me", lambda: False)

    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    rc = ac.cmd_resume(ns(json=True, task=str(td), attempt=None, count=None,
                          from_scratch=False, probe=False, wait_lock=None,
                          timeout_min=None))
    assert rc == 0
    assert box["status"] == "续跑完成"
    assert box["old_contents"] == 20
    assert box["new_contents"] == 3
    assert box["merged_contents"] == 22        # 20 旧 + 3 新 − 1 重复
    assert box["duplicates_dropped"] == 1
    expect = "resume_contents_" + ac.datetime.now().strftime("%Y-%m-%d") + ".jsonl"
    out = Path(box["out_dir"]) / expect
    assert out.is_file()
    got = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(got) == 22
    assert len({r["video_id"] for r in got}) == 22   # 真的没有重复 ID


def test_cmd_resume_nothing_to_do(tmp_path, iso, monkeypatch):
    rows = [{"video_id": f"v{i}", "source_keyword": "考研"} for i in range(40)]
    td = _mk_resume_task(tmp_path, rows=rows,
                         log="search bilibili keyword: 考研, page: 2\n")
    monkeypatch.setattr(ac, "run_task",
                        lambda vp, st: pytest.fail("都够数了不该再跑"))
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    rc = ac.cmd_resume(ns(json=True, task=str(td), attempt=None, count=None,
                          from_scratch=False, probe=False, wait_lock=None,
                          timeout_min=None))
    assert rc == 0
    assert box["status"] == "无事可做"
    assert box["merged_contents"] == 40


def test_cmd_resume_detach_only_when_todo(tmp_path, iso, monkeypatch):
    """--detach：有活才甩后台；都够数了就不该白拉一个后台进程。"""
    rows = [{"video_id": f"v{i}", "source_keyword": "考研"} for i in range(10)]
    td = _mk_resume_task(tmp_path, count=40, rows=rows,
                         log="search bilibili keyword: 考研, page: 1\n")
    calls = []
    monkeypatch.setattr(ac, "_detach_launch",
                        lambda *a, **k: calls.append((a, k)) or 0)
    monkeypatch.setattr(ac, "run_task", lambda vp, st: pytest.fail("甩后台就不该前台跑"))
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))

    rc = ac.cmd_resume(ns(json=True, task=str(td), attempt=None, count=None,
                          from_scratch=False, probe=None, wait_lock=None,
                          timeout_min=None, detach=True))
    assert rc == 0
    assert len(calls) == 1
    kws = calls[0][1]
    assert kws["match_history"] is False and kws["noun"] == "续跑"
    assert calls[0][0][2] == ["考研"]          # 关键词列表原样带过去

    calls.clear()
    full = [{"video_id": f"v{i}", "source_keyword": "考研"} for i in range(40)]
    td2 = _mk_resume_task(tmp_path, count=40, rows=full, name="old2",
                          log="search bilibili keyword: 考研, page: 2\n")
    rc = ac.cmd_resume(ns(json=True, task=str(td2), attempt=None, count=None,
                          from_scratch=False, probe=None, wait_lock=None,
                          timeout_min=None, detach=True))
    assert rc == 0
    assert calls == []                         # 没事可做 → 不甩后台
    assert box["status"] == "无事可做"


def test_cmd_resume_unknown_task(iso, monkeypatch):
    box = {}
    monkeypatch.setattr(ac, "emit_json", lambda o: box.update(o))
    rc = ac.cmd_resume(ns(json=True, task="根本没有这个任务", attempt=None, count=None,
                          from_scratch=False, probe=False, wait_lock=None,
                          timeout_min=None))
    assert rc == 1
    assert box["ok"] is False and box["status"] == "预检失败"
    assert "找过的地方" in box["reason"]


# ----------------------------------------------------------------------------
# 12. 长任务默认开探针（第 6 项）
# ----------------------------------------------------------------------------
def test_auto_probe_off_for_verifiable_platform():
    # bili 本机能真问一句票是否有效 → 不需要探针
    on, why = ac.auto_probe_decision("bili", 5000)
    assert on is False and why == ""


def test_auto_probe_off_for_short_task():
    # dy 验不了票，但只有 20 条，不值得为它多花半分钟
    on, why = ac.auto_probe_decision("dy", 20)
    assert on is False and why == ""


def test_auto_probe_on_for_long_unverifiable_task():
    on, why = ac.auto_probe_decision("dy", 2000)
    assert on is True
    assert "--no-probe" in why          # 必须告诉用户怎么关掉


def test_validate_params_probe_tristate_auto():
    st = fresh_state()
    # 不表态 → 自动开（dy + 长任务：300 条 ≥ 100）
    vp = ac.validate_params(_base_params(platform="dy", keywords="露营", count=300), st)
    assert vp["probe"] is True and vp["probe_auto_reason"]


def test_validate_params_probe_tristate_no_probe():
    st = fresh_state()
    vp = ac.validate_params(_base_params(platform="dy", keywords="露营", count=300,
                                         probe=False), st)
    assert vp["probe"] is False and vp["probe_auto_reason"] == ""


def test_validate_params_probe_tristate_force_probe():
    st = fresh_state()
    # bili 短任务本来不自动开，显式 --probe 要能强行开
    vp = ac.validate_params(_base_params(platform="bili", keywords="考研", count=20,
                                         probe=True), st)
    assert vp["probe"] is True and vp["probe_auto_reason"] == ""
