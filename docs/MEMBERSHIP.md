# MEMBERSHIP — 会员购买（Stripe）设计方案

> 状态：**✅ 已实现（v0.7.0，2026-09-15）**——实现细节与验证见 [CHANGELOG.md](CHANGELOG.md) 0.7.0；运维操作（Stripe 密钥/建价格/CLI 转发/测试卡/离线测试）见 [STRIPE-SETUP.md](STRIPE-SETUP.md)。
> 技术细节以本文为准，已同步 [DESIGN.md](DESIGN.md) §7 / [API.md](API.md) §12-13 / [SECURITY.md](SECURITY.md) §10 / [CHANGELOG.md](CHANGELOG.md) / [PLAN.md](PLAN.md) M2.10。

## 0. 已人工确认的决策

| 项 | 决策 |
|---|---|
| 身份模型 | **邮箱 + 密码账户体系**：注册 / 登录 / 退出；会员绑定账户，任意设备登录即享权益 |
| 付费模式 | **一次性购买会员时长**（Stripe Checkout `mode=payment`）：月卡 30 天、年卡 365 天；到期可再买，**重复购买叠加天数**；不做自动续费 |
| 测试环境 | 本机可访问 stripe.com，用 **Stripe CLI** 把 Webhook 转发到 localhost（无需公网域名 / HTTPS） |
| PRO 权益 | ① 1080p / 4K 超清下载；② 字幕翻译；③ AI 功能不限次 + 更高限流 |
| 免费保留 | 单链接下载（≤720p）、**批量下载（维持免费）**、字幕「仅提取」、AI 功能每日 3 次 |
| 数据库 | **SQLite**（Python 标准库 `sqlite3`，零额外服务，契合 `--workers 1` 单进程部署） |
| 支付通道 | **Stripe Checkout 托管收银台**（本站不接触卡号）+ 官方 Python SDK `stripe`（v8+ `StripeClient`） |

### v1 有意不做（如不认可请在审批时提出）

- ❌ 邮箱验证、❌ 忘记密码 / 自动找回（两者都需要邮件发送服务 SMTP，本期不引入）。
  - 注册页明确提示「请牢记邮箱与密码，本站暂不提供密码找回」；登录页同提示。
  - 运营兜底：数据库可查邮箱，后续接 SMTP 后再补验证/找回。
- ❌ 自动续费、发票、退款自助流程（Stripe Dashboard 可人工退款，退款后本期不自动回收会员，记入 v2）。
- ❌ 第三方登录（微信/Google 等）。

---

## 1. Stripe 入门：你需要做什么（傻瓜版操作指南）

> Stripe = 海外收单机构。用户在 **Stripe 托管的收银台**输卡，Stripe 收到钱后用 **Webhook（服务器回调）** 通知本站，本站据此开通会员。本站全程不接触银行卡信息。

### 1.1 注册与拿密钥（测试模式全程免费，不扣真钱）

1. 打开 <https://dashboard.stripe.com> 用邮箱注册（公司/国家信息测试模式可随便填）。
2. 确认右上角处于 **「测试模式 / Test mode」**（橙色开关）。
3. 进入 **开发者 → API 密钥（Developers → API keys）**：
   - **可发布密钥** `pk_test_...`：前端可用，不保密（本期实际前端不直接用，跳转由后端发起）。
   - **密钥** `sk_test_...`：点「Reveal test key」显示，**只放服务端 `.env`，绝不进前端/不入 git**。

### 1.2 创建两个商品价格（Products → Prices）

在 **产品目录（Products）** 里创建 1 个产品「PRO 会员」，下面建 2 个一次性价格（**One-off / One-time，不是 Recurring**）：

| 价格用途 | 建议金额 | 说明 |
|---|---|---|
| 月卡 | 例如 `$2.99 USD`（对应页面 ¥19） | 一次性付款，开通 30 天 |
| 年卡 | 例如 `$29.99 USD`（对应页面 ¥199） | 一次性付款，开通 365 天 |

