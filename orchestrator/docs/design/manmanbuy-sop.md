# 慢慢买（manmanbuy）数据采集 · SOP（原交接文档，2026-10-14 已程序化收编）

> 交接时间：2026-10-14 · 状态：**已收编为确定性模块**（R27），采集/转换/
> 入库全链零模型参与。本文保留逆向知识与踩坑清单。
> 代码资产：`orchestrator/tools/manmanbuy_client.py`（客户端）、
> `orchestrator/tools/manmanbuy_backfill.py`（渠道适配器）、
> `orchestrator/config/backfill_manmanbuy.yaml`（listing 注册表）。

---

## 0. 30秒上手（程序化后的正规入口）

```powershell
cd E:\code\AutoTaskByMuseSpark1.3\orchestrator

# 一次性登录（密码不落盘，会话存 data/manmanbuy_session.json）
python -m tools.manmanbuy_backfill login

# 按注册表采集→按日坍缩→转换→入库（全链无 AI）
python -m tools.manmanbuy_backfill run
python -m tools.manmanbuy_backfill run --dry-run        # 只看行数不落盘
python -m tools.manmanbuy_backfill run --from-cache artifacts\2026-10-14\backfill_manmanbuy_raw.json  # 离线重放

# 调试单个商品
python -m tools.manmanbuy_client https://item.jd.com/100211023968.html
```

新 listing 上架流程（唯一需要人的环节）：searchbox 搜索 → 勾选双锚写
提案 → 人工核定 channel → 誊入 `config/backfill_manmanbuy.yaml`（含
verified 时刻）。详见 §1.1 品类采集规则 v1。

期望输出：`名称 / 现价 / 最低 / 60日均价 / 价格点`。若提示 402 或"未登录"，见 §3 重新登录。

---

## 1. 能力现状（2026-10-14 晚更新：四大能力全通）

| 数据 | 状态 | 接口 | 说明 |
|---|---|---|---|
| **商品历史价格走势** | ✅ 可用 | tool `/api.ashx` `getHistoryTrend` | 全量曲线（5060Ti 410 点/13.5 个月）；`datePrice` 无外括号须补 `[ ]` 再 loads |
| **关键词搜索全网** | ✅ 可用 | apapia-search `/index_json.ashx` `getsearchkeylist` | **免登录**！34 条/页，含 sales 销量/monthComment 月评/prourl 直达链接 |
| **销量 Top+商品地址** | ✅ 可用 | 同上 + 客户端 sales 排序 | `search_top()` page1+2 去重按销量降序 |
| **同款各店比价** | ✅ 可用 | 精确标题搜索 → 价格升序 | `compare_price()`；批量富化 `japi/searchpriceBySpbh3` 免 token |
| **单品到手价/促销** | ✅ 可用 | `/japi/appsearching/prom2Extra` `sku_compare_price` | 免 token；返回原价/预计到手价 |
| 折扣/爆料流 | ✅ 可用 | apapia-sqk `getcuxiaolist3340` | 免登录 |
| 同款比价(旧) | ⚠️ 弃用 | tool `getSearchItems` | GPU 常返 0 条，已被 compare_price 取代 |
| combineSearch/v3 | ❌ 放弃 | `/japi/multipleSearch/combineSearch` | token 在爱加密原生拦截器，Web 侧无解；功能已被 getsearchkeylist 老协议绕过 |

**「全网最低价」三层结论**：① 单商品历史最低 ✅（trend 的 lowerPrice/lowerDate）；
② 同款全网各店比价 ✅（compare_price 价格升序）；③ 关键词级搜索 ✅（search）。

## 1.1 品类采集规则 v1（2026-10-14 用户定稿）

每实体挂**双锚**，发现→打勾→回填全链程序化：

1. **销量锚**：`search_top` 规格匹配里销量第一——市场主流成交价；
2. **价格锚**：`compare_price` 价格升序第一个正规链接——R29 最低价地板；
3. 双锚硬门槛：siteid∈{1 京东, 10 天猫}、URL 可解析为 item.jd.com 详情页、
   trend 试采有点数、规格子串匹配带**后缀串台防线**（5070Ti 混不进 5070 桶）；
