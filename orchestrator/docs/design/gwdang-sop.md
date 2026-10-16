# 购物党（gwdang.com）数据采集 · SOP + 逆向调研进度（第二价格渠道）

> 调研时间：2026-10-15 ~ 10-16 · 状态：**client 已收编**（趋势数据可用），
> 管线接入（source 枚举/backfill 分发/第二轨入库）待做。
> 代码资产：`orchestrator/tools/gwdang_client.py`（客户端，trend() 与
> `manmanbuy_client.Mmb.trend` 同构）、`orchestrator/data/gwdang_session.json`
> （登录 Cookie，gitignore 内）。
> 关联文档：`manmanbuy-sop.md`（第一渠道）、`HANDOFF-2026-10-15-R36交接.md`。

---

## 0. 30 秒上手

```powershell
cd E:\code\AutoTaskByMuseSpark1.3\orchestrator
python -m tools.gwdang_client https://item.jd.com/100164458263.html
# → 现价 4999.0 | 最低 3914.1 | 最高 5099.0 | 点数 21 | 周期 360 天
```

Cookie 过期/失效的表现：`HTTP 403`（重新导出 `data/gwdang_session.json`）；
被风控的表现：`GwdangBlocked`（浏览器登录 gwdang.com 过一次人机验证）。

---

## 一、逆向调研进度时间线（全过程留痕）

| # | 时刻 | 动作 | 结果 |
|---|---|---|---|
| 1 | 10-15 晚 | 社区调研：greasyfork #416002《电商历史价格查询》脚本 | 拿到两步链形状：`url_to_dp` → `trend/data_www`，需登录 Cookie，含 `is_ban` 风控处理 |
| 2 | 10-15 晚 | 匿名探测 | `url_to_dp` **200**（dp_id=`100164458263-3`，规律=`京东SKU-3`）；`data_www` **403** |
| 3 | 10-15 晚 | REA 受控浏览器（headless/有头各一轮） | **www 页一律 503**（拦非白名单流量，与 headless 无关）；m 页 200 但是纯落地页（15KB 静态壳，无内嵌数据、不发 XHR） |
| 4 | 10-15 晚 | 模拟 m 站完整浏览流（Cookie 罐） | 不种 Cookie，`data_www` 仍 403 → **定案：登录 Cookie 是硬门槛** |
| 5 | 10-15 晚 | 用户注册并提供 Cookie | `data_www` **解锁**：4 段 series，现价 4999.0 与慢慢买同键一致 ✓ |
| 6 | 10-15 晚 | `gwdang_client.py` 落地 + 6 单测 + 实弹 | 提交 `51dbbbc`；全量 1011 passed |
| 7 | 10-16 晨 | 替代方案排查：官方插件 JS 4.8MB 解包 | **插件无趋势 API**（`price_trend` 是插件本地攒数）；i.gwdang.com 全是用户中心端点；签名机制摸清（见 §三.3） |
| 8 | 10-16 晨 | data_www 变体探测（8 种 host/路径/版本） | **`www2.gwdang.com` 为官方镜像（同返回）**；v1/无参返空、`trend/data*` 是 HTML 错误页、app 空、m 404 |
| 9 | 10-16 晨 | client 加 www2 主机 fallback | 提交 `bf9d833`；403/is_ban 不 fallback（同后端无意义） |

---

## 二、接口明细（全部实测）

### 2.1 url_to_dp（商品链接 → 内部 ID，**匿名可用**）

```
GET https://www.gwdang.com/api/url_to_dp?url=<urlencode(商品URL)>&t=<秒级时间戳>
→ {"dp_id": "100164458263-3"}
```

dp_id 规律：`<商城SKU>-<站点号>`（京东=3）。

### 2.2 trend/data_www（趋势数据，**需登录 Cookie**）

```
GET https://www.gwdang.com/trend/data_www?show_prom=true&v=2&get_coupon=1&dp_id=<dp_id>
Headers: Cookie(GWD_USER_TOKEN/GWD_USER_TIME/dfp/fp) + X-Requested-With + Referer
→ {
    "series": [                     # 多段嵌套窗口（30/90/180/360 天）
      { "period": 360,              # 窗口天数
        "max": 509900, "min": 391410, "current": 499900,   # 单位：分
        "data": [ {"x": 1760457600, "y": 434900}, ... ]    # x=秒, y=分
      }, ...],
    "promo_detail": ..., "analysis": ..., "first_inc_time": ..., "currency": ...
  }
```