> ⚠️ Stripe **不支持人民币（CNY）收单**，测试与海外收款用 USD 等币种；页面中文展示价与 Stripe 收款币种在 v1 解耦（后端套餐表配置「展示价」文案，真实扣款金额以后台 Price 为准）。
> 每个价格创建后获得 `price_...` ID，分别填入 `.env` 的 `STRIPE_PRICE_MONTH` / `STRIPE_PRICE_YEAR`。
> **金额以后台 Price 为唯一事实源**，后端下单只传 price ID，前端永远无法指定/篡改金额。

### 1.3 安装并登录 Stripe CLI（让本机收到支付回调）

```powershell
# Windows 推荐：winget install Stripe.StripeCLI  （或 scoop install stripe）
stripe login            # 会打开浏览器授权，选测试模式账户，回车确认
```

启动回调转发（本站服务起来后另开一个终端运行）：

```powershell
stripe listen --forward-to localhost:8000/api/billing/stripe/webhook
# 输出：> Ready! Your webhook signing secret is whsec_xxxxx
# 把 whsec_xxxxx 填入 .env 的 STRIPE_WEBHOOK_SECRET（Ctrl+C 后该密钥仍有效）
```

CLI 登录/转发要求**本机能访问 api.stripe.com**；不需要公网 IP、域名、HTTPS。

### 1.4 测试卡（测试模式固定卡号）

| 场景 | 卡号 | 其余 |
|---|---|---|
| 支付成功 | `4242 4242 4242 4242` | 有效期：任意未来日期（如 12/30）；CVC：任意 3 位；姓名/邮编：随便填 |
| 需要 3DS 验证 | `4000 0027 6000 3184` | 支付时会弹验证页，点通过即可（用于验证异步回调链路） |
| 支付失败（拒付） | `4000 0000 0000 0002` | 模拟卡被拒，订单不应开通 |

### 1.5 上线（M3 阶段，本期不做）的前置条件（仅告知）

- Stripe **不支持中国大陆主体**直接开户收款；正式收真钱需香港/美国/新加坡等主体的 Stripe 账户，把 `sk_test_`/`pk_test_` 换成 live 密钥、`whsec_` 换成线上端点密钥、价格用 live Price ID。
- 线上需在 Dashboard 配置 Webhook 端点（`https://你的域名/api/billing/stripe/webhook`）并订阅本方案列出的事件。

---

## 2. 账户体系设计

### 2.1 认证方式

- 注册：邮箱（小写归一、去空格）+ 密码（≥8 位）。密码用 **PBKDF2-HMAC-SHA256**（标准库 `hashlib.pbkdf2_hmac`，每用户独立随机 salt，≥200000 次迭代）哈希存储，不存明文。
- 登录成功后服务端生成 **不透明随机会话令牌**（`secrets.token_urlsafe(32)`），令牌仅做 **SHA-256 哈希后入库**，明文只返回一次。
- 前端把令牌存 `localStorage`（键 `vdl_auth_token`），之后所有请求自动带 `Authorization: Bearer <token>`；登出即删令牌行（可吊销）。
- 不采用 JWT：不透明令牌可主动失效、服务端可查会员状态，无 JWT 撤销难题。
- 登录/注册接口独立限流（默认 5 次/分钟/IP，防爆破与批量注册）。

### 2.2 接口（前缀 `/api`）

| 方法 路径 | 鉴权 | 作用 |
|---|---|---|
| POST `/api/auth/register` | 无 | `{email,password}` → 建号并自动登录，返回 `{token, user}` |
| POST `/api/auth/login` | 无 | `{email,password}` → `{token, user}`；失败统一报「邮箱或密码错误」（不区分账号是否存在，防邮箱枚举） |
| POST `/api/auth/logout` | 登录 | 吊销当前令牌 |
| GET `/api/auth/me` | 可选 | **不报错**：未登录返回 `{user:null}`；登录返回用户信息 + 会员状态 + 当日 AI 配额。全站启动与支付回跳后都用它刷新状态 |

