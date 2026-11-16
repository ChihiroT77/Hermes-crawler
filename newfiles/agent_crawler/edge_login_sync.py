# -*- coding: utf-8 -*-
"""edge_login_sync.py —— 把桌面 Edge 的登录态搬给 MediaCrawler 的爬虫 profile。

为什么需要它
------------
MediaCrawler 每个平台用自己的 profile（browser_data/cdp_<平台>_user_data_dir），
是个全新的浏览器身份，登录票只能靠扫码现赚，过期就得再扫。
你桌面 Edge 里其实一直登录着，票是新鲜的 —— 本工具把这些票搬过去。

安全性
------
- 只在同一个 Windows 用户下解密（DPAPI），不联网传任何东西，不解锁明文到磁盘。
- 写之前自动备份目标 profile 的 Cookies 库（*.bak_年月日时分秒）。
- 桌面 Edge 正在运行时它的 Cookies 库是独占锁的，必须先关掉（Edge 会自动恢复标签页）。
  不加 --close-edge 时会停下并提示你手动关；加了就优雅关闭（taskkill 不带 /F），绝不强杀。

用法
----
  # 只看各平台登录票还有效吗（不开浏览器、不改任何文件）
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --check

  # 把桌面 Edge 的 B站 + 微博 登录态搬进爬虫 profile（需先关闭 Edge）
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --sync bili wb

  # 全搬（默认排除 小红书：这个按你的要求保持登出）
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --sync all --close-edge

  # 确实要让小红书也带上登录态时才显式点名
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --sync xhs --close-edge

  # 【推荐路线】新版 Edge 的 cookie 是 App-Bound 加密（v20），在本机解不开也搬不走。
  # 那就不搬文件，改成你把 cookie 值贴进来，本工具负责"翻译"成爬虫浏览器认得的东西：
  #   F12 → 网络(Network) → 随便点一个请求 → 右键"复制为 cURL" → 存成 txt
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --import wb --curl-file cookies_wb.txt
  # 或者直接贴一行 cookie 头：
  .venv\\Scripts\\python.exe agent_crawler\\edge_login_sync.py --import tieba --cookie "BDUSS=xxx; STOKEN=yyy"
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（MediaCrawler 所在目录）
BROWSER_DATA = os.path.join(ROOT, "browser_data")
DESKTOP_PROFILE = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")

# 平台 -> 需要搬运的 cookie 域名后缀
PLATFORM_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "bili": ("bilibili.com", "bilibili.cn", "biligame.com"),
    "wb": ("weibo.com", "weibo.cn", "sina.com.cn"),
    "dy": ("douyin.com",),
    "ks": ("kuaishou.com", "gifshow.com"),
    "xhs": ("xiaohongshu.com",),
    "tieba": ("baidu.com",),
    "zhihu": ("zhihu.com",),
}
# 判定"这个平台到底登录没登录"的登录票名称
# 注意：平台改版会换名字，这份清单只是"惯例"，不是唯一标准。
# 快手：爬虫自己认的是 passToken（media_platform/kuaishou/login.py:70），不是 web_st。
# 知乎：2026 年新版只发 SESSIONID，早就不发 z_c0 了；爬虫靠 /api/v4/me 问得通就算登录。
LOGIN_COOKIES: Dict[str, Tuple[str, ...]] = {
    "bili": ("SESSDATA",),
    "wb": ("SUB",),
    "dy": ("sessionid", "sessionid_ss", "sid_tt"),
    "ks": ("passToken", "kuaishou.server.web_st", "kuaishou.server.webday7_st"),
    "xhs": ("web_session",),
    "tieba": ("BDUSS", "BDUSS_BFESS"),
    "zhihu": ("z_c0", "SESSIONID"),
}
# 默认保持登出的平台（用户明确要求），除非显式点名
# 2026-11-15 改动：知乎从这份名单里去掉（用户要登录知乎真跑核对），小红书继续留在这
LOGGED_OUT_BY_DESIGN = ("xhs",)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")


# ---------------------------------------------------------------------------
# 加解密（v10/v11 = AES-256-GCM，密钥来自 Local State，DPAPI 保护）
# ---------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI 解密失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def master_key(profile_dir: str) -> bytes:
    with open(os.path.join(profile_dir, "Local State"), "r", encoding="utf-8") as f:
        state = json.load(f)
    raw = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    return dpapi_unprotect(raw)


def decrypt_value(key: bytes, blob: bytes, host_key: str) -> Tuple[str, bool]:
    """返回 (明文, 是否带域名指纹前缀)。"""
    if not blob:
        return "", False
    if blob[:3] in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
        digest = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plain[:32] == digest:
            return plain[32:].decode("utf-8", "replace"), True
        return plain.decode("utf-8", "replace"), False
    if blob[:3] == b"v20":
        raise RuntimeError("App-Bound 加密（v20），本机解不开")
    return blob.decode("utf-8", "replace"), False


def encrypt_value(key: bytes, text: str, host_key: str, with_hash: bool = True) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    payload = text.encode("utf-8")
    if with_hash:
        payload = hashlib.sha256(host_key.encode("utf-8")).digest() + payload
    nonce = os.urandom(12)
    return b"v10" + nonce + AESGCM(key).encrypt(nonce, payload, None)


# ---------------------------------------------------------------------------
# Cookies 库读写
# ---------------------------------------------------------------------------
def cookies_db(profile_dir: str) -> str:
    p = os.path.join(profile_dir, "Default", "Network", "Cookies")
    if os.path.isfile(p):
        return p
    return os.path.join(profile_dir, "Default", "Cookies")


def is_locked(path: str) -> bool:
    """浏览器开着时它的 Cookies 库连只读都打不开。"""
    try:
        with open(path, "rb"):
            return False
    except PermissionError:
        return True
    except OSError:
        return True
    except Exception:
        return True


class CookieDB:
    def __init__(self, path: str, writable: bool = False):
        self.path = path
        uri = f"file:{path}?{'mode=rw' if writable else 'immutable=1'}"
        self.con = sqlite3.connect(uri, uri=True)
        self.columns = [r[1] for r in self.con.execute("PRAGMA table_info(cookies)")]

    def rows(self, domains: Iterable[str]) -> List[dict]:
        cond = " OR ".join("host_key LIKE ?" for _ in domains)
        args = [f"%{d}" for d in domains]
        cur = self.con.execute(f"SELECT {','.join(self.columns)} FROM cookies WHERE {cond}", args)
        return [dict(zip(self.columns, r)) for r in cur]

    def upsert(self, row: dict) -> None:
        cols = ",".join(self.columns)
        marks = ",".join("?" for _ in self.columns)
        self.con.execute(f"INSERT OR REPLACE INTO cookies ({cols}) VALUES ({marks})",
                         [row.get(c) for c in self.columns])

    def close(self) -> None:
        self.con.commit()
        self.con.close()


# ---------------------------------------------------------------------------
# 登录态实测（不需要浏览器）
# ---------------------------------------------------------------------------
def _http(url: str, jar: Dict[str, str], referer: str = "") -> dict:
    header = "; ".join(f"{k}={v}" for k, v in jar.items()
                       if isinstance(v, str) and v and all(32 <= ord(c) < 127 for c in v))
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9", "Cookie": header, "Connection": "close"}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}")


def _jar_for(cookies: Dict[str, Dict[str, str]], domains: Tuple[str, ...]) -> Dict[str, str]:
    jar: Dict[str, str] = {}
    for host, dom in cookies.items():
        if any(host.endswith(d) for d in domains):
            jar.update(dom)
    return jar


def probe_login(platform: str, jar: Dict[str, str]) -> str:
    """返回人话结论。能直连验的就验，验不了的直说。"""
    need = LOGIN_COOKIES[platform]
    if not any(n in jar for n in need):
        return "FAIL 爬虫 profile 里没有登录票（这个平台需要贴一次 cookie 或扫码一次）"
    try:
        if platform == "bili":
            d = _http("https://api.bilibili.com/x/web-interface/nav", jar,
                      "https://www.bilibili.com/").get("data") or {}
            if d.get("isLogin"):
                return f"OK 已登录：{d.get('uname')}（uid {d.get('mid')}）"
            return "FAIL 登录票已失效"
        if platform == "wb":
            # 带票问一次、不带票问一次：返回的"全部关注"分组 uid 变了才算真登录
            # （未登录时微博给的是一个固定的匿名 uid，所以自校准，不写死常量）
            def _uid(with_cookie: bool) -> str:
                d = _http("https://weibo.com/ajax/feed/allGroups",
                          jar if with_cookie else {}, "https://weibo.com/")
                groups = d.get("groups") or []
                for g in groups:
                    for item in (g.get("group") or []):
                        if item.get("uid"):
                            return str(item["uid"])
                return ""
            mine, anon = _uid(True), _uid(False)
            if mine and mine != anon:
                return f"OK 已登录（关注分组 uid {mine}）"
            return "FAIL 登录票已失效（微博只认匿名身份）"
        if platform == "tieba":
            d = _http("https://tieba.baidu.com/f/user/json_userinfo", jar,
                      "https://tieba.baidu.com/").get("data") or {}
            if d.get("user_name"):
                return f"OK 已登录：{d.get('user_name')}"
            return "FAIL 登录票已失效"
        if platform == "zhihu":
            d = _http("https://www.zhihu.com/api/v4/me", jar, "https://www.zhihu.com/")
            if d.get("id"):
                return f"OK 已登录：{d.get('name')}"
            return "FAIL 登录票已失效"
    except Exception as e:
        return f"? 接口判定不可用（{e}），cookie 在但有效性未知"
    return "? cookie 在（该平台接口要签名，本机验不了有效性）"


def read_platform_cookies(profile_dir: str, platform: str):
    key = master_key(profile_dir)
    db = CookieDB(cookies_db(profile_dir), writable=False)
    try:
        rows = db.rows(PLATFORM_DOMAINS[platform])
    finally:
        db.close()
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        try:
            val, _ = decrypt_value(key, r["encrypted_value"], r["host_key"])
        except Exception:
            continue
        out.setdefault(r["host_key"], {})[r["name"]] = val
    return out


def cmd_check(only: Optional[List[str]] = None) -> int:
    print("=" * 68)
    print("各平台登录态实测（不开浏览器、不改文件）")
    print("=" * 68)
    any_bad = False
    targets = only or list(PLATFORM_DOMAINS)
    for platform in targets:
        if platform not in PLATFORM_DOMAINS:
            print(f"!! 不认识的平台 {platform}")
            continue
        prof = os.path.join(BROWSER_DATA, f"cdp_{platform}_user_data_dir")
        tag = f"{platform:6s}"
        if platform in LOGGED_OUT_BY_DESIGN:
            tag += "（按你的要求保持登出）"
        if not os.path.isdir(prof):
            print(f"{tag} 没有 profile → 从没在爬虫里开过这个平台")
            continue
        try:
            cookies = read_platform_cookies(prof, platform)
            verdict = probe_login(platform, _jar_for(cookies, PLATFORM_DOMAINS[platform]))
        except sqlite3.OperationalError as e:
            # 浏览器开着时 Cookies 库被独占，报的是 unable to open database file，
            # 极易被误读成"登录失效/库坏了"，所以单独说清楚。
            if "unable to open" in str(e):
                verdict = "读不到（这个平台的浏览器正开着，可能正在跑采集）→ 等它跑完再看，不是登录失效"
            else:
                verdict = f"出错: {type(e).__name__}: {e}"
        except Exception as e:
            verdict = f"出错: {type(e).__name__}: {e}"
        print(f"{tag} {verdict}")
        if verdict.startswith(("FAIL", "出错")):
            any_bad = True
    print("-" * 68)
    print("免扫码优先用扩展：在桌面 Edge 里登录好就行，扩展会自动导出，不用点按钮。")
    print("想让爬虫马上用上，也可以手动导一次：")
    print("  agent_crawler\\edge_login_sync.py --import-json all latest")
    print("扩展没装/不想装，再退回贴 cURL：--import <平台> --curl-file 文件")
    return 0 if not any_bad else 0


# ---------------------------------------------------------------------------
# 手动导入：把"复制为 cURL / 一行 cookie 头 / DevTools 的 cookie JSON"翻译进 profile
# ---------------------------------------------------------------------------
# 每个平台挑一个主域名承载登录票（爬虫访问的就是这个域，够用）
PRIMARY_HOST: Dict[str, str] = {
    "bili": ".bilibili.com",
    "wb": ".weibo.com",
    "dy": ".douyin.com",
    "ks": ".kuaishou.com",
    "xhs": ".xiaohongshu.com",
    "tieba": ".baidu.com",
    "zhihu": ".zhihu.com",
}

# 这些平台必须按原始域名逐条写：压平域名会让百度系在 tieba ↔ passport 之间 302 死循环
# ks 同理：登录票 passToken 只在 id.kuaishou.com 下有效，压到 .kuaishou.com 快手就不认（实测 No Login）
HOST_FAITHFUL_DEFAULT = {"tieba", "ks"}


def chrome_time(seconds_from_now: float = 0) -> int:
    """Chrome/Edge 的时间戳：1601-01-01 起的微秒数。"""
    unix = time.time() + seconds_from_now
    return int((unix + 11644473600) * 1_000_000)


def parse_cookie_input(text: str) -> Dict[str, str]:
    """支持三种粘贴格式，提取 name=value 对。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("没有拿到内容")
    # 1) DevTools 的 JSON（[{name,value,domain...}, ...] 或 {"cookies":[...]}）
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            data = data.get("cookies") or data.get("data") or None
        if isinstance(data, list):
            pairs = {}
            for item in data:
                if isinstance(item, dict) and item.get("name") and "value" in item:
                    pairs[item["name"]] = str(item["value"])
            if pairs:
                return pairs
    def _split(body: str) -> Dict[str, str]:
        pairs = {}
        for chunk in body.replace("\r", "").replace("\n", ";").split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                pairs[name] = value
        return pairs

    # 2) cURL：找 cookie 头。值里可能含引号（真 curl 会转义成 '\''），
    #    所以按行取整段和按引号配对取各算一遍，谁解析出的条数多用谁。
    low = text.lower()
    candidates: List[str] = []
    if "cookie:" in low:
        import re
        m = re.search(r"(?:-H\s+|--cookie\s+|-b\s+)?(['\"]?)cookie:\s*", text, re.I)
        if m:
            quote, rest = m.group(1), text[m.end():]
            line = rest.splitlines()[0] if rest.splitlines() else rest
            candidates.append(line.rstrip("\\").rstrip("\"'"))
            if quote:
                end = rest.find(quote)
                if end >= 0:
                    candidates.append(rest[:end])
            candidates.append(rest)
    else:
        import re
        m = re.search(r"(?:--cookie\s+|-b\s+)(['\"])(.*?)\1", text, re.I | re.S)
        if m:
            candidates.append(m.group(2))

    best: Dict[str, str] = {}
    for cand in candidates:
        got = _split(cand)
        if len(got) > len(best):
            best = got
    if best:
        return best
    # 3) 裸 "a=b; c=d"
    pairs = _split(text)
    if not pairs:
        raise ValueError("没解析出任何 cookie（期望 a=b; c=d 这种形式）")
    return pairs