4. 发现入口：看板顶部 searchbox（勾选写 data/manmanbuy_proposals.yaml 提案）
   或 `python -m tools.manmanbuy_backfill discover`（候选表落 artifacts/）；
5. 人工只做一件事：核定 channel（自营/第三方）后誊入
   `config/backfill_manmanbuy.yaml`——注册表仍是唯一采集依据；
6. 节流 **2~7 秒随机**（`--sleep-min/--sleep-max` 可调）：固定间隔是可被
   识别的机器指纹；并发采集已评估并否决（省 3 分钟 vs 账号封禁风险）。

---

## 2. 工具 `manmanbuy_api.py` 用法

```powershell
python manmanbuy_api.py login <手机号> <密码>     # 登录并保存会话到 mmb_session.json
python manmanbuy_api.py trend <商品URL>           # 历史价格（dict: spName/currentPrice/lowerPrice/lowerDate/avgPrice60/datePrice/points）
python manmanbuy_api.py feed [页码]               # 折扣流
python manmanbuy_api.py items <标题> <spbh>       # 同款比价
```

也可 `import` 复用：`Mmb().login() / load_session() / trend(url) / feed(page) / items(title,spbh) / prom2(key)`。
`trend()` 返回的 `datePrice` 已解析为 `[时间(ms), 价格, 备注]` 列表，可直接画图。

依赖：仅 Python 3.13 标准库（urllib），无需第三方包。

---

## 3. 认证（核心，采集必须懂）

### 3.1 Web 登录（主力，已验证）

- 地址：`https://home.manmanbuy.com/login.aspx`（ASP.NET WebForms）
- 参数：`__VIEWSTATE`、`__VIEWSTATEGENERATOR`、`__EVENTVALIDATION`（从登录页 HTML 抓）、`txtUser`、`txtPass`、`autoLogin=on`、`btnLogin=登陆`、`skey=`
- 成功标志：拿到 Cookie **`60014_mmmuser`**（域 `.manmanbuy.com`，tool 子站共享）+ `mmbuser_ext`
- ⚠️ 登录 POST 后 302 跟随可能超时——**Cookie 已写入，忽略该异常继续即可**（工具已做容错）
- 账号：手机号 `15850730219`（用户本人账号，密码见用户口述；不要把密码写进任何日志/记忆文件）
- 会话持久化：`mmb_session.json`（`Mmb.load_session()` 自动恢复）

### 3.2 tool 签名 + 票据（每次采集都要）

```
SECRET   = "c5c3f201a8e8fc634d37a766a0299218"
token    = MD5( (SECRET + Σ 按key排序的 enc(k)+enc(v) + SECRET).upper() ).upper()
enc      = encodeURIComponent（Python: quote(s, safe="!~*'()")，空值跳过）
Header   = Authorization: BasicAuth <ticket>
ticket   = GET tool.manmanbuy.com/HistoryLowest.aspx 页面里 <input id="ticket" value="X">
           的值右旋4位：v[-4:] + v[:-4]
```
票据可缓存 ~20 分钟；**遇到 402 就重新登录 + 重取票据**。

### 3.3 错误码速查

| 码 | 含义 | 处置 |
|---|---|---|
| `code:0` (api.ashx) | 成功 | — |
| HTTP body `402` | 需要登录 | 重新 Web 登录 |
| `10006` "token校验失败" | Java 系接口，需 App 端 u_token（未解决，见 §6） | 避开这些接口 |
| `7700` + `restrictAction:requestLogin` | 同上（Web 会话不够） | 避开 |
| `{"data":"emplety"}` | 旧 .NET 趋势服务已下线（sqk/search host） | 别再试这条老路 |
| `[{"msg":"0"}]` | index_json 类接口的"成功" | — |

---

## 4. 接口明细（已验证）

### 4.1 历史价格（主力）