`user` 对象：`{email, member_expire_at(秒级时间戳或null), is_pro, ai_used_today, ai_daily_limit}`。

---

## 3. 支付链路与接口（核心）

### 3.1 端到端时序

```
①用户（已登录）点「开通月卡」
  POST /api/billing/checkout {plan:"month"}
  后端：
    a. 校验登录、校验 plan ∈ 服务端套餐表、独立限流
    b. 【先建本地订单 order_id(UUID)，status=pending】——幂等基石
    c. stripe.checkout.Session.create(
         mode="payment",
         line_items=[{price: PRICE_MONTH, quantity:1}],
         client_reference_id=order_id,      # Stripe 回跳/回调里原样带回
         customer_email=user.email,         # 收银台预填邮箱
         success_url=<本站>/?pay=success#pricing,
         cancel_url=<本站>/?pay=cancel#pricing,
       )
    d. 回写订单 stripe_session_id（UNIQUE），返回 {url}
  前端 window.location = url → 跳到 Stripe 托管收银台输测试卡

②支付成功后 Stripe 做两件相互独立的事：
  A) 浏览器跳转 success_url（仅 UX 展示；【绝不在此开通会员】）
  B) Stripe 服务器 POST /api/billing/stripe/webhook（开通会员的【唯一可信依据】）

③Webhook 处理（POST /api/billing/stripe/webhook）
  - 必须取【原始字节体】await request.body()，禁止先 JSON 解析（验签依赖原文）
  - stripe_client.construct_event(raw, sig_header, WEBHOOK_SECRET)
      验签失败 → 400（Stripe 会重试；伪造请求被拒）
  - event_id 去重：INSERT IGNORE webhook_events，已存在 → 直接 200（重复投递短路）
  - 按事件类型分发（见 3.3），全部完成 → 200；非 2xx 时 Stripe 会按策略重试最多约 3 天

④浏览器回到本站：前端识别 ?pay=success，轮询 GET /api/auth/me（最多约 10s）
  看到 is_pro=true → 展示「开通成功，会员有效期至 xxxx-xx-xx」
```

### 3.2 业务接口

| 方法 路径 | 鉴权 | 作用 |
|---|---|---|
| POST `/api/billing/checkout` | 登录 | `{plan:"month"|"year"}` → `{url}`；未配置 Stripe → 503 `billing_disabled`（前端保留「即将上线」兜底） |
| POST `/api/billing/stripe/webhook` | 无（验签） | Stripe 回调，原始体验签 + 幂等履约 |
| GET `/api/billing/orders` | 登录 | 查看本人订单记录（金额/套餐/状态/时间），供「我的会员」卡片展示 |

### 3.3 处理的 Webhook 事件

| 事件 | 处理 |
|---|---|
| `checkout.session.completed` | 取 `client_reference_id`（=order_id）；校验 session 存在、金额与套餐匹配性（币种/金额记录留痕）、`payment_status==paid` → 事务内履约（见 3.4） |
| `checkout.session.async_payment_succeeded` | 同 completed（3DS/异步付款成功也走同一履约函数，天然幂等） |
| `checkout.session.async_payment_failed` | 订单标记 `failed`，不开放任何权益 |
| `checkout.session.expired` | 订单标记 `expired` |
| 其他事件 | 记录后返回 200（不认领导致 Stripe 无意义重试） |

### 3.4 履约（开通会员）——事务 + 状态机保证只开通一次

```sql
BEGIN IMMEDIATE;
  -- 1) 行锁订单，并做状态机条件更新（关键幂等点）
  UPDATE orders SET status='paid', paid_at=?, amount_cents=?, currency=?
    WHERE order_id=? AND status='pending';
  -- 若影响行数=0：订单已处理/已失效 → 提交空事务并短路（绝不重复加时长）
  -- 2) 叠加会员时长（在当前到期时间与"现在"中取较晚者，再加套餐天数）
  UPDATE users
     SET member_expire_at = MAX(COALESCE(member_expire_at,0), ?now) + ?plan_days
   WHERE user_id=(SELECT user_id FROM orders WHERE order_id=?);
COMMIT;
```

