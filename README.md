# 私聊文本 AI 客服工作台 V1

这是一个不包含任何真实聊天记录的独立解析与人工起草项目。数据通过 `--input` 从外部目录传入；运行产生的数据库默认保存在项目自己的 `.wechat-cs/` 目录。V1 仅处理一对一普通文字，不会读取群聊、控制微信或自动发送消息。

输入文件格式见 [DATA_FORMAT.md](DATA_FORMAT.md)。`tests/fixtures/` 只包含虚构测试数据，不包含历史客户信息。

## 当前能力

- 客户机会与未闭环售后队列，所有结论都带规则原因和脱敏证据。
- 客户详情、最近私聊和脱敏身份候选人工复核。
- 200 条角色校准与 500 条风格样本人工审核。
- 手动粘贴最新消息，调用 Kimi 开放平台生成待审核回复。
- 草稿复制、直接采用、修改后采用和放弃反馈。
- 可选的飞书知识缓存注册；动态事实缺少依据时强制转人工。
- 远端 Dashboard 代理，浏览器不会拿到本地服务或 Kimi 密钥。
- M0 只读真值链：四账号会话资格、确定性飞书身份桥，以及顾客付款/退款/换货/补偿规范事实。
- 点时客户画像、盲决策卡、1/3/7/30 天三态结果，以及规则驱动的三泳道“今日行动队列”。
- Plan 7 的 20 卡协议校准、100 卡人工验收和 500 卡金标批次；成交结果不会进入复核上下文。

## 今日行动队列的安全状态

当前实现是 `shadow/review-only`：不会发送微信，也没有发送接口或发送按钮。规则负责联系动作、事实槽位和禁止承诺；`kimi-k2.6` 只有在人工填写完整事实白名单时才允许润色，空白名单、模型失败或输出越界都会回退到不可变规则骨架。

消息采集不是 `running` 或消息快照超过 15 分钟时，实时 `reply_now` 会关闭；订单快照仍在 24 小时内时，`proactive_today` 可以继续生成明确标注截止时间的历史快照候选，但每一项都要求联系前人工核对新消息、刚下单、售后和拒绝联系。队列元数据异常仍会全局 fail-closed。订单快照超过 24 小时时，客户价值、复购和商品信号会被隐藏，依赖这些信号的主动联系也会暂停。当前 `M0-C=false` 不会因构建、复核或安装检查而改变。

## 1. 构建派生数据库

可选：先安装本项目。项目同时兼容现代 `pyproject.toml` 与旧版 `pip/setuptools`：

```bash
python3 -m pip install --no-deps .
```

先准备两个不同的长随机值。不要把它们写进仓库：

```bash
export WECHAT_CS_HMAC_SECRET='至少 32 字符的随机 HMAC 密钥'
export WECHAT_CS_TOKEN='至少 32 字符的随机服务令牌'
python3 -m wechat_cs build \
  --input /absolute/path/to/your-export \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --account-id your-account-namespace
python3 -m wechat_cs status --db .wechat-cs/data/wechat_cs.sqlite3
```

构建只读取原始导出，并将脱敏派生数据写入 `.wechat-cs/`。重复构建会保留已有的角色校准、样本审核和草稿反馈。

## 2. 启动本地工作台

```bash
export WECHAT_CS_TOKEN='与上面相同的服务令牌'
export KIMI_API_KEY='Kimi 开放平台 Key'
export KIMI_BASE_URL='https://api.moonshot.cn/v1'
export KIMI_MODEL='kimi-k2.6'
python3 -m wechat_cs.api --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/`。服务令牌为必填项；在“系统状态”页面输入后只保存在当前浏览器标签页的 `sessionStorage`。本地与远程访问都不会启用无令牌模式。

未配置 Kimi Key 时，分析和审核功能仍可用，但起草接口会明确返回不可用，不生成模拟回复。

## 3. 构建今日行动队列

行动产物只从已经规范化的独立 run DB 读取，不读取 Dashboard 快照或原始 inbox：

```bash
python3 -m wechat_cs build-action-queue \
  --db .wechat-cs/runs/<run-id>/wechat_cs.sqlite3 \
  --as-of 2026-07-13T12:00:00+08:00 \
  --collector-status running
```

不传 `--profile` 时会为 run DB 中全部账号构建；首轮 Dashboard 试用仍只展示 `aolai1`。接口为：

- `GET /v1/action-queue?profile=aolai1&date=YYYY-MM-DD&limit=20`
- `GET /v1/action-queue/{action_id}`
- `POST /v1/action-queue/{action_id}/draft`
- `POST /v1/action-queue/{action_id}/feedback`

所有接口只返回匿名 `customer_key`，并固定返回 `send_allowed=false`。

## 4. 成交归因样本审计

在训练客户优先级或话术权重之前，先运行只读归因审计。它把同一手机号同一天的付款记录合并为一个购买事件，并将结果分为唯一跨日接触、同日相关、多次接触竞争、无匹配接触、身份未核验和订单质量不足。输出同时包含成交正样本、可比未成交样本和观察期未满样本；不会训练权重，也不会产生可发送消息。