```
POST https://tool.manmanbuy.com/api.ashx
Content-Type: application/x-www-form-urlencoded
Authorization: BasicAuth <ticket>

method=getHistoryTrend & key=<商品URL> & t=<毫秒时间戳> & token=<签名>
```
返回 `data`：`spName` `spName` `siteName` `spbh` `currentPrice` `lowerPrice` `lowerDate` `avgPrice60` `changePriceRemark` `spUrl` `spPic` `datePrice`

⚠️ `datePrice` 是**字符串**：`"[ts,价格,备注],[ts,价格,备注],..."`（无外层括号）
解析：`json.loads("[" + s + "]")` → 每项 `[毫秒时间戳, 价格, 备注文本]`

商品 URL 从哪来：京东 SKU 可用 DuckDuckGo HTML 搜 `商品名 item.jd.com`（`https://html.duckduckgo.com/html/?q=...`）解析 `item.jd.com/数字.html`。样例：iPhone 17 Pro Max = `100278221408`。

### 4.2 折扣/爆料流（免登录）

```
POST https://apapia-sqk.manmanbuy.com/index_json.ashx
methodName=getcuxiaolist3340 & page=N
+ App公共参数: t=<ms> jsoncallback=? mmbyhm= username= u_name= u_avatar= sign=
  c_individ= c_appver=5.2.40 c_ostype=android c_osver=14 c_devid=<32位hex> c_mmbDevId= c_systemDevId=
```
返回被 `( ... )` 包裹 → 去括号再 `json.loads`。字段：`id/title/subTitle/img/mall/link/golink/spbh/currentprice/priceTag/showTrend/monitorText...`

### 4.3 SPA 签名（s.manmanbuy.com 一侧）

```
SECRET = "4e5244531g5rd4fvd0437fend72mf47b"
token  = MD5( (SECRET + Σ 排序enc(k)+enc(v) + SECRET).upper() ).upper()   # 公式同 tool，密钥不同，无 ticket
```
已验证可用于 `prom2Extra`。

### 4.4 App 端登录（拿到 u_token 才能进 Java 接口，**尚未走通**）

- 账号密码：`POST index_json.ashx` `methodName=signin & u=enc(账号) & p=enc(密码) & c=users & fpzy_controls=noOperation` → 返回 `sign` 即 u_token（实测能拿到，但打不开 Java 接口）
- 短信：`methodName=sendMsg_kuaijie & mobile=<手机号> & fpzy_controls=noOperation` → 返回 `[{"msg":"0"}]` 但**实际收不到短信**（疑似需要数美风控真实票据，`noOperation` 是"无滑块"占位）
- DES（算 `mmbyhm` 用）：`keyDES='4d16-0cd3-cf5c-04f9-057e-11c3-9438-ebc0'` → `dealKey = split('-').reverse().join('')` → **取前8字节** `ebc09438` 做 DES-ECB-PKCS7，输出 hex。已用 Node crypto-js 对拍验证一致

---

## 5. 坑与注意事项（血泪清单）

1. **编码**：慢慢买页面有 gb18030；PowerShell 内联写含中文的脚本会乱码 → 用 `Set-Content + here-string(@'...'@)` 写 UTF8 文件再 `python 执行`
2. **磁盘**：C 盘空间 <0.5GB 时 write 工具会静默失败 → 先 `Get-PSDrive C` 查剩余空间
3. **datePrice 别逐字符解析**：它是整段字符串，先补外括号再整体 loads（工具已处理）
4. **登录 302 超时**不是失败，Cookie 已生效，捕获异常继续
5. **频率**：未知限速；实测 1 请求/2秒 无任何封锁。**不要刷短信接口**
6. 老趋势方法（`getHistoryTrend2021`/`trendJava` 走 sqk/search host）全部返回 `emplety`，是死接口，别浪费时间
7. **trend 返「合法 JSON 但空数据」先查 URL 完整性**（2026-10-15 R36 定案）：`haveTrend=0, spbh=None, datePrice=[]` 的真凶是 **jingfen 锚 URL 被截断**（必须完整 `…detail/<token>_3<spbh>.html`，截断即静默空数据、不报错）——上会话曾误诊为"账号级配额冷却"，干等 75 分钟。判别顺序：**①核 URL 形状（是否完整）→ ②逐锚体检定位个别/全局 → ③才怀疑接口**。另：tool 接口裸数字 `4030`（非 dict）= **账号×PC网页通道级标记（10-16 19:4x 终版：重登新会话/换IP/真Chrome同会话均仍 4030，App 通道独立计数故手机正常），语义以页面 JS 为准**（histroyTrendExtend.js：4030→crawling() 弹 no_access 模态框，官方文案「频繁操作触发风控，请联系客服解除」；403→跳 /aliVal.aspx 阿里云人机验证）。24h/自然日等时间窗假设均被证伪（15 日 12:10 触发，16 日 19:4x 仍限）。**解封=联系客服；期间 gwdang 顶班**（gwdang-sop.md）。trend() 已带 5s 重试，但对 4030/403 无效。纪律：trend 日预算 ≤40 发，勿再触发