- **三层幂等**：
  1. `orders.stripe_session_id` UNIQUE：一个 Stripe 会话对应且仅对应一个订单；
  2. `webhook_events.event_id` PRIMARY KEY：同一事件重放只处理一次（官方明确：Webhook 可能重复且**乱序**，不能按时间戳判断，只能按事件 ID 去重）；
  3. `UPDATE ... WHERE status='pending'` 条件更新：并发/重放下履约代码执行多次，也只有一次真正生效。
- 金额以 Webhook 回执 `session.amount_total/currency` 落单留痕（它来自验签后的 Stripe 数据，不可伪造）。

---

## 4. 数据库表（SQLite，新文件 `app/db.py`；库文件 `data/vdl.db`）

```text
users                账户
  user_id            TEXT PK
  email              TEXT UNIQUE NOT NULL      -- 小写归一后
  password_hash      TEXT NOT NULL             -- pbkdf2$iter$salt_hex$hash_hex
  stripe_customer_id TEXT                      -- 预留，v1 可空
  member_expire_at   INTEGER NOT NULL DEFAULT 0 -- Unix 秒；0=非会员
  created_at         INTEGER

auth_tokens          登录会话（不透明令牌，仅存哈希）
  token_hash         TEXT PK                   -- sha256(token)
  user_id            TEXT NOT NULL
  created_at         INTEGER
  last_seen_at       INTEGER

orders               支付订单（对账与状态机）
  order_id           TEXT PK                   -- 下单前生成，=Stripe client_reference_id
  user_id            TEXT NOT NULL
  plan_key           TEXT NOT NULL             -- month | year（服务端套餐表决定天数）
  amount_cents       INTEGER NOT NULL DEFAULT 0
  currency           TEXT NOT NULL DEFAULT ''
  status             TEXT NOT NULL             -- pending | paid | failed | expired
  stripe_session_id  TEXT UNIQUE
  created_at         INTEGER
  paid_at            INTEGER

webhook_events       Stripe 回调幂等去重
  event_id           TEXT PK                   -- evt_xxx
  event_type         TEXT
  received_at        INTEGER

ai_usage             免费用户 AI 每日配额
  subject            TEXT NOT NULL             -- u:<user_id> 或 ip:<ip>
  ymd                TEXT NOT NULL             -- YYYY-MM-DD（服务器时区）
  calls              INTEGER NOT NULL DEFAULT 0
  PRIMARY KEY (subject, ymd)
```

- 连接：模块级单连接 `sqlite3.connect(check_same_thread=False)` + `threading.Lock` 串行化写事务（FastAPI 端点协程 + 线程池混用，单 worker 下足够；开启 WAL：`PRAGMA journal_mode=WAL`）。
- `data/` 目录启动时自动创建，`data/*.db*` 加入 `.gitignore`。
- 建表用 `CREATE TABLE IF NOT EXISTS`（v1 不引迁移工具，零额外依赖）。

---

## 5. 权益矩阵与后端拦截（前端仅引导，以后端判定为准）

| 能力 | 游客 / 免费账户 | PRO（member_expire_at > now） |
|---|---|---|
| 单链接解析、≤720p 下载 | ✅ | ✅ |
| **批量下载** | ✅ **维持免费** | ✅ |
| 1080p / 2K / 4K / 高码率 | ❌ 服务端拒绝（见下） | ✅ |
| 字幕「仅提取」 | ✅ | ✅ |
| 字幕**翻译**（`target_lang` 非空） | ❌ 403 `pro_required` | ✅ |
| AI 摘要 / 章节 / 思维导图 / 问答 | ✅ **每日 3 次**（游客按 IP、免费账户按 user_id） | ✅ 不限次 |
| AI 限流桶（每分钟） | 沿用 `RATE_AI_PER_MIN`（默认 30） | `RATE_AI_PRO_PER_MIN`（默认 60，可配） |

后端落点：

