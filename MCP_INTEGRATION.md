# MCP 集成说明

本文说明麦团餐官实际调用了哪些麦当劳 MCP Server 与 Tool、调用顺序为什么这么排、以及每一步对应的业务价值。

---

## 一、接入方式

| 项目 | 值 |
|---|---|
| Server | 麦当劳中国 MCP Server（`mcd-mcp`），远程托管 |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0） |
| 鉴权 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 客户端实现 | [`scripts/mcd_client.py`](scripts/mcd_client.py)，纯 Python 标准库，无第三方依赖 |
| 限流 | 每 Token 每分钟 600 次请求 |

Token 来源：手机号登录 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) → 「控制台」→「激活」→ 同意服务协议 → 复制。

**本项目仓库内不含任何真实 Token**。配置示例见 [`mcp-config.example.json`](mcp-config.example.json)，只使用环境变量占位符 `${MCD_MCP_TOKEN}`。

客户端处理了两种返回体：`application/json` 与 `text/event-stream`（SSE），并从 SSE 流里挑出 `id` 匹配的那条 JSON-RPC 响应；会话 ID 从 `Mcp-Session-Id` 响应头取回并在后续请求中回带。

---

## 二、用到的 Tool

| Tool | 调用场景 | 关键参数 | 返回里我们真正用的字段 |
|---|---|---|---|
| `delivery-query-addresses` | 拿到用户已有配送地址 | — | `data.addresses[].addressId` |
| `delivery-query-stores` | 查能做企业团餐的门店 | `addressId`, `beType=6` | `storeCode`、`beCode`、`reservationTimeOptions`、`businessStartTime/EndTime` |
| `query-meal-assistance` | 查三档助餐服务与可用性 | `storeCode`, `beType=6`, `orderType=2`, `beCode`, `reservationDate` | `mealAssistanceItems[]` 的 `gmServiceCode`、`gmServiceName`、`serviceItems`、`enable`、`unusableReason` |
| `query-meals` | 拉团餐菜单 | `storeCode`, `beType=6`, `orderType=2`, `beCode`, `reservationDate` | `categories[].meals[].code` |
| `calculate-price` | 批量钓价 + 三档算价 | `storeCode`, `beType=6`, `orderType=2`, `beCode`, `gmServiceCode`, `items[]`, `reservationDate` | `productList[]`、`price`、`originalPrice`、`packingPrice`、`deliveryPrice`、`enjoyed`、`enjoyable` |
| `query-meal-detail` | 单品详情、可特制选项 | `storeCode`, `code`, `beType=6`, `orderType=2`, `reservationDate` | `name`、`modification`（可选路径，主流程不依赖） |

**没有调用 `create-order`。** 见第六节。

---

## 三、调用流程

```
① delivery-query-addresses
        ↓ addressId
② delivery-query-stores(beType=6)
        ↓ storeCode + beCode + reservationTimeOptions
③ reservation_check(when)            ← 本地校验，不额外发请求
        ↓ 通过 / 不通过（不通过直接给出最近可约时段）
④ query-meal-assistance(beType=6)
        ↓ 三档 gmServiceCode + enable + unusableReason
⑤ query-meals(beType=6, orderType=2)
        ↓ categories[].meals[].code（无名字、无价格）
⑥ calculate-price × N                ← 批量钓价，12 个商品一批
        ↓ {code: {name, price}}
⑦ choose_structure()                 ← 本地：场景 + 预算 → 结构
⑧ build_combo()                      ← 本地：穷举求最优组合
⑨ calculate-price × 3                ← 三档并行算价，出对比表
        ↓
      渲染成可转发的方案
```

### 为什么助餐服务要在算价之前

`calculate-price` 需要 `gmServiceCode`。不同档位的折扣互不相同，所以必须先拿到可用档位清单，再按档位分别算价——一次算价只能对应一个档位。

### 为什么菜单和价格要分两步

`query-meals` 只返回餐品编码和标签，**不返回餐品名称，也不返回价格**。想得到名字和价格有两条路：

