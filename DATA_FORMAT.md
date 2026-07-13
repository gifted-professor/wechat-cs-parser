# 输入数据契约

解析器通过 `--input` 接收旧版导出目录或 live-inbox 的 `events.jsonl`。项目本身不保存原始聊天。

输入目录至少包含：

```text
export/
├── conversation_index.json
└── messages.jsonl
```

## conversation_index.json

顶层必须是数组。每个私聊会话至少需要：

```json
{
  "conversation_id": "stable-conversation-id",
  "conversation_type": "friend",
  "display_name": "可选显示名",
  "message_count": 12,
  "source_file": "可选原始来源引用"
}
```

`conversation_id` 必须在同一账号内稳定。不同账号必须使用不同的 `--account-id`，禁止混合客户空间。

## messages.jsonl

每行一个 JSON 对象：

```json
{
  "conversation_id": "stable-conversation-id",
  "conversation_type": "friend",
  "message_type": "text",
  "timestamp": "2026-07-11T12:00:00+08:00",
  "text": "消息正文",
  "raw_payload": {
    "status": 3
  }
}
```

当前批处理适配器的账号专属角色映射是：

```text
raw_payload.status = 2  → studio
raw_payload.status = 3  → customer
```

这不是通用微信协议。接入新的导出器或实时数据源时，必须先验证其发送者字段，并修改或替换角色适配器。未知角色应隔离，不能进入客户分析或风格样本。

解析器只接收：

- `conversation_type=friend`
- `message_type=text`
- `status` 为 2 或 3
- 非空正文
- 可解析且带明确时区的时间戳

群聊、图片、语音、卡片和未知状态会被排除。

## live-inbox 输入

M0 可以直接只读解析 live-inbox，不需要复制或改写源文件：

```bash
python3 -m wechat_cs build \
  --input /Volumes/GPFS/wechat-live-inbox/events.jsonl \
  --input-format live-inbox \
  --state /Volumes/GPFS/wechat-live-inbox/state.json \
  --accounts-config .wechat-cs/config/accounts.local.json \
  --db .wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3
```

实际事件使用顶层字段：`event_id`、`account_profile`、`message_timestamp`、
`message_time`、`conversation_id`、`chat_type`、`sender`、`message_type` 和
`text`。接受规则为：

- `chat_type=private`。
- `message_type=文本`。
- `event_id` 全局去重；同 ID 不同内容隔离为冲突。
- `message_timestamp` 是时间真值，按 `Asia/Shanghai` 保存为带 `+08:00` 的 ISO-8601。
- `message_time` 只作交叉检查，偏差超过 60 秒即隔离。
- 空 `sender` 是客户；非空 sender 只有与本地配置的 `self_sender` 精确相等才是工作室，其余隔离。
- `conversation_id` 和 sender 原值只在解析进程内短暂存在；派生库只保存 HMAC 标识。

`state.json.accounts.<profile>.last_poll_at` 是各账号的观察边界。只有
`initialized=true`、`last_error` 为空且边界不早于该账号最新消息时才可使用；缺失或异常时保存
`NULL`，后续不得退回使用全库最后消息时间。顶层 `status=stopped` 可以表示批次正常结束，
不能单独判失败；`event_count` 也不是累计游标。

账号配置只能放在 `.wechat-cs/config/accounts.local.json`。仓库中的
`config/accounts.example.json` 只含虚构值。账号配置在人工确认前应保持 `state=review`。

## 实时数据适配建议

实时采集器建议先转换为以下规范事件，再落成上述批处理格式或接入后续增量处理器：

```json
{
  "event_id": "global-idempotency-id",
  "account_id": "account-namespace",
  "conversation_id": "stable-conversation-id",
  "conversation_type": "friend",
  "sender_id": "sender-id",
  "self_id": "studio-account-id",
  "role": "studio | customer | unknown",
  "message_type": "text",
  "text": "消息正文",
  "sent_at": "2026-07-11T12:00:00+08:00",
  "received_at": "2026-07-11T12:00:01+08:00",
  "source_version": "collector-version"
}
```