归一化要点（`normalize_trend` 已处理）：**取最长段**；y÷100 变元；x×1000 变
毫秒；输出与 `Mmb.trend` 同构（`datePrice=[[毫秒,元],…]`）。曲线是**稀疏变价
点**（360 天约 22 点），与慢慢买的逐日点（411 点）形态不同，但管线
`collapse_daily` 按日坍缩后同轨。

### 2.3 promotion_discount（折扣爆料流，**匿名可用**）

```
GET https://www.gwdang.com/api/promotion_discount?format=json&page=1&ps=20&class=all
→ {"list": [{site_name, title, price_info, org_price, update_time, keyword}, ...]}
```

对标慢慢买的 `feed`，可作第二爆料源（尚未收编）。

---

## 三、风控模型（与慢慢买对照）

| 维度 | 慢慢买 trend | 购物党 data_www |
|---|---|---|
| 认证 | Web 登录 Cookie + tool 签名 + ticket | 登录 Cookie（`GWD_USER_TOKEN`，2027-11 到期） |
| 限流 | **24h 滚动窗口 ~80 次**（信号=裸数字 `4030`，跨午夜不重置，记账号不记 IP） | 未观测到窗口配额（未长期压测） |
| 风控形态 | 空数据/裸数字，静默 | `is_ban` 显式返回 → 浏览器过一次人机验证 |
| WAF | 无页面拦截 | **www 页拦非白名单流量（503）**，m 页开放；**API 直连不拦** |
| 失效处置 | 402 → 重新 login；4030 → 等窗口 | 403 → 重导 Cookie；is_ban → 浏览器过验证 |

### 签名机制（i.gwdang.com 用户 API，暂未用到，留档）

```
sign = md5( md5( 排序后 kv 串拼接 ) + ac )     # ac = 路径转点号，如 /User/Detail → user.detail
请求带 platform=ext & t=<秒> & sign=...
```

来源：官方插件 webpack 包（4.8MB，gzip 解包）逆向。

---

## 四、坑清单（每条都实测踩过）

1. **单位是分、时间是秒**——归一化 ÷100、×1000（与慢慢买的元/毫秒相反）
2. **series 多段嵌套窗口**——必须取最长段（360），拿 90 天段会只有 1-2 点
3. **别碰页面，只打 API**——www 页 503 拦非白名单流量，真 Chrome 也拦；
   模拟页面是死路，API 直连畅通
4. **插件 JS 是 gzip 裸发**——`Accept-Encoding: identity` 或手动 gzip 解压，
   否则拿到 1.2MB 二进制以为混淆
5. `v=1`、无参调用返回空 `[]`；`trend/data`、`trend/data_m` 是 HTML 错误页；
   `app.gwdang.com` 空、`m.gwdang.com` 404——**唯一活的路径就是
   `trend/data_www?show_prom=true&v=2&get_coupon=1`**
6. 用户导出的 Cookie 里一堆 Baidu 域的是登录流程噪音，**只要
   `GWD_USER_TOKEN`/`GWD_USER_TIME`（.gwdang.com）+ `dfp`/`fp`（www）四枚**
7. 变价点稀疏（22 点/年），做双源对照时按日坍缩再比，别逐点比

---

## 五、冗余结构（双渠道互备）

```
慢慢买 trend（24h 窗口 ~80 次，4030） ←→ 购物党 data_www（Cookie+is_ban）
                  └── 两个独立后端，风控不同源，互为替代 ──┘
购物党内部：www ⇄ www2 镜像（仅防主机级故障）
```

同款商品实测一致：魔鹰 5060Ti 16G 小雕（item.jd.com/100164458263）现价
4999.0 两源吻合（2026-10-15/16 各自实测）。

---

## 六、待办（管线接入，接 R36 交接 §4.5）

1. 契约 `source` 枚举加 `gwdang`
2. backfill 渠道分发（listing 注册表挂 channel 维度或独立 gwdang 注册表）
3. sku.db 落第二轨 → 看板双源叠加/对照视图
4. 长期观测：is_ban 触发频率、Cookie 有效期（GWD_USER_TOKEN 标称 2027-11，
   以实际为准）、www2 镜像可用性
5. 可选：`promotion_discount` 收编为第二爆料源

## 七、资产位置

| 资产 | 路径 |
|---|---|
| 客户端 | `orchestrator/tools/gwdang_client.py` |
| 单元测试 | `orchestrator/tests/test_gwdang_client.py`（6 项） |
| Cookie 会话 | `orchestrator/data/gwdang_session.json`（gitignore 内） |
| 探针/挖掘脚本（过程留痕） | `%TEMP%\opencode\probe_gwd*.py`、`mine_gwd_js*.py`、`gwd_ext.txt`（临时目录，可能被清理） |
| 需求台账 | `~/.ai-memory/requirements/manmanbuy.md` |