1. **下载清晰度**：`POST /api/download` 与批量任务实际启动下载处：
   - 非 PRO 且所选格式 `height > 720` → 403 `pro_required`（格式列表仍照常返回，由前端给 1080p+ 打锁标）。
   - 非 PRO 且未指定格式（走"默认最佳"）→ 在 yt-dlp format 串中强制压到 `[height<=720]`（防止自动选出 4K 绕过）。
2. **字幕翻译**：`POST /api/subtitles` 当 `target_lang` 非空且非 PRO → 403 `pro_required`。
3. **AI 每日配额**：summary / chapters / mindmap / ask 四处，成功调用后对 `ai_usage` UPSERT 计数；非 PRO 当日第 4 次起 → 403 `pro_required`（响应带 `ai_used_today/ai_daily_limit`）。PRO 不计数、不限日次数（分钟级限流仍生效）。
4. 所有 `pro_required` 响应体：`{"ok":false,"error":"该功能为 PRO 会员专属…","code":"pro_required"}`，前端收到统一弹升级弹窗。

---

## 6. 前端改造（零构建单页，沿用现有风格）

1. **导航栏**（[static/index.html](../static/index.html) 约 L112-129）：
   - 未登录：右侧「登录 / 注册」文字按钮 + 原「升级 PRO」按钮保留。
   - 已登录免费：显示邮箱缩写 + 「升级 PRO 👑」。
   - 已登录 PRO：显示「👑 PRO 至 MM-DD」（绿色胶囊），点击进「我的会员」卡片。
2. **登录/注册模态框**：Tab 切换两个表单，复用现有 modal 样式；错误信息走中文文案；成功后存 token、刷新全站状态；若从「开通」流程被拦进来，登录成功后**自动续走下单跳转**。
3. **支付弹窗（替换现有 #proModal 的"即将上线"）**：两张套餐卡（月/年），点击 → 已登录直接请求 checkout 并跳转；未登录先弹登录框。
4. **支付结果**：`?pay=success` 回跳后轮询 `/api/auth/me`，成功 toast/横幅「开通成功，有效期至 …」；`?pay=cancel` 提示「支付已取消」。
5. **我的会员**：弹窗或区块，展示会员状态/到期时间、订单记录（调 `/api/billing/orders`）、退出登录。
6. **功能加锁 UI**：
   - 格式列表 1080p+ 行加 🔒 与置灰，点击拦截弹升级；
   - 字幕「翻译」按钮非 PRO 加锁标，点击弹升级；
   - AI 配额将尽/用尽时按 `pro_required` 弹升级。
7. `api()` 统一封装：自动附带 `Authorization` 头；401 → 清 token 弹登录；`pro_required` → 开升级弹窗（顺带修复 SECURITY.md 已记录的 `detail` 包裹错误文案丢失缺口）。

---

## 7. 安全清单（支付专项）

- [x] Webhook 强制**签名验证**（`construct_event` 原始体 + `whsec_` 密钥），失败 400。
- [x] 开通会员只由验签后的 Webhook 驱动；success_url 仅 UX（防关页面/伪造跳转）。
- [x] 三层幂等（session 唯一 / event 去重 / 订单状态机条件更新），杜绝重复开通、重复加时长。
- [x] 价格/时长只取自服务端套餐表与 Stripe Price ID，前端只传 `plan` 键。
- [x] 密钥只存 `.env`（已 gitignore）；`/api/auth/me` 等任何响应绝不回传 sk/password_hash/token。
- [x] 密码 PBKDF2 加盐 200k 迭代；登录失败不区分「账号不存在/密码错」防邮箱枚举。
- [x] 登录/注册/下单接口独立限流；AI 配额防匿名滥用；令牌仅哈希存储。
- [x] 订单查询强制 `WHERE user_id=当前用户`（越权查不到他人订单）。
- [x] `BILLING_ENABLED`（由密钥是否配置推导）：未配置时支付端点 503 且不注册对 Stripe 的任何外呼，保证没配密钥的部署零影响。
- [x] SQLite 参数化查询（全量 `?` 占位，杜绝 SQL 注入）；写操作走统一锁。