```bash
python3 -m wechat_cs build-conversion-audit \
  --db .wechat-cs/runs/<run-id>/wechat_cs_m0.sqlite3 \
  --facts-db .wechat-cs/runs/<facts-run-id>/wechat_cs_m0.sqlite3 \
  --history-facts-db .wechat-cs/runs/<history-run-id>/wechat_cs_m0.sqlite3 \
  --as-of 2026-07-15T11:50:00+08:00 \
  --output-dir .wechat-cs/analysis/conversion-attribution-v1
```

`--facts-db` 只在聊天快照比订单事实快照更新时使用；不传时两类数据读取同一个库。当前 Base 只覆盖部分历史时，可传 `--history-facts-db`，审计器会按匿名客户和付款日合并购买事件，避免按不一致的记录 ID 重复计数。所有输出都限制在项目的 `.wechat-cs/` 派生目录内，源数据库以只读方式打开。订单只有日期、没有可靠付款时分秒时，同日成交固定保留为相关性歧义，不进入方法学习。

## 5. Plan 7 人工复核

三个批次必须依次完成，后续阶段不会越过前一阶段：

```bash
python3 -m wechat_cs review-status --db /absolute/path/to/run.sqlite3
python3 -m wechat_cs review-batch --db /absolute/path/to/run.sqlite3 --stage protocol_20
python3 -m wechat_cs review-annotate \
  --db /absolute/path/to/run.sqlite3 \
  --stage protocol_20 \
  --reviewer reviewer_01 \
  --input /absolute/path/to/redacted-annotations.json
```

阶段名依次为 `protocol_20`、`acceptance_100`、`gold_500`。人工复核可以看到独立标记的实际回复，模型载荷会剔除它；付款、订单和结果字段不会被复核查询读取。

## 6. 角色校准和样本审核

角色校准必须完成全部 200 条且准确率达到 99%，之后才能安全导出训练集。审核通过的低风险风格样本才会进入起草检索。

```bash
python3 -m wechat_cs export-chatml \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --output .wechat-cs/exports/wechat_style_pair.v1.jsonl \
  --split all
```

不要使用 `--include-pending`、`--allow-unverified-roles` 或 `--include-risky` 生成生产训练集；这些参数只用于隔离研究。

## 7. 飞书知识缓存

复制并编辑 `config/knowledge_sources.example.json`。当前服务读取本地缓存文件，不会在请求时直接把整份飞书文档拉入模型。每个缓存需要包含采集时间和可检索条目；缺失或过期会显示在系统状态中。

动态价格、库存、物流、退款或补发问题没有有效依据时，起草结果会带 `grounding_missing`、`needs_clarification` 和 `needs_human`。

## 8. 安装到现有远端 Dashboard

先在 Dashboard 主机上只做兼容性检查：

```bash
python3 dashboard_integration/install.py \
  --dashboard-dir /absolute/path/to/dashboard \
  --project-root /absolute/path/to/wechat-cs-parser \
  --check
```

检查通过后再去掉 `--check` 执行安装。安装器会：

- 复制 `/wechat-cs/` 静态工作台和 `wechat_cs_proxy.js`；
- 给 `server.js` 与 Dashboard 导航做幂等补丁；
- 首次修改前生成 `server.js.before-wechat-cs` 和 `index.html.before-wechat-cs` 备份；
- 绝不复制原始聊天或本地 SQLite 数据库。

Dashboard 服务进程需要：

```bash
export WECHAT_CS_BASE_URL='http://本机的-Tailscale-IP:8765'
export WECHAT_CS_TOKEN='解析服务令牌'
export WECHAT_CS_DASHBOARD_TOKEN='至少 32 字符的独立操作员令牌'
export WECHAT_CS_PRIVATE_MAP_PATH='/仅本机可读/customer-map.json'
```

浏览器只访问 `/api/wechat-cs/*`，并在系统页输入独立操作员令牌。解析服务令牌由 Dashboard 代理注入，不进入页面脚本。代理只允许上述四个行动接口和 `/health`，不会转发客户聊天、旧草稿或审核接口。

私有映射只接受 `display_name`、`owner`、`account_label` 和不含联系方式的通用 `contact_hint`；不要写入手机号、微信号、raw ID 或 HMAC。若 API 绑定通配地址，还必须设置 `WECHAT_CS_ALLOWED_HOSTS`；优先直接绑定本机或 Tailscale IP。

安装前必须先保存 Dashboard 当前未提交改动的检查点，并取得人工授权。兼容性检查本身不会写入 Dashboard，也不代表 `M0-C` 或试用放行。

回滚时停止 Dashboard，恢复两个 `.before-wechat-cs` 备份，并移除 `wechat_cs_proxy.js` 与 `wechat-cs/` 目录。

## 9. 验证

```bash
python3 -m py_compile wechat_cs/*.py
python3 -m unittest discover -s tests -v
node --check wechat_cs/static/app.js
node --check dashboard_integration/wechat_cs_proxy.js
```

真实 Kimi、飞书和远端 Dashboard 验证需要对应的服务端凭据或链接；测试不会读取或打印这些密钥。
