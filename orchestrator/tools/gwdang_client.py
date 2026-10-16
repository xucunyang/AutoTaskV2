# -*- coding: utf-8 -*-
"""gwdang_client：购物党（gwdang.com）HTTP 客户端（第二价格渠道）。

逆向成果（2026-10-15 实测；接口形状来源 greasyfork #416002 社区脚本）：
  * url_to_dp:  GET www.gwdang.com/api/url_to_dp?url=<商品URL>&t=<秒级时间戳>
                → {"dp_id": "<商城SKU>-<站点号>"}，如 100164458263-3（匿名可用）
  * data_www:   GET www.gwdang.com/trend/data_www?show_prom=true&v=2
                &get_coupon=1&dp_id=<dp_id>
                → series[{period,max,min,current,data:[{x:秒,y:分}]}]
                需登录 Cookie（GWD_USER_TOKEN/GWD_USER_TIME + dfp/fp，
                会话文件 data/gwdang_session.json）
  * WAF：www 页对非浏览器 503（m 站 200）、data_www 匿名 403——
    客户端只打 API、绝不请求页面
  * 风控：带 Cookie 也可能返回 {"is_ban": ...}（需浏览器过人机验证）

trend() 输出与 manmanbuy_client.Mmb.trend 同构（datePrice=[[毫秒,元],…]），
backfill.trend_to_series 可直接复用。第二渠道价值：慢慢买 4030 限流时顶班、
双曲线交叉验证、补慢慢买未收录实体。业务编排见 tools/manmanbuy_backfill.py。
"""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / "data" / "gwdang_session.json"   # data/ 已 gitignore
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class GwdangBlocked(RuntimeError):
    """is_ban 风控——需浏览器登录购物党过一次人机验证，Cookie 不一定失效。"""


def normalize_trend(payload) -> dict:
    """data_www 响应 → 与 Mmb.trend 同构的 dict。坏输入返回 {}（宁丢不炸）。"""
    if not isinstance(payload, dict):
        return {}
    series = payload.get("series")
    if not isinstance(series, list) or not series:
        return {}
    segs = [s for s in series if isinstance(s, dict) and isinstance(s.get("data"), list)]
    if not segs:
        return {}
    longest = max(segs, key=lambda s: len(s["data"]))
    pts = [[p.get("x"), p.get("y")] for p in longest["data"]
           if isinstance(p, dict) and isinstance(p.get("x"), (int, float))
           and isinstance(p.get("y"), (int, float))]

    def _meta(key, scale=100):
        v = longest.get(key)
        return v / scale if isinstance(v, (int, float)) else None

    out = {
        "datePrice": [[int(x * 1000), y / 100] for x, y in pts],
        "currentPrice": _meta("current"),
        "lowerPrice": _meta("min"),
        "higherPrice": _meta("max"),
        "periodDays": longest.get("period"),
        "spName": "",
    }
    if pts:
        out["firstIncTime"] = payload.get("first_inc_time")
    return out


class Gwdang:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA)]

    # ---------- http ----------
    def get(self, url: str, referer: str | None = None, xhr: bool = False,
            timeout: int = 25) -> str:
        req = urllib.request.Request(url)
        if referer:
            req.add_header("Referer", referer)
        if xhr:
            req.add_header("X-Requested-With", "XMLHttpRequest")
            req.add_header("Accept", "application/json, text/plain, */*")
        with self.op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    # ---------- 会话 ----------
    def load_session(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            d = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            for domain, name, value in d["cookies"]:
                c = http.cookiejar.Cookie(0, name, value, None, False,
                                          domain.lstrip("."), True,
                                          domain.startswith("."), "/", True,
                                          False, None, False, None, None, {})
                self.cj.set_cookie(c)
            return any(name == "GWD_USER_TOKEN" for _, name, _ in d["cookies"])
        except Exception:
            return False

    # ---------- 数据接口 ----------
    HOSTS = ("www.gwdang.com", "www2.gwdang.com")   # www2 为官方镜像（2026-10-15 实测同返回）

    def trend(self, url: str) -> dict:
        """商品链接 → 归一化历史价格。与 Mmb.trend 同构，另带 raw。

        主机级 fallback：www 连接失败/5xx 时换 www2 镜像；
        403（会话失效）与 is_ban（账号风控）不 fallback——同后端换了也一样。"""
        u1 = ("https://www.gwdang.com/api/url_to_dp?url="
              + urllib.parse.quote(url, safe="") + f"&t={int(time.time())}")
        dp = json.loads(self.get(u1, referer="https://www.gwdang.com/trend")).get("dp_id")
        if not dp:
            raise RuntimeError(f"购物党 url_to_dp 未返回 dp_id：{url}")
        time.sleep(1.5)   # 两跳之间保底间隔，节流大头由调用方控制
        last_err: Exception | None = None
        for host in self.HOSTS:
            u2 = (f"https://{host}/trend/data_www?show_prom=true&v=2"
                  f"&get_coupon=1&dp_id={dp}")
            try:
                payload = json.loads(self.get(u2,
                                              referer="https://www.gwdang.com/trend",
                                              xhr=True))
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise RuntimeError("购物党 403：Cookie 失效，重新导出 "
                                       "gwdang_session.json") from e
                last_err = e
                continue    # 5xx 等：换镜像
            except Exception as e:
                last_err = e
                continue
            if isinstance(payload, dict) and "is_ban" in payload:
                raise GwdangBlocked("购物党 is_ban：浏览器登录 gwdang.com 过一次人机验证")
            out = normalize_trend(payload)
            out["raw"] = payload
            out["host"] = host
            return out
        raise RuntimeError(f"购物党 trend 两台主机均失败：{last_err}")


if __name__ == "__main__":   # 调试入口：python -m tools.gwdang_client <商品URL>
    import sys
    c = Gwdang()
    if not c.load_session():
        print("无会话：先在 data/gwdang_session.json 放入登录 Cookie")
        raise SystemExit(2)
    d = c.trend(sys.argv[1])
    print("现价", d.get("currentPrice"), "| 最低", d.get("lowerPrice"),
          "| 最高", d.get("higherPrice"), "| 点数", len(d["datePrice"]),
          "| 周期", d.get("periodDays"), "天")