def _row_template(db: CookieDB, platform: str) -> dict:
    """借用目标库里已有的一行当模板，缺省就用保守默认值。"""
    base = {
        "creation_utc": chrome_time(),
        "host_key": PRIMARY_HOST[platform],
        "top_frame_site_key": "",
        "name": "",
        "value": "",
        "encrypted_value": b"",
        "path": "/",
        "expires_utc": chrome_time(365 * 86400),
        "is_secure": 1,
        "is_httponly": 1,
        "last_access_utc": chrome_time(),
        "has_expires": 1,
        "is_persistent": 1,
        "priority": 1,
        "samesite": -1,
        "source_scheme": 2,
        "source_port": 443,
        "last_update_utc": chrome_time(),
        "source_type": 0,
        "has_cross_site_ancestor": 0,
    }
    try:
        rows = db.rows(PLATFORM_DOMAINS[platform])
    except Exception:
        rows = []
    if rows:
        tpl = dict(rows[0])
        tpl.update({k: v for k, v in base.items() if k not in tpl})
        tpl["encrypted_value"] = b""
        return tpl
    return base


SAME_SITE_BY_NAME = {"no_restriction": 0, "lax": 1, "strict": 2, "unspecified": -1}

# 有些票平台只下在"登录子域"，但爬虫只向主站取 cookie，就取不到。
# 快手实测：passToken 只在 id.kuaishou.com，爬虫只问 www.kuaishou.com，
# 于是爬虫认为没登录、去点"登录"按钮（真跑日志：waiting for locator("//p[text()='登录']")）。
# 这里把这些票额外复制一份到爬虫会读的主域，原域那份保持不动。
MIRROR_TO_READ_HOST: Dict[str, Tuple[str, ...]] = {
    "ks": ("passToken", "userId"),
}
READ_HOST: Dict[str, str] = {"ks": "www.kuaishou.com"}


