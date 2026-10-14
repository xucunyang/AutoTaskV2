# -*- coding: utf-8 -*-
"""manmanbuy_client：慢慢买（manmanbuy.com）HTTP 客户端。

逆向成果（详见 docs/design/manmanbuy-sop.md，2026-10-14 实测）：
  * Web 登录: home.manmanbuy.com ASP.NET 表单（txtUser/txtPass/btnLogin）
              → 域级 Cookie 60014_mmmuser（tool 子站共享）
  * tool 签名: token = MD5((SECRET + Σ排序enc(k)+enc(v) + SECRET).upper()).upper()
  * ticket:   HistoryLowest.aspx 页 <input id=ticket> 值右旋 4 位，缓存 20 分钟
  * datePrice: 无外层括号的字符串 "[ts,价,备注],[ts,价,备注],…"，
              必须 json.loads("[" + s + "]")（2026-10-14 踩坑：直接 loads
              必败落空列表，401 个点变 0 个）。

本模块只做 HTTP 与解析——不懂实体、不懂契约；业务编排见
tools/manmanbuy_backfill.py（渠道适配器）。全链无模型参与。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOOL_SECRET = "c5c3f201a8e8fc634d37a766a0299218"
SPA_SECRET = "4e5244531g5rd4fvd0437fend72mf47b"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
SESSION_FILE = ROOT / "data" / "manmanbuy_session.json"   # data/ 已 gitignore，Cookie 不入库


class SessionExpired(RuntimeError):
    """402/未登录——唯一处置是人工重新 login（密码不进代码/配置）。"""


def parse_date_price(dp) -> list[list]:
    """datePrice 字段 → [[毫秒时间戳, 价格, 备注], …]。

    接口返回三种形态都见过：合规字符串（无外括号）、已是列表、空。
    解析失败返回 []——宁可丢曲线也不抛异常中断整批采集。
    """
    if isinstance(dp, list):
        return [x for x in dp if isinstance(x, list) and len(x) >= 2]
    if not (isinstance(dp, str) and dp.strip()):
        return []
    try:
        out = json.loads("[" + dp + "]")
        return [x for x in out if isinstance(x, list) and len(x) >= 2]
    except Exception:
        return []


def rotate4(v: str) -> str:
    """ticket 右旋 4 位（v[-4:] + v[:-4]）。"""
    return v[-4:] + v[:-4]


class Mmb:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA)]
        self.devid = uuid.uuid4().hex[:32]
        self._ticket_cache = ("", 0)

    # ---------- http ----------
    def _decode(self, raw):
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except Exception:
                pass
        return raw.decode("utf-8", "replace")

    def get(self, url, timeout=30):
        return self._decode(self.op.open(url, timeout=timeout).read())

    def post(self, url, data, headers=None, timeout=30):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body)
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
        for k, v in (headers or {}):
            req.add_header(k, v)
        try:
            return self._decode(self.op.open(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            return "HTTP %d %s" % (e.code, e.read()[:200])
        except Exception:
            return ""  # 302 跟随超时等：Cookie 已写入

    # ---------- 登录 ----------
    def login(self, user: str, password: str) -> bool:
        """ASP.NET 表单登录。POST 后 302 跟随可能超时——Cookie 已生效，
        属预期（2026-10-14 实测），异常忽略继续验 Cookie。"""
        html = self.get("https://home.manmanbuy.com/login.aspx")
        f = lambda n: (re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(n), html) or [None, ""])[1]
        m = re.search(r'name="(btnLogin)"[^>]*value="([^"]*)"', html)
        self.post("https://home.manmanbuy.com/login.aspx", {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "",
            "__VIEWSTATE": f("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": f("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": f("__EVENTVALIDATION"),
            "txtUser": user, "txtPass": password, "autoLogin": "on",
            (m.group(1) if m else "btnLogin"): (m.group(2) if m else "登陆"),
            "skey": ""})
        ok = any("mmmuser" in c.name for c in self.cj)
        if ok:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps(
                {"cookies": [(c.domain, c.name, c.value) for c in self.cj]},
                ensure_ascii=False, indent=1), encoding="utf-8")
        return ok

    def load_session(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            d = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            for domain, name, value in d["cookies"]:
                c = http.cookiejar.Cookie(0, name, value, None, False,
                                          domain.lstrip("."), True, domain.startswith("."),
                                          "/", True, False, None, False, None, None, {})
                self.cj.set_cookie(c)
            return any("mmmuser" in name for _, name, _ in d["cookies"])
        except Exception:
            return False

    # ---------- 签名 ----------
    @staticmethod
    def _enc(s):
        return urllib.parse.quote(str(s), safe="!~*'()")

    @classmethod
    def _sign_tool(cls, params: dict) -> str:
        s = TOOL_SECRET
        for k, v in sorted(params.items(), key=lambda kv: kv[0]):
            if v is not None and v != "":
                s += cls._enc(k) + cls._enc(v)
        s += TOOL_SECRET
        return hashlib.md5(s.upper().encode()).hexdigest().upper()

    @classmethod
    def _sign_spa(cls, params: dict) -> str:
        e = {cls._enc(k): cls._enc(v) for k, v in params.items()}
        o = "".join(k + v for k, v in sorted(e.items()) if v != "")
        return hashlib.md5((SPA_SECRET + o + SPA_SECRET).upper().encode()).hexdigest().upper()

    def _ticket(self) -> str:
        ts, t0 = self._ticket_cache
        if ts and time.time() - t0 < 1200:
            return ts
        page = self.get("https://tool.manmanbuy.com/HistoryLowest.aspx")
        m = re.search(r'id="ticket"\s+value="([^"]+)"', page)
        if not m:
            raise SessionExpired("HistoryLowest.aspx 拿不到 ticket（可能需要重新 login）")
        ts = rotate4(m.group(1))
        self._ticket_cache = (ts, time.time())
        return ts

    # ---------- 数据接口 ----------
    def trend(self, url: str) -> dict:
        """历史价格走势。url: 京东/天猫等商品链接。datePrice 已解析为列表。"""
        p = {"method": "getHistoryTrend", "key": url, "t": str(int(time.time() * 1000))}
        p["token"] = self._sign_tool(p)
        raw = self.post("https://tool.manmanbuy.com/api.ashx", p, [
            ("Authorization", "BasicAuth " + self._ticket()),
            ("X-Requested-With", "XMLHttpRequest"),
            ("Referer", "https://tool.manmanbuy.com/HistoryLowest.aspx")], 40)
        if raw.startswith("HTTP 402"):
            raise SessionExpired("tool 接口 402：会话过期，请人工执行 "
                                 "python -m tools.manmanbuy_backfill login")
        try:
            j = json.loads(raw)
        except Exception:
            raise SessionExpired(f"tool 接口返回非 JSON（会话可能失效）：{raw[:120]!r}")
        d = j.get("data") or {}
        d["datePrice"] = parse_date_price(d.get("datePrice"))
        return d

    def items(self, title: str, spbh: str) -> list:
        """同款比价（全网各店价格）。GPU 品类常返 0 条（2026-10-14 实测）。"""
        p = {"method": "getSearchItems", "title": title, "spbh": spbh,
             "t": str(int(time.time() * 1000))}
        p["token"] = self._sign_tool(p)
        raw = self.post("https://tool.manmanbuy.com/api.ashx", p, [
            ("Authorization", "BasicAuth " + self._ticket()),
            ("X-Requested-With", "XMLHttpRequest"),
            ("Referer", "https://tool.manmanbuy.com/HistoryLowest.aspx")], 40)
        if raw.startswith("HTTP 402"):
            raise SessionExpired("tool 接口 402：会话过期")
        return (json.loads(raw).get("data") or []) if raw else []

    def feed(self, page: int = 1) -> list:
        """折扣/爆料流（免登录，App 协议）。"""
        p = {"methodName": "getcuxiaolist3340", "page": page,
             "t": str(int(time.time() * 1000)), "jsoncallback": "?",
             "mmbyhm": "", "username": "", "u_name": "", "u_avatar": "", "sign": "",
             "c_individ": "", "c_appver": "5.2.40", "c_ostype": "android", "c_osver": "14",
             "c_devid": self.devid, "c_mmbDevId": "", "c_systemDevId": ""}
        raw = self.post("https://apapia-sqk.manmanbuy.com/index_json.ashx", p, [], 40)
        s = raw.strip()
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1]
        return json.loads(s)


if __name__ == "__main__":   # 调试入口：python -m tools.manmanbuy_client <商品URL>
    import sys
    c = Mmb()
    if not c.load_session():
        print("未登录：python -m tools.manmanbuy_backfill login")
        raise SystemExit(2)
    d = c.trend(sys.argv[1])
    print(d.get("spName"), "| 现价", d.get("currentPrice"),
          "| 最低", d.get("lowerPrice"), "| 点数", len(d["datePrice"]))
