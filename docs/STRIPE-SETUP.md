# STRIPE-SETUP — Stripe 会员支付操作指南（运维者向）

> 配套 [MEMBERSHIP.md](MEMBERSHIP.md)（设计）与 [CHANGELOG.md](CHANGELOG.md) 0.7.0（实现记录）。本文回答三件事：**① 现在什么都不配，网站会怎样？② 没有外网怎么测支付？③ 怎么用 Stripe 测试模式真实走单？**
> 你（运营者）只需要按 §4 / §5 操作；代码侧无需任何改动。

---

## 0. 先安心：不配置 Stripe 时网站完全正常

`billing_enabled` 由四个配置是否**齐全**推导（[app/config.py](../app/config.py)：`STRIPE_SECRET_KEY` + `STRIPE_PRICE_MONTH` + `STRIPE_PRICE_YEAR`）。缺任何一个：

- 注册 / 登录 / 会员中心 / 权益拦截**照常工作**（它们不依赖 Stripe）；
- 点「开通会员」→ 后端返回 503 `billing_disabled`，前端弹出「支付功能即将上线，敬请期待」；
- 服务端**不会**向 stripe.com 发起任何外呼（连 SDK 客户端都不会创建）。

---

## 1. 五分钟理解支付链路（你在其中做什么）

```
用户点「开通月卡」→ 本站建 pending 订单 → 跳转 Stripe 托管收银台输卡
→ Stripe 收到钱 → 服务器对服务器回调本站 Webhook（验签）→ 本站开通会员
```

你要做的只有三件事：**① 在 Stripe 后台拿到 3 个 ID/密钥；② 让本机能收到 Stripe 回调（Stripe CLI 转发）；③ 用测试卡走一遍单**。全程在**测试模式**（Test mode），不扣真钱、不需要营业执照、不需要公网域名/HTTPS。

---

## 2. 没有外网怎么测？（离线全链路验证）

真实调 Stripe API（创建收银台、回调转发）**必须**能访问 `api.stripe.com`，这无法离线。但支付系统最核心、最容易出错的部分——**验签、事件去重、幂等履约、权益开通、订单状态机**——已由仓库根 `test_membership.py` **离线**覆盖（不触网）：

```powershell
d:\LCP_agent\video_download\.venv\Scripts\python.exe test_membership.py
```

原理：用 Stripe SDK 的本地签名函数（`stripe.WebhookSignature.generate_signature_header`）模拟 Stripe 服务器构造**真实格式**的签名事件，向 TestClient 发起完整 HTTP 回调。覆盖 29 项：

| 类别 | 覆盖点 |
|---|---|
| 账户 | 注册 / 重复注册 409 / 非法邮箱 / 弱密码 / 错误密码 401 / 登录 / me 三态 / 令牌吊销 |
| 鉴权与降级 | 未登录访问支付 401 / Stripe 未配置 503 `billing_disabled` |
| Webhook 安全 | 缺签名头 400 / 篡改载荷 400（伪造请求被拒） |
| 履约与幂等 | completed 开通 +30 天 / **同一事件重放只生效一次** / 不同事件同订单不重复履约 / 续费叠加 / unpaid 不履约 / 失败与过期事件正确标记 |
| 权益 | AI 免费配额计满后拦截 |

> ✅ 当前基线：**PASS=29 FAIL=0**。改动 `auth.py`/`billing.py`/`db.py`/`routes.py` 后**必须重跑**。
> 唯一离线测不到的是「与 Stripe 真实服务器的一次完整往返」（下节补测）。

---

## 3. 测试模式真实联调（需要能访问 stripe.com）

### 3.1 注册并拿密钥

1. 打开 <https://dashboard.stripe.com> 注册（测试模式资料随便填）。
2. 确认右上角处于**测试模式**（橙色 Test mode 开关）。
3. 开发者 → API 密钥（Developers → API keys）：复制 **密钥（Secret key）** `sk_test_...`（点 Reveal test key）。⚠️ 只放服务端 `.env`，绝不进前端/不入 git。
   > 本项目用 Checkout 托管收银台（整页跳转到 checkout.stripe.com），**不需要** Publishable key（`pk_test_...`）。Dashboard 页面同屏会显示「可发布密钥」`pk_test_...`，无需复制、不填进 `.env`。

### 3.2 创建 1 个商品 + 2 个一次性价格

产品目录（Products）→ +添加商品「PRO 会员」→ 添加两个**一次性（One-off）**价格（不是 Recurring）：

| 价格 | 建议金额 | 说明 |
|---|---|---|
| 月卡 | `$2.99 USD`（对应页面展示 ¥19/月） | 开通 30 天 |
| 年卡 | `$29.99 USD`（对应页面展示 ¥199/年） | 开通 365 天 |