def _wipe_domain(db: CookieDB, platform: str) -> int:
    """清掉某个平台域名下的旧 cookie（含子域）。

    百度系换票时必须清：profile 里自己攒的 BAIDUID/TIEBAUID 和桌面带来的票不是一套，
    两套并存会让 tieba ↔ passport 来回 302 踢成死循环。
    """
    wiped = 0
    for d in PLATFORM_DOMAINS[platform]:
        for pat in (d, "%." + d, "%" + d):
            cur = db.con.execute("DELETE FROM cookies WHERE host_key LIKE ?", (pat,))
            wiped += cur.rowcount or 0
    return wiped


def _write_rows_faithful(db: CookieDB, tkey: bytes, platform: str, cookies: list) -> list:
    """按导出里的原始域名/路径/属性逐条写入，返回 [(host, name, path, value)]。

    不能把整批压到一个域再写：域、path、Secure、SameSite 都参与服务端判定，
    压平之后 tieba 会认不出自己的会话（实测 302 死循环），原样写就正常。
    """
    tpl = _row_template(db, platform)
    wrote = []
    for c in cookies:
        host = c.get("domain") or ""
        name = c.get("name")
        value = c.get("value")
        if not host or not isinstance(name, str) or not name:
            continue
        if not isinstance(value, str) or not value:
            continue
        path = c.get("path") or "/"
        row = dict(tpl)
        row["host_key"] = host
        row["name"] = name
        row["value"] = ""
        row["path"] = path
        row["encrypted_value"] = encrypt_value(tkey, value, host, with_hash=True)
        row["is_secure"] = 1 if c.get("secure") else 0
        row["is_httponly"] = 1 if c.get("httpOnly") else 0
        row["samesite"] = SAME_SITE_BY_NAME.get(str(c.get("sameSite")).lower(), -1)
        ts = chrome_time()
        row["creation_utc"] = ts
        row["last_update_utc"] = ts
        row["last_access_utc"] = ts
        exp = c.get("expirationDate")
        if exp:
            row["has_expires"] = 1
            row["is_persistent"] = 1
            row["expires_utc"] = int((float(exp) + 11644473600) * 1_000_000)
        else:
            row["has_expires"] = 0
            row["is_persistent"] = 0
            row["expires_utc"] = 0
        db.con.execute("DELETE FROM cookies WHERE host_key=? AND name=? AND path=?",
                       (host, name, path))
        db.upsert(row)
        wrote.append((host, name, path, value))

    # 关键票再复制一份到爬虫实际会读的主域（原域那份保留）
    mirror_names = MIRROR_TO_READ_HOST.get(platform, ())
    read_host = READ_HOST.get(platform)
    if mirror_names and read_host:
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if name not in mirror_names or not isinstance(value, str) or not value:
                continue
            if (c.get("domain") or "").lstrip(".") == read_host:
                continue
            row = dict(tpl)
            row["host_key"] = read_host
            row["name"] = name
            row["value"] = ""
            row["path"] = "/"
            row["encrypted_value"] = encrypt_value(tkey, value, read_host, with_hash=True)
            row["is_secure"] = 1
            row["is_httponly"] = 0
            row["samesite"] = 1
            ts = chrome_time()
            row["creation_utc"] = ts
            row["last_update_utc"] = ts
            row["last_access_utc"] = ts
            exp = c.get("expirationDate")
            if exp:
                row["has_expires"] = 1
                row["is_persistent"] = 1
                row["expires_utc"] = int((float(exp) + 11644473600) * 1_000_000)
            else:
                row["has_expires"] = 0
                row["is_persistent"] = 0
                row["expires_utc"] = 0
            db.con.execute("DELETE FROM cookies WHERE host_key=? AND name=? AND path=?",
                           (read_host, name, "/"))
            db.upsert(row)
            wrote.append((read_host, name, "/", value))
    return wrote