## 6. 逆向资料位置（需要深挖时）

| 资料 | 位置 |
|---|---|
| 采集客户端 | `orchestrator/tools/manmanbuy_client.py`（2026-10-14 18:07 版迁移+datePrice 修复） |
| 渠道适配器 | `orchestrator/tools/manmanbuy_backfill.py`（registry→采集→坍缩→转换→入库） |
| listing 注册表 | `orchestrator/config/backfill_manmanbuy.yaml`（人工甄别一次，程序复用） |
| 会话文件 | `orchestrator/data/manmanbuy_session.json`（gitignore 内，Cookie 不入库） |
| 转换层 | `orchestrator/tools/sku_backfill.py`（P0 既有，渠道无关） |
| 入库器 | `orchestrator/tools/sku_ingest.py`（唯一写库入口，既有） |
| APK | `E:\Downloads\manmanbuy.apk`（com.manmanbuy.bijia 5.2.40，爱加密只加密 dex，RN bundle 明文） |
| RN 反编译 | `%TEMP%\opencode\hbc\decomp.js`（109MB，可用 Scoop 的 hbc-decompiler 从 bundle 再生） |
| Web Next.js chunks | `%TEMP%\opencode\nextjs\`（SPA 签名模块 150/334/599） |
| App 内 H5 | `%TEMP%\opencode\h5\`（baseJavaUrl = https://apapia-history-weblogic.manmanbuy.com） |
| 反编译器 | `D:\development\program\Scoop\apps\python313\current\Scripts\hbc-decompiler.exe <in> <out>` |

⚠️ `%TEMP%\opencode` 可能被系统清理，核心是 tools/ 下两个模块（已入项目目录），其余可按上表重新生成。

---

## 7. 待办（新会话的活）

1. **关键词搜索列表**（价值最高，未通）：三条备选路
   a. 用一加平板登录 App（需先解决短信收不到，或用已登录的真机）拿 u_token → 打通 Java 接口（顺带解锁 v3 历史价格）
   b. 逆向 s.manmanbuy.com 搜索列表真正的数据接口（page chunk 里 queryKey=`pc_search_list`，找它的 fetch 来源）
   c. 解析搜索页 SSR 的 RSC flight 数据（`self.__next_f.push`）
2. **同款比价 sparse**：`getSearchItems` 多换几个热门商品验证，确认是数据问题还是参数问题
3. **prom2Extra 的 productInfos**：入参 shape 在 SPA chunk 的 `handlePromotionItem`（page-7546*.js），逆出来就能拿单品到手价/促销
4. 采集规模化时注意控制频率，并观察 402/7700 是否出现（出现即降速）

---

## 8. 验收样例（本会话实测留档）

- iPhone 17 Pro Max（京东 100278221408）：401 个价格点，首发 9999（2025-09-10）→ 现价 8499，历史最低 7899（2026-05-26），60日均价 8679.06
- 成品报告：`E:\code\AutoTaskByMuseSpark1.3\iPhone17ProMax_历史价格.md`（含 mermaid 折线图、12 个关键降价事件、月度明细）