创建后各得一个 `price_...` ID。⚠️ Stripe 不支持人民币收单，用 USD；页面展示价与真实扣款币种在 v1 解耦，**扣款金额以后台 Price 为唯一事实源**（后端只传 price ID）。

### 3.3 安装 Stripe CLI 并转发回调（本机收回调的关键）

```powershell
winget install Stripe.StripeCLI        # 或 scoop install stripe
stripe login                           # 打开浏览器授权测试账户，回车确认
```

**本站服务启动后**，另开一个终端常驻运行：

```powershell
stripe listen --forward-to localhost:8000/api/billing/stripe/webhook
# > Ready! Your webhook signing secret is whsec_xxxxx  ← 复制它（Ctrl+C 退出后密钥仍有效）
```

CLI 只要求**本机能访问 api.stripe.com**；不需要公网 IP、域名、HTTPS。

### 3.4 填 `.env` 并重启

```ini
STRIPE_SECRET_KEY=sk_test_你的密钥
STRIPE_PRICE_MONTH=price_月卡ID
STRIPE_PRICE_YEAR=price_年卡ID
STRIPE_WEBHOOK_SECRET=whsec_3.3输出的签名密钥
```

重启后启动日志应出现 `billing_enabled=True`（若为 False，按 §0 核对四个值是否齐全）。

### 3.5 用测试卡走单（固定卡号，仅测试模式有效）

| 场景 | 卡号 | 要点 |
|---|---|---|
| ✅ 支付成功 | `4242 4242 4242 4242` | 有效期任意未来（如 12/30）、CVC 任意 3 位、姓名邮编随便 |
| ✅ 3DS 异步验证 | `4000 0027 6000 3184` | 弹验证页点通过 → 验证 `async_payment_succeeded` 也履约 |
| ❌ 拒付 | `4000 0000 0000 0002` | 订单应保持 pending，会员不开通 |

**走单清单**（对应验收步骤）：

1. 浏览器注册/登录 → 点「开通月卡」→ 跳 Stripe 收银台 → 4242 卡支付 → 回跳本站看到「开通成功，有效期至 +30 天」。
2. 会员中心确认状态与订单记录（2 笔：month paid）。
3. 再买年卡 → 到期时间应 **+365 天（叠加）** 而非重置。
4. 用 0002 卡再下一单 → 提示失败，会员时长不变，订单状态 `failed`（CLI 窗口可见 webhook 返回 200）。
5. **重放幂等**：`stripe trigger checkout.session.completed`（或对 CLI 窗口中已成功的事件 `stripe listen` 重发）→ 本站日志出现 `duplicate stripe event ignored`，会员时长**不**叠加。

### 3.6 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动日志 `billing_enabled=False` | `.env` 四个 Stripe 值缺一；或没重启进程 |
| 收银台 404 / `No such price` | Price 属于测试模式但用了 live 密钥（或反之）；两处模式必须一致 |
| 支付成功但没开通 | ① `stripe listen` 窗口没在跑（回调没人接收）；② `STRIPE_WEBHOOK_SECRET` 不是当前 listen 输出的那个（验签 400）；看 CLI 窗口与本站日志即可定位 |
| 日志 `signature verification failed` | `whsec_` 不匹配；重新复制 `stripe listen` 启动行输出的密钥 |
| 回跳后会员状态没刷新 | 前端已自动轮询 ~10s；仍没有则看 3.6 第一条（回调没到） |

---

## 4. 上线（切 live）检查单 — M3 部署时

- [ ] Stripe 账户需**境外主体**（大陆主体无法直接开户收款）；激活账户。
- [ ] Dashboard 切换 **live 模式**：新拿 `sk_live_...`；产品价格在 live 模式**重新创建**（live/test Price ID 不通用）。
- [ ] Dashboard 开发者 → Webhooks → **添加端点** `https://你的域名/api/billing/stripe/webhook`，订阅 4 个事件（`checkout.session.completed` / `checkout.session.async_payment_succeeded` / `checkout.session.async_payment_failed` / `checkout.session.expired`），拿**线上** `whsec_`。
- [ ] `.env` 全部替换为 live 值并重启；确认无残留 `test` 值。
- [ ] 支付域名必须 HTTPS（Stripe 强制）。
- [ ] 线上先以最低价 Price 真实走一单验证端到端，再对外开放。
- [ ] 财务对账：Stripe Dashboard 交易 vs 本站 `/api/billing/orders`（`amount_cents/currency` 已留痕）。

## 5. 后续维护

- Webhook 密钥轮换 / 端点重建后，同步更新 `.env` 的 `STRIPE_WEBHOOK_SECRET` 并重启。
- 退款：v1 在 Stripe Dashboard 人工操作；退款**不会**自动回收会员时长（记 v2，见 PLAN 演进池）。
- 每次改动支付相关代码后重跑 `test_membership.py`（29 项，离线）。