def _encrypted_rows(db_path: str) -> dict:
    """把库里所有 (host, name, path) → 密文 读出来做回读比对。"""
    con = sqlite3.connect(db_path)
    try:
        return {(h, n, p): e for h, n, p, e in
                con.execute("SELECT host_key, name, path, encrypted_value FROM cookies")}
    finally:
        con.close()


def _apply_pairs(platform: str, pairs: Dict[str, str], seed_from: Optional[str],
                 dry_run: bool, force: bool, raw_cookies: Optional[list] = None,
                 host_faithful: bool = False) -> int:
    """共同落地流程：校验有没有登录票 → 平台接口验真 → 备份 → v10 加密写入 → 回读比对。

    不管票是从 cURL 贴来的、还是扩展导出的 JSON 来的，最后都走这里，只维护一套。
    """
    print(f"解析出 {len(pairs)} 条 cookie：{', '.join(sorted(pairs))}")
    need = LOGIN_COOKIES[platform]
    # 清单里列的是"可能叫这些名字"，有任意一个就算有票。
    # 以前写成"全都要有"，平台一改版（快手只剩 passToken）就全被拦下。
    if not any(n in pairs for n in need):
        print(f"!! 没看到登录票 {list(need)} —— 这份 cookie 里没有 {platform} 的登录凭据。")
        print("   通常说明：复制的时候页面没登录，或你复制的请求不是该平台的。")
        return 2

    verdict = probe_login(platform, pairs)
    print(f"先验一下有效性：{verdict}")
    if verdict.startswith("FAIL") and not force:
        print("!! 这票现在是失效的，导入也没用；确认要导入请加 --force")
        return 3
    if dry_run:
        print(f"[dry-run] 本应写入 {len(pairs)} 条到 cdp_{platform}_user_data_dir，未改动任何文件")
        return 0

    target = ensure_target_profile(platform, seed_from)
    db_path = cookies_db(target)
    if is_locked(db_path):
        print(f"!! {platform} 的爬虫浏览器正开着，Cookies 库被占用；先关掉它再导入")
        return 3
    backup = db_path + ".bak_" + datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(db_path, backup)
    tkey = master_key(target)
    db = CookieDB(db_path, writable=True)
    written = 0
    wrote_rows: list = []
    try:
        tpl = _row_template(db, platform)
        host = PRIMARY_HOST[platform]
        if host_faithful and raw_cookies:
            # 原样写：域名/路径/属性都照抄，先清掉旧票避免两套设备身份打架
            wiped = _wipe_domain(db, platform)
            print(f"按原始域名写入：先清掉旧 cookie {wiped} 条")
            wrote_rows = _write_rows_faithful(db, tkey, platform, raw_cookies)
            written = len(wrote_rows)
        else:
            for name, value in pairs.items():
                row = dict(tpl)
                row["name"] = name
                row["value"] = ""
                row["host_key"] = host
                row["encrypted_value"] = encrypt_value(tkey, value, host, with_hash=True)
                row["creation_utc"] = chrome_time()
                row["last_update_utc"] = chrome_time()
                row["last_access_utc"] = chrome_time()
                row["expires_utc"] = chrome_time(365 * 86400)
                # 同名同域的旧行先删，避免路径不同留两份
                db.con.execute("DELETE FROM cookies WHERE host_key=? AND name=?", (host, name))
                db.upsert(row)
                written += 1
    finally:
        db.close()

    # 回读：用目标自己的钥匙解回来比对
    got = _encrypted_rows(db_path)
    bad: list = []
    if host_faithful and raw_cookies:
        for h, n, p, v in wrote_rows:
            enc = got.get((h, n, p))
            ok_row = False
            if enc:
                try:
                    val, _ = decrypt_value(tkey, enc, h)
                    ok_row = (val == v)
                except Exception:
                    ok_row = False
            if not ok_row:
                bad.append(n)
    else:
        back = {}
        for (h, n, _p), enc in got.items():
            if h != host:
                continue
            try:
                back[n], _ = decrypt_value(tkey, enc, h)
            except Exception:
                pass
        bad = [n for n, v in pairs.items() if back.get(n) != v]
    print(f"写入 {written} 条 → {os.path.basename(target)}（备份 {os.path.basename(backup)}）")
    print("回读校验：" + ("OK 全部一致" if not bad else f"WARN 不一致 {bad}"))
    print(f"复查命令：agent_crawler\\edge_login_sync.py --check {platform}")
    return 0 if not bad else 4