---

## 8. 文件改动清单

| 文件 | 动作 |
|---|---|
| `requirements.txt` | 新增 `stripe>=11.0` |
| `.env.example` | 新增 Stripe / 账户 / 配额配置段（见 §10） |
| `.gitignore` | 新增 `data/*.db*` |
| `app/config.py` | 新增 Stripe 配置、套餐表（plan→price env / 天数 / 展示价）、`AI_FREE_DAILY_LIMIT`、`RATE_AUTH_PER_MIN`、`RATE_BILLING_PER_MIN`、`RATE_AI_PRO_PER_MIN` |
| `app/db.py` | **新建**：SQLite 连接/建表/通用查询助手/事务锁 |
| `app/auth.py` | **新建**：密码哈希、令牌、`current_user` 依赖、配额读写 |
| `app/billing.py` | **新建**：Stripe 客户端、创建 Checkout、Webhook 构造与履约（纯函数化，便于单测） |
| `app/routes.py` | 新增 `/api/auth/*`、`/api/billing/*`；下载/字幕/AI 端点接入权益拦截 |
| `app/downloader.py` | format 串支持免费用户 `height<=720` 封顶（入参化，默认不封顶） |
| `static/index.html` / `static/app.js` / `static/styles.css` | 账户模态、会员导航、支付/会员中心、加锁 UI、支付回跳处理 |
| `docs/DESIGN.md` `API.md` `SECURITY.md` `CHANGELOG.md` `PLAN.md` | 落地后同步 |
| `e2e_billing.py`（仓库根，按惯例不入仓） | 履约幂等/验签/越权/配额的自动化脚本（直调函数 + TestClient） |

## 9. 开发与验收步骤（确认后按序执行，每步可独立验证）

1. **数据层**：`db.py` + 建表；脚本验证建库/并发写。
2. **账户**：注册/登录/退出/me；限流；TestClient 单测（错误密码、枚举、令牌哈希、越权）。
3. **Stripe 接入**：套餐表 + checkout + webhook 验签；**幂等履约单测**（同一 event 重放 N 次只加一次时长；乱序/重复 session）。
4. **权益拦截**：清晰度封顶与 403、翻译 403、AI 日配额（IP 与用户双口径）。
5. **前端**：导航/账户模态/支付弹窗/会员中心/加锁标/回跳轮询。
6. **真实联调（Stripe 测试模式 + CLI，需你在场提供操作）**：
   - 你在 Stripe 后台建好 2 个 Price，把 `sk_test_`、两个 `price_`、`whsec_` 填进本地 `.env`；
   - 起服务 + `stripe listen`；浏览器注册→买月卡（4242 卡）→回跳看到期时间=+30 天；
   - 再买年卡验证**叠加**；用 0002 失败卡验证不开通；CLI `stripe trigger checkout.session.completed` 验证重放幂等；
   - 验证三个 PRO 锁（1080p 下载/翻译/AI 第 4 次）与 PRO 账户全部放行。
7. 文档同步、CHANGELOG、找你整体验收。

## 10. 新增 .env 配置（示例，落地时写入 .env.example）

```text
# ---- Stripe 会员支付 ----
STRIPE_SECRET_KEY=sk_test_xxx          # 留空=支付功能关闭（端点 503，不影响其他功能）
STRIPE_WEBHOOK_SECRET=whsec_xxx        # stripe listen 输出
STRIPE_PRICE_MONTH=price_xxx_month     # 一次性价格（30 天）
STRIPE_PRICE_YEAR=price_xxx_year       # 一次性价格（365 天）
# ---- 账户 / 配额 ----
RATE_AUTH_PER_MIN=5                    # 注册/登录 每IP每分钟
RATE_BILLING_PER_MIN=5                 # 创建支付会话 每IP每分钟
AI_FREE_DAILY_LIMIT=3                  # 非 PRO 每日 AI 次数（游客按IP、账户按ID）
RATE_AI_PRO_PER_MIN=60                 # PRO 每分钟 AI 限流
```