实时角色优先通过 `sender_id == self_id` 判断。上线前仍需完成人工角色校验；采集器版本或账号变化后应重新校验。

## 输出边界

默认输出数据库：

```text
.wechat-cs/data/wechat_cs.sqlite3
```

它是脱敏派生数据库，不是原始聊天备份。原始输入目录应保持只读，并独立管理保留期限和访问权限。

M0 使用更严格的隔离发布流程：

```text
.wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3   # working DB
.wechat-cs/data/wechat_cs_m0.sqlite3            # validated published DB
```

`init-m0-run` 创建 working DB；`validate-m0` 只有在 SQLite 完整性、外键检查以及
M0-A/B/C/D/集成验收门禁全部通过后才把运行标为 `complete`；`publish-m0` 只接受已完成的
working DB，并在 `.wechat-cs/` 内原子发布。
所有命令必须使用同一个至少 32 字符、非默认的 `WECHAT_CS_HMAC_SECRET`；数据库只保存不可逆
指纹，指纹变化会硬停止。

## M0 身份绑定输入

Task 3 只读取经过人工确认的账号别名和绑定 CSV：

```bash
python3 -m wechat_cs import-bindings \
  --db .wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3 \
  --bindings /read-only/path/高置信可直接使用表.csv \
  --accounts-config .wechat-cs/config/accounts.local.json
```

CSV 至少需要中文列：`账号`、`客户手机号`、`微信原始ID`、`绑定置信度`。

自动批准必须同时满足：

- 本地账号配置为 `approved`，并且 `binding_account_alias` 与 CSV `账号` 精确相等。
- 同账号范围内 `微信原始ID` 精确相等。
- 手机号可以规范化为合法的中国大陆手机号。
- 绑定置信度不低于 `0.95`。
- 同一 `(账号, 微信原始ID)` 只对应一个手机号。

`0.82` 只进入 `review`；同一复合键对应多个手机号进入 `conflict`；未知账号、缺 raw ID、
非法手机号和没有 conversation ref 的行只进入聚合统计，不自动创建客户。昵称、姓名和显示名不能用于批准。

手机号和微信 raw ID 只在导入进程内短暂存在。数据库只保存全局 `phone_hmac`、账号范围的
`raw_wechat_id_hash`、匹配方法、置信度、状态和源哈希。CLI 不输出手机号、raw ID、姓名、路径或单行明细。

### 订单资格与奥莱4飞书证据

订单关联还必须先通过会话名称资格门：最新有效私聊事件的 `conversation_name` 只有包含
`下单客户` 或 `相册客户` 才能进入订单连接。派生库只保存
`order_customer | album_customer | order_ineligible` 枚举，不保存会话名称。

奥莱4使用 Dashboard 已只读同步的飞书订单缓存作为确定性证据：

```bash
python3 -m wechat_cs import-feishu-bindings \
  --db .wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3 \
  --events /Volumes/GPFS/wechat-live-inbox/events.jsonl \
  --accounts-config .wechat-cs/config/accounts.local.json \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_realtime.json \
  --target-profile aolai4
```

自动批准只允许客户发送的唯一手机号精确存在于订单源，或客户发送的唯一单号在订单源中只映射到
一个手机号。多手机号、单号多手机号和证据互相冲突进入 `conflict`；姓名、备注相似只能人工复核。
手机号和单号明文只在内存中参与连接。

## M0 订单事实输入

Task 4 只读 Dashboard 的 `orders_live.json` envelope：

```bash
python3 -m wechat_cs import-orders \
  --db .wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3 \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json
```

顾客成交只认有效的 `顾客付款日期/pay_date` 与大于零的 `收款额/revenue` 同时存在。
`打款金额/pay_amount`、`打款日期/pay_date_actual` 和 `是否打款/is_paid` 属于厂家结算，永不回退成顾客成交。
退款类型规范为 `cancel | return | return_taro | exchange | compensation | other`；异常日期、缺退款字段、
退款额大于收入和未结束售后保留 quality flag。订单源的手机号、单号、姓名和地址不进入派生库；
订单手机号只保存全局 HMAC，退款原因会再次移除同订单中的客户标识。