def cmd_import(platform: str, cookie_text: str, seed_from: Optional[str],
               dry_run: bool, force: bool) -> int:
    """老路线：用户从 F12 贴 cURL / 一行 cookie 头。"""
    if platform not in PLATFORM_DOMAINS:
        print(f"!! 不认识的平台 {platform}；可选 {list(PLATFORM_DOMAINS)}")
        return 2
    if platform in LOGGED_OUT_BY_DESIGN and not force:
        print(f"!! {platform} 是你要求保持登出的平台；确实要导入请加 --force")
        return 2
    try:
        pairs = parse_cookie_input(cookie_text)
    except Exception as e:
        print(f"!! {e}")
        return 2
    return _apply_pairs(platform, pairs, seed_from, dry_run, force)


# ---------------------------------------------------------------------------
# 新路线：读 Edge 扩展导出的 JSON（点一下版，全程免扫码）
# ---------------------------------------------------------------------------
EXPORT_PREFIX = "mc_cookies_"


def _platforms_from_json(data) -> Dict[str, list]:
    """容忍几种写法：扩展的 {platforms:{k:{cookies:[...]}}}、{k:[...]}、裸 [ ... ]。"""
    if isinstance(data, dict) and isinstance(data.get("platforms"), dict):
        data = data["platforms"]
    out: Dict[str, list] = {}
    if isinstance(data, list):
        out["_raw"] = data
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(v.get("cookies"), list):
                out[k] = v["cookies"]
            elif isinstance(v, list):
                out[k] = v
    return out