- 逐个调 `query-meal-detail`：一次一个商品，二三十个商品就是二三十次请求
- 一次塞进 `calculate-price`：每个商品 `quantity=1`，返回的 `productList[]` 里每个商品都带 `originalSubtotal`，即单价

本项目走第二条。**一批 12 个商品一次调用**，二三十个商品 2–3 次请求就能拿全价格，比逐个查详情省一个数量级。这也解释了为什么 `query-meal-detail` 在依赖表里是可选的。

### 为什么算价要跑两轮

第一轮是「钓价」，用 `quantity=1` 拿单价，不做决策；第二轮才是真实报价，用 `quantity=人数` 乘上选中组合，并且对三个档位各跑一次。两轮的参数结构相同，只是用途不同。

---

## 四、三个必须按团餐口径处理的地方

### 1. `beType` 必须是 6

麦当劳 MCP 的 `beType` 区分了四种业务：

| beType | 业务 |
|---|---|
| 1 | 到店自取 |
| 2 | 麦乐送 |
| 5 | 得来速（Drive Through） |
| **6** | **企业团餐** |

用 `beType=2` 查出来的门店、`beCode`、可约时段、价格全部不是团餐口径。同一个门店（如徐汇万科三期），`beType=6` 的 `beCode` 是 `145057681`，`beType=2` 是 `145057602`，两者不可混用。

### 2. 团餐的预约时段和麦乐送不是一套

同一家门店同一天：

| 场景 | 午餐时段 | 下午茶时段 |
|---|---|---|
| 企业团餐（beType=6） | 12:00—15:59 | 16:00—18:29 |
| 麦乐送（beType=2） | 11:00—14:59 | 15:00—17:29 |

团餐时段整体后移，且团餐可预约的天数更长。按麦乐送的时段给客户承诺"11 点送到"，是接不了的。

### 3. 助餐服务档位直接改变折扣

同一份餐品、同一个门店，只换 `gmServiceCode`，应付金额不同——因为档位对应不同的助餐折扣：

| gmServiceCode | 服务 | 20 人同菜品应付 | 折扣 |
|---|---|---|---|
| `GMS001` 保鲜速达 | 准时送达 + 保温 | ¥601.00 | 7.4 折 |
| `GMS002` 专人分餐 | + 送餐到人/到桌 | ¥681.00 | 8.4 折 |
| `GMS003` 专人摆台 | + 餐品摆台 + 专人服务 | ¥721.00 | 约 9.0 折 |

这也意味着「升级服务」是一个可以用人均价差讲清楚的决策，而不是一句"会更贵一点"。

**另有一条硬约束**：`GMS002` / `GMS003` 需提前 4 小时预约，不足 4 小时时接口返回 `enable: false` 与 `unusableReason`。本项目在生成方案阶段就把这条前置，而不是等到下单才暴露。

---

## 五、业务价值

| 谁用 | 原来怎么做 | 用这个之后 |
|---|---|---|
| 企业行政 | 打电话问门店、手算人均、微信来回确认档位 | 一次输入，直接拿到带价格的方案 |
| 企业 HR / 销售 | 不知道门店有没有专人服务、来不来得及 | 方案里直接标出可约/不可约和原因 |
| 门店侧 | 反复被问"能不能送到座位""多少钱一个人" | 需求进来时已经算好，减少往复 |
| 活动/团建组织者 | 预算和餐品对不上，超了才发现 | 组合按预算穷举，跑之前就知道人均占用 |

核心是把团餐里**隐性的规则**（业务类型、时段、档位折扣、4 小时提前量）变成**显性的输出**，让不懂团餐的人也能一次问对。

---

## 六、刻意不做的事

| 不做 | 原因 |
|---|---|
| 不调 `create-order` | 它会真实扣款下单。工具做到"出方案 + 报价"为止，最后一下由人点 |
| 不缓存价格到本地 | 价格、库存、活动随时变；每次跑都实时取 |
| 不写入 Token / 地址 / 手机号 | 配置只用环境变量占位符；方案输出只含门店、餐品、价格、时间 |
| 不推测发票与变更规则 | 接口不返回开票信息，不同门店规则不同，不臆造 |