def _pairs_for_platform(cookies: list, platform: str) -> Dict[str, str]:
    """把 cookie 列表压成 name→value。

    同一个名字可能在多个域名下各有一份，主域名那份最后写入、以它为准。
    """
    host = PRIMARY_HOST[platform]

    def is_primary(c) -> bool:
        d = c.get("domain") or ""
        return d == host or d.endswith(host)

    ordered = sorted(cookies, key=lambda c: 1 if is_primary(c) else 0)
    pairs: Dict[str, str] = {}
    for c in ordered:
        name, value = c.get("name"), c.get("value")
        if isinstance(name, str) and name and isinstance(value, str) and value:
            pairs[name] = value
    return pairs


def newest_export() -> Optional[str]:
    """找最新的扩展导出文件。

    不能只看"下载"文件夹：用户可能把 Edge 的下载目录改到别处，
    所以先读 Edge 偏好里设的目录。
    """
    dirs = []
    local = os.environ.get("LOCALAPPDATA", "")
    for prof in ("Default", "Profile 1", "Profile 2"):
        pref = os.path.join(local, "Microsoft", "Edge", "User Data", prof, "Preferences")
        if not os.path.isfile(pref):
            continue
        try:
            with open(pref, "r", encoding="utf-8", errors="replace") as f:
                conf = json.load(f)
            d = (conf.get("download") or {}).get("default_directory")
            if isinstance(d, str) and os.path.isdir(d):
                dirs.append(d)
        except Exception:
            pass
    home = os.path.expanduser("~")
    dirs += [os.path.join(home, "Downloads"), os.path.join(home, "下载")]

    cands = []
    seen = set()
    for d in dirs:
        if not d or not os.path.isdir(d) or d.lower() in seen:
            continue
        seen.add(d.lower())
        for fn in os.listdir(d):
            if fn.startswith(EXPORT_PREFIX) and fn.endswith(".json"):
                p = os.path.join(d, fn)
                try:
                    cands.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    if not cands:
        return None
    return max(cands)[1]


def cmd_import_json(target: str, path: str, seed_from: Optional[str],
                    dry_run: bool, force: bool, host_faithful: bool = False) -> int:
    """从扩展导出的 JSON 一次灌多个平台。target 写 all 表示全灌。"""
    if path == "latest":
        found = newest_export()
        if not found:
            print("!! 下载文件夹里没找到扩展导出的文件（文件名以 mc_cookies_ 开头）")
            print("   先在桌面 Edge 里把要采的平台登录好，等半分钟让扩展自己导；")
            print("   或者在扩展图标上点一下「全部导出」，再重新执行")
            return 2
        print(f"用最新的导出文件：{found}")
        path = found
    if not os.path.isfile(path):
        print(f"!! 文件不存在：{path}")
        return 2
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"!! 这个文件读不出来（要扩展导出的 JSON）：{type(e).__name__}: {e}")
        return 2

    per = _platforms_from_json(data)
    if not per:
        print("!! 文件里没认出平台数据；确认是扩展按钮导出的那份")
        return 2
    if target != "all":
        if target not in per:
            print(f"!! 这份文件里没有 {target}；里面有 {sorted(per)}")
            return 2
        per = {target: per[target]}

    print("=" * 68)
    print(f"从扩展导出文件导入：{os.path.basename(path)}")
    print("=" * 68)

    ok, skipped, failed = [], [], []
    for platform, cookies in per.items():
        if platform not in PLATFORM_DOMAINS:
            continue
        if platform in LOGGED_OUT_BY_DESIGN and not force:
            print(f"- {platform}：你要求保持登出，跳过（真要导加 --force）")
            skipped.append(platform)
            continue
        pairs = _pairs_for_platform(cookies, platform)
        faithful = host_faithful or platform in HOST_FAITHFUL_DEFAULT
        print("-" * 68)
        print(f"[{platform}] 导出文件里 {len(cookies)} 条 → 可用 {len(pairs)} 条")
        if faithful:
            print(f"[{platform}] 用原域名写入模式（{len(cookies)} 条逐条照抄域/路径/属性）")
        try:
            rc = _apply_pairs(platform, pairs, seed_from, dry_run, force,
                              raw_cookies=cookies, host_faithful=faithful)
        except Exception as e:
            print(f"!! {platform} 导入失败：{type(e).__name__}: {e}")
            rc = 9
        (ok if rc == 0 else failed).append(platform)

    print("=" * 68)
    print(f"结果：成功 {ok or '无'}｜跳过 {skipped or '无'}｜失败 {failed or '无'}")
    if not dry_run:
        print("复查：agent_crawler\\edge_login_sync.py --check")
    return 0 if ok else 3


# ---------------------------------------------------------------------------
# 关闭桌面 Edge（优雅）
# ---------------------------------------------------------------------------
def close_desktop_edge(wait_sec: int = 25) -> bool:
    db = cookies_db(DESKTOP_PROFILE)
    if not is_locked(db):
        print("  桌面 Edge 没在运行（或已可读），无需关闭")
        return True
    print("  正在优雅关闭桌面 Edge（不带 /F，不加 /T，等同点右上角 ×）…")
    subprocess.run(["taskkill", "/IM", "msedge.exe"], capture_output=True, text=True)
    for _ in range(wait_sec * 2):
        time.sleep(0.5)
        if not is_locked(db):
            print("  Edge 已完全退出，Cookies 库可读了")
            print("  提示：下次打开 Edge 时它会自动恢复标签页")
            return True
    print("  !! 等了 %d 秒 Edge 还没退干净，放弃（不会强杀）" % wait_sec)
    return False


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------
def ensure_target_profile(platform: str, seed_from: Optional[str]) -> str:
    """目标 profile 不存在就造一个最小骨架（Local State + 空 Cookies 库）。"""
    prof = os.path.join(BROWSER_DATA, f"cdp_{platform}_user_data_dir")
    db = cookies_db(prof)
    if os.path.isfile(db):
        return prof
    if not seed_from or not os.path.isdir(seed_from):
        raise RuntimeError(
            f"平台 {platform} 还没有 profile（{prof}），需要先用爬虫登录一次生成；"
            f"或指定 --seed-from 一个已有 profile 作模板")
    print(f"  平台 {platform} 还没 profile，从 {os.path.basename(seed_from)} 复制骨架…")
    net = os.path.join(prof, "Default", "Network")
    os.makedirs(net, exist_ok=True)
    shutil.copy2(os.path.join(seed_from, "Local State"), os.path.join(prof, "Local State"))
    for extra in ("Preferences",):
        src = os.path.join(seed_from, extra)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(prof, extra))
    seed_db = cookies_db(seed_from)
    shutil.copy2(seed_db, db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM cookies")
    con.commit()
    con.close()
    print(f"  已生成空骨架：{prof}")
    return prof


def sync_one(platform: str, src_key: bytes, src_db: CookieDB,
             seed_from: Optional[str], dry_run: bool) -> str:
    domains = PLATFORM_DOMAINS[platform]
    src_rows = src_db.rows(domains)
    planted = 0
    failed = 0
    prepped: List[dict] = []
    for r in src_rows:
        try:
            val, with_hash = decrypt_value(src_key, r["encrypted_value"], r["host_key"])
        except Exception:
            failed += 1
            continue
        row = dict(r)
        row["value"] = ""
        row["_plain"] = val
        row["_with_hash"] = with_hash
        prepped.append(row)

    jar = {}
    for r in prepped:
        jar.setdefault(r["host_key"], {})[r["name"]] = r["_plain"]
    src_verdict = probe_login(platform, _jar_for(jar, domains))
    print(f"  源（桌面 Edge）：共 {len(src_rows)} 条 cookie，解出 {len(prepped)} 条"
          + (f"，{failed} 条解不开" if failed else ""))
    print(f"  桌面登录状态：{src_verdict}")
    if not prepped:
        return src_verdict
    if dry_run:
        print(f"  [dry-run] 本应写入 {len(prepped)} 条到 cdp_{platform}_user_data_dir，未改动任何文件")
        return src_verdict

    target = ensure_target_profile(platform, seed_from)
    tdb_path = cookies_db(target)
    if is_locked(tdb_path):
        return f"SKIP 爬虫 {platform} 的浏览器还开着，Cookies 库被占用，先关掉它再搬"
    backup = tdb_path + ".bak_" + datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(tdb_path, backup)
    tkey = master_key(target)
    tdb = CookieDB(tdb_path, writable=True)
    try:
        for r in prepped:
            r["encrypted_value"] = encrypt_value(tkey, r["_plain"], r["host_key"], r["_with_hash"])
            tdb.upsert({k: v for k, v in r.items() if not k.startswith("_")})
            planted += 1
    finally:
        tdb.close()

    # 回读校验：用目标 profile 自己的钥匙解回来，逐条比对
    verify = CookieDB(tdb_path, writable=False)
    try:
        check_rows = verify.rows(domains)
    finally:
        verify.close()
    bad = 0
    expect = {(r["host_key"], r["name"]): r["_plain"] for r in prepped}
    for r in check_rows:
        key2 = (r["host_key"], r["name"])
        if key2 not in expect:
            continue
        try:
            got, _ = decrypt_value(tkey, r["encrypted_value"], r["host_key"])
        except Exception:
            bad += 1
            continue
        if got != expect[key2]:
            bad += 1
    status = "OK" if bad == 0 else f"WARN 有 {bad} 条回读不一致"
    print(f"  写入 {planted} 条 → {os.path.basename(target)}（备份 {os.path.basename(backup)}）")
    print(f"  回读校验：{status}")
    return src_verdict


def cmd_sync(platforms: List[str], close_edge: bool, dry_run: bool, seed_from: Optional[str]) -> int:
    if "all" in platforms:
        platforms = [p for p in PLATFORM_DOMAINS if p not in LOGGED_OUT_BY_DESIGN]
        print(f"all = {platforms}（小红书按你的要求保持登出；要搬请显式点名）")
    unknown = [p for p in platforms if p not in PLATFORM_DOMAINS]
    if unknown:
        print(f"!! 不认识的平台：{unknown}；可选 {list(PLATFORM_DOMAINS)}")
        return 2

    src_db_path = cookies_db(DESKTOP_PROFILE)
    if not os.path.isfile(src_db_path):
        print(f"!! 找不到桌面 Edge 的 Cookies 库：{src_db_path}")
        return 2
    if is_locked(src_db_path):
        if close_edge:
            if not close_desktop_edge():
                return 3
        else:
            print("!! 桌面 Edge 正在运行，它把 Cookies 库独占锁住了，读不到登录票。")
            print("   解决：先手动关掉所有 Edge 窗口后重跑本命令；")
            print("   或者加 --close-edge 让本工具帮你优雅关闭（会自动恢复标签页）。")
            return 3

    src_key = master_key(DESKTOP_PROFILE)
    print(f"桌面主密钥已解开（{len(src_key)} 字节），开始搬运\n")
    src_db = CookieDB(src_db_path, writable=False)
    results = {}
    try:
        for platform in platforms:
            print(f"== {platform} ==")
            try:
                results[platform] = sync_one(platform, src_key, src_db, seed_from, dry_run)
            except Exception as e:
                results[platform] = f"出错: {type(e).__name__}: {e}"
                print(f"  {results[platform]}")
            print()
    finally:
        src_db.close()

    print("=" * 68)
    print("汇总")
    for p, v in results.items():
        print(f"  {p:6s} {v}")
    print("=" * 68)
    if not dry_run:
        print("下一步：直接跑爬虫即可，正常不会再要你扫码。")
        print("  复查命令：agent_crawler\\edge_login_sync.py --check")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="把桌面 Edge 的登录态搬进 MediaCrawler 爬虫 profile（免扫码）")
    ap.add_argument("--check", nargs="*", metavar="平台",
                    help="只测登录票是否有效，不改文件；可跟平台名，留空=全部")
    ap.add_argument("--sync", nargs="+", metavar="平台", help="要搬的平台，或写 all")
    ap.add_argument("--import", dest="import_platform", metavar="平台",
                    help="手动导入 cookie：配合 --cookie / --curl-file")
    ap.add_argument("--import-json", dest="import_json", nargs=2,
                    metavar=("平台", "文件"),
                    help="从 Edge 扩展导出的 JSON 导入；平台写 all 全灌，"
                         "文件写 latest 表示用下载文件夹里最新那份")
    ap.add_argument("--cookie", help="直接贴一行 cookie 头：a=b; c=d")
    ap.add_argument("--curl-file", help="装着 F12『复制为 cURL』内容的 txt 文件（UTF-8）")
    ap.add_argument("--force", action="store_true", help="导入时无视『保持登出』或票失效的拦截")
    ap.add_argument("--host-faithful", dest="host_faithful", action="store_true",
                    help="按导出里的原始域名/路径/属性逐条写入（tieba 默认就是）")
    ap.add_argument("--close-edge", action="store_true", help="需要时自动优雅关闭桌面 Edge")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--source-dir", help="改用别的目录当来源（自测用）")
    ap.add_argument("--seed-from", help="目标 profile 不存在时用作骨架的已有 profile 目录")
    args = ap.parse_args(argv)

    global DESKTOP_PROFILE
    if args.source_dir:
        DESKTOP_PROFILE = os.path.abspath(args.source_dir)

    if args.import_json:
        target, path = args.import_json
        return cmd_import_json(target, path, args.seed_from, args.dry_run, args.force,
                               args.host_faithful)

    if args.import_platform:
        text = args.cookie or ""
        if args.curl_file:
            with open(args.curl_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        if not text.strip():
            print("!! 没有 cookie 内容：用 --cookie 'a=b; c=d' 或 --curl-file 文件名")
            return 2
        return cmd_import(args.import_platform, text, args.seed_from,
                          args.dry_run, args.force)

    if args.check is not None:
        return cmd_check(args.check or None)
    if args.sync:
        return cmd_sync(args.sync, args.close_edge, args.dry_run, args.seed_from)
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
