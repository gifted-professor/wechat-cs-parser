# WeChat 售前决策卡与客服 Skill 挖掘 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把只读微信明文事件、微信身份绑定和飞书订单结果接成一条可复核的数据链，生成不偷看未来的售前决策卡，让强模型制定规则、国产/普通模型批量标注，并从真实付款与退款结果中产出候选客服 Skill。

**Architecture:** 原始微信、绑定 CSV 和 Dashboard 订单 JSON 始终只读；所有标准化事件、HMAC 身份、决策卡、模型标注、结果标签和 Skill 候选只写入项目自己的 `.wechat-cs/`。程序负责身份与订单真值，模型只看 `as_of_at` 以前的脱敏会话并输出带证据的严格 JSON，程序随后挂接 1/3/7/30 天结果并计算统计，避免数据泄漏和因果夸大。

**Tech Stack:** Python 3.9+ 标准库、SQLite、JSON/JSONL/CSV、现有 `urllib` OpenAI-compatible 调用、`unittest`、现有本地 HTTP API 与静态工作台。

---

## 0. 已确认范围与非目标

第一版只做售前成交决策，不把售后处置和自动发送一起塞进 MVP。

必须完成：

- 读取 `/Volumes/GPFS/wechat-live-inbox/events.jsonl`，不得写入、移动或删除源文件。
- 用 `(canonical_account_id, raw_wechat_id)` 关联高置信绑定，再用全局 phone HMAC 关联订单。
- 订单成交只认 `顾客付款日期/pay_date + 收款额/revenue`，绝不回退到厂家 `打款金额/pay_amount`。
- 模型盲分析会话；付款、退款、平台和留存结果在模型返回后由程序挂接。
- 每个模型判断都要引用真实 `message_key`，缺证据时输出 `unknown` 或进入复核。
- 输出候选 Skill，先人工审核，不自动发送微信消息。

第一版不做：

- 不微调模型；先用严格 Schema、金标集和盲测比较模型。
- 不声称某条话术“导致”成交；只报告关联率、样本量和不确定性。
- 不把整个客户历史当一张卡；使用 `message -> turn -> episode -> decision card -> skill card`。
- 不把昵称、显示名或模型猜测当身份主键。
- 不把 `换`、`补`一律当成成交失败。

当前目录不是 Git 仓库。以下每个 Task 保留提交检查点；只有后续初始化 Git 或接入正式仓库后才执行 `git add/commit`，不得伪造已提交状态。

## 1. 目标数据流

```text
read-only events.jsonl
  -> live-inbox adapter
  -> normalized messages / turns / episodes
  -> blind decision cards -----------------------> model annotations
                                                      |
read-only binding CSV -> account registry -> phone HMAC
                                                      |
read-only orders_live.json -> canonical orders -> rule outcomes
                                                      |
                         annotations + outcomes ------+
                                      -> evaluated patterns
                                      -> candidate skill cards
                                      -> human review / shadow-mode suggestions
```

## 2. 建议里程碑

- **M0 真值链路：** Task 1-6。做到微信事件、身份、订单、决策卡、结果标签可重复构建。
- **M1 模型标注：** Task 7-9。冻结标签协议，形成 500 张首批金标卡并完成国产模型盲测。
- **M2 Skill 候选：** Task 10。只从通过质检的标注和可观察结果中聚合候选 Skill。
- **M3 人工工作台：** Task 11-12。审核候选 Skill，并在人工确认模式下用于回复建议。

Task 1-6 的执行接口、只读快照、观察窗口、身份置信度和验收门禁已进一步收紧；实施 M0 时以 [`2026-07-13-m0-truth-chain-task-1-6.md`](2026-07-13-m0-truth-chain-task-1-6.md) 为准。

## 2.1 实施前 6 卡子代理 Pilot（2026-07-13）

已从真实 live-inbox 只读抽取 6 张脱敏售前决策卡，覆盖延迟承诺、价格异议、问价、库存尺码、明确购买和一般售前，三个账号各 2 张。源 `events.jsonl` 前后 size、mtime_ns、SHA-256 完全一致；卡片不含订单结果、未来消息文本、手机号或 raw ID。

两名盲标子代理独立输出后：

```text
结构有效：6/6 vs 6/6
证据 ID 属于卡片：6/6 vs 6/6
intent_stage 一致：5/6 = 83.3%
recommended_action 一致：4/6 = 66.7%
followup.has_hook 一致：1/6 = 16.7%
signal(type,direction) 平均 Jaccard：11.1%
```

结论：数据切片和严格 JSON 路线可行，但标签字典目前不能冻结。两个代理都能引用合法证据，却使用了不同的自由文本 signal 类型，并对“什么算跟进钩子”和“缺少事实时应回复、追问还是转人工”理解不同。Task 7-9 必须先修正以下边界：

- signal type 使用固定枚举；禁止模型发明新 type，无法归类时使用 `other/unknown` 并进入人工字典扩展流程。
- signal strength 固定为 `weak|medium|strong`；`moderate` 等同义值直接 Schema 失败，不在运行时偷偷归一化。
- 每个 signal 增加 `scope=current_turn|prior_context` 和 `rank=primary|secondary`，避免把历史售后信号当成当前主意图。
- followup 不再由模型直接给 `has_hook`；模型输出严格 `hook_type=none|exact_time|relative_time|condition|unspecified` 与 `explicitness`，程序派生 has_hook。
- followup 时间使用固定 bucket，禁止模型自由写 `later_same_day_or_near_term` 这类混合范围。
- recommended action 必须附 `reason_codes`、`required_facts` 和 `blockers`；事实不足时区分“追问事实”和“高风险转人工”。
- 把 evidence 拆成 `reference_validity`（ID 存在）与 `entailment_fidelity`（证据确实支持判断）；前者 100% 不代表后者正确。
- 自报 confidence 不能单独作为放行标准；模型分歧必须进入复核队列。
- 抽样器增加 `eligibility=presales|mixed|not_presales|unknown`，第一版只自动放行 presales；mixed 进入人工复核。
- 当前脱敏器对“详细地址：某学校/某机构”这类无门牌数字的语义地址存在 false negative；外部模型调用前必须增加结构化字段前缀脱敏和相应 fixture 门禁。

实施 Task 7 前先把 pilot 扩到至少 20 张并再次双盲；只有 action、hook 和 primary signal 的边界达到预定一致率后，才把 schema 从 draft 升为 v1。

### Task 1: 建立 Schema v2 与可重复迁移

**Files:**
- Modify: `wechat_cs/store.py:16-171`
- Modify: `wechat_cs/build.py:237-341`
- Modify: `tests/test_core.py`
- Create: `tests/test_schema_v2.py`

**Step 1: 写失败测试，锁定新表和重建保留行为**

```python
def test_schema_v2_contains_decision_pipeline_tables(self):
    connection = open_store(self.db_path)
    initialize_schema(connection)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    self.assertTrue({
        "account_registry",
        "conversation_links",
        "orders",
        "decision_cards",
        "card_annotations",
        "card_outcomes",
        "skill_cards",
    }.issubset(names))
```

再增加一个重建测试：写入人工审核过的 annotation/skill 状态，重新构建数据库后状态仍存在；若对应 card 已消失，则安全丢弃孤儿行。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_schema_v2 -v`

Expected: FAIL，提示新表不存在或 `SCHEMA_VERSION` 仍为 1。

**Step 3: 最小实现 Schema v2**

在 `wechat_cs/store.py`：

```python
SCHEMA_VERSION = 2

def initialize_schema(connection: sqlite3.Connection) -> None:
    # 保留现有 v1 表；追加 v2 表和索引；不删除用户审核数据。
    ...
```

表的最小字段：

```text
account_registry(profile_id, canonical_account_id, state, confidence, evidence_json, version)
conversation_links(link_id, customer_key, profile_id, raw_wechat_id_hash,
                   phone_hmac, match_method, confidence, state, source_hash)
orders(order_line_id, record_id, phone_hmac, paid_on, revenue, platform,
       refund_type, refund_reason, refund_amount, refund_on, return_status,
       source_hash, quality_flags_json)
decision_cards(card_id, customer_key, episode_id, card_type, as_of_at,
               context_json, action_json, evidence_message_keys_json,
               split, review_status, created_at)
card_annotations(annotation_id, card_id, schema_version, prompt_version,
                 model_name, input_hash, annotation_json, state, error_code,
                 reviewed_at, created_at)
card_outcomes(card_id, paid_1d, paid_3d, paid_7d, retained_30d,
              aftersale_30d, exchange_30d, compensation_30d,
              refund_loss_ratio, attribution_state, matched_orders_json,
              computed_at)
skill_cards(skill_id, version, definition_json, stats_json,
            review_status, created_at, reviewed_at)
```

布尔结果使用 `0/1/NULL`；`NULL` 表示观察窗口不足或事实不完整，不能偷换成 false。

扩展 `_snapshot_existing()` / `_restore_existing()`，只恢复稳定 ID 能重新命中的人工审核和 Skill 状态，不恢复可重新计算的订单 outcome。

**Step 4: 运行测试确认通过**

Run: `python3 -m unittest tests.test_schema_v2 tests.test_core -v`

Expected: PASS；现有 v1 数据库构建与 ChatML 导出测试不回归。

**Step 5: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/store.py wechat_cs/build.py tests/test_schema_v2.py tests/test_core.py
git commit -m "feat: add decision pipeline schema v2"
```

### Task 2: 添加 live-inbox 只读适配器

**Files:**
- Create: `wechat_cs/live_inbox.py`
- Create: `config/accounts.example.json`
- Create: `tests/fixtures/live_inbox/events.jsonl`
- Create: `tests/fixtures/live_inbox/accounts.json`
- Create: `tests/test_live_inbox.py`
- Modify: `wechat_cs/build.py:34-186`
- Modify: `wechat_cs/__main__.py:19-51`
- Modify: `DATA_FORMAT.md:65-96`

**Step 1: 用虚构数据写失败测试**

fixture 覆盖：四条文本、一个图片、一个公众号、一个重复 `event_id`、一个未知 sender、两个账号 profile，以及一条不含数字的 `详细地址：某学校`。测试断言：

```python
def test_live_inbox_adapter_filters_and_deduplicates(self):
    snapshot = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET)
    self.assertEqual(len(snapshot.messages), 4)
    self.assertEqual(snapshot.quarantine_counts["unknown_sender"], 1)
    self.assertTrue(all(item.message_key.startswith("message_") for item in snapshot.messages))
    self.assertEqual({item.role for item in snapshot.messages}, {"studio", "customer"})
```

另写只读测试：适配前后源文件 size、mtime_ns、SHA-256 均不变。

另写脱敏回归测试：`详细地址/收货地址/所在地区/手机号码/联系人` 等结构化字段整行替换为对应占位符，即使值中没有数字也不能进入 blind card。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_live_inbox -v`

Expected: FAIL，`wechat_cs.live_inbox` 不存在。

**Step 3: 实现标准化适配器**

```python
@dataclass
class SourceSnapshot:
    messages: List[Message]
    conversation_index: Dict[str, Dict[str, Any]]
    first_at: Optional[datetime]
    last_at: Optional[datetime]
    source_hash: str
    quarantine_counts: Dict[str, int]

def load_live_inbox(
    events_path: Path,
    accounts_config: Path,
    *,
    secret: str,
) -> SourceSnapshot:
    ...
```

映射规则：

- `chat_type == private`
- `message_type == 文本`
- `text.strip()` 非空
- `event_id` 全局幂等
- `account_profile` 必须存在于本地账号配置
- `sender == configured self_sender` -> `studio`
- `sender == ""` -> `customer`
- 其他 sender -> `unknown_sender` 隔离
- 时间以整数 `message_timestamp`（Unix epoch seconds）为真值，转换成 `Asia/Shanghai` 的带时区 ISO-8601；当前源里的 `message_time` 只有分钟且不带时区，只作人类可读交叉检查，不能直接进入 `parse_timestamp()`；`observed_at` 仅用于接收延迟审计
- `message_timestamp` 与按上海时区解释的 `message_time` 偏差超过 60 秒时记录 `timestamp_mismatch` 并隔离/复核
- `customer_key = HMAC(account_profile, conversation_id)`，不持久化原始会话 ID

`config/accounts.example.json` 只放占位值；真实映射放 `.wechat-cs/config/accounts.local.json`，不提交。

**Step 4: 接入现有 build 命令**

给 `build` 增加：

```text
--input-format auto|export|live-inbox
--accounts-config .wechat-cs/config/accounts.local.json
```

`auto`：目录含 `conversation_index.json + messages.jsonl` 时走旧适配器；文件名为 `events.jsonl` 时走 live-inbox。两条路径最终都返回同一个 `SourceSnapshot`，后续分析逻辑不复制。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_live_inbox tests.test_core -v`

Expected: PASS；未知角色不进入 messages/style_pairs。

测试还必须断言所有规范化 message timestamp 都带 `+08:00`，不能把源 `YYYY-MM-DD HH:mm` 当成本地无时区 datetime 存储。

Run: `python3 -m wechat_cs build --help`

Expected: 显示两个新参数，不打印账号名、会话 ID 或文本。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/live_inbox.py wechat_cs/build.py wechat_cs/__main__.py config/accounts.example.json DATA_FORMAT.md tests/fixtures/live_inbox tests/test_live_inbox.py
git commit -m "feat: add read-only live inbox adapter"
```

### Task 3: 建立账号 + raw ID -> 全局 phone HMAC 身份桥

**Files:**
- Create: `wechat_cs/identity.py`
- Create: `tests/fixtures/identity/safe_phone_bindings.csv`
- Create: `tests/test_identity.py`
- Modify: `wechat_cs/build.py:385-490`
- Modify: `wechat_cs/store.py:65-80`
- Modify: `wechat_cs/__main__.py`

**Step 1: 写失败测试覆盖复合键、BOM 和冲突**

```python
def test_composite_identity_bridge_is_account_scoped_but_phone_hash_is_global(self):
    rows = load_binding_csv(FIXTURE, registry=REGISTRY, secret=SECRET)
    self.assertEqual(rows[("account-a", "raw-1")].state, "exact")
    self.assertEqual(
        rows[("account-a", "raw-1")].phone_hmac,
        rows[("account-b", "raw-9")].phone_hmac,
    )

def test_raw_id_collision_without_account_never_auto_approves(self):
    ...
```

测试必须证明 UTF-8 BOM CSV 可读、一个复合键多手机号进入 `conflict`、昵称或姓名不能单独触发 `approved`。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_identity -v`

Expected: FAIL，模块不存在。

**Step 3: 实现身份模块**

```python
def normalize_phone(value: str) -> Optional[str]: ...

def global_phone_hmac(secret: str, phone: str) -> str:
    return hmac_id(secret, "phone", normalize_phone(phone))

def load_binding_csv(path: Path, registry: AccountRegistry, secret: str) -> Dict[Tuple[str, str], Binding]:
    # utf-8-sig；优先读取“账号/微信原始ID/客户手机号/绑定置信度/匹配依据”。
    ...
```

重要迁移：现有 `build.py` 的 phone HMAC 包含 `actual_account_id`，导致同一手机号跨微信账号无法连接。新 bridge 使用全局 phone HMAC；旧 account-scoped 候选只作聊天内证据，不直接连接订单，直到迁移完成。

匹配优先级：

1. `(canonical_account_id, raw_wechat_id)` 高置信精确匹配。
2. raw ID 全局唯一且只指向一个手机号。
3. 多证据候选进入 `review`。
4. 冲突进入 `conflict`，禁止模型猜测。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs import-bindings \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --bindings /read-only/path/高置信可直接使用表.csv \
  --accounts-config .wechat-cs/config/accounts.local.json
```

输出只能包含总数、各 state 数量和 source hash，不打印手机号或 raw ID。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_identity tests.test_core -v`

Expected: PASS；序列化数据库检查不包含虚构明文手机号。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/identity.py wechat_cs/build.py wechat_cs/store.py wechat_cs/__main__.py tests/fixtures/identity tests/test_identity.py
git commit -m "feat: add versioned customer identity bridge"
```

### Task 4: 从 orders_live 构建规范订单真值

**Files:**
- Create: `wechat_cs/orders.py`
- Create: `tests/fixtures/orders/orders_live.json`
- Create: `tests/test_orders.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `wechat_cs/store.py`
- Modify: `DATA_FORMAT.md`

**Step 1: 写失败测试锁定业务字段语义**

```python
def test_customer_payment_never_falls_back_to_supplier_payment(self):
    row = fixture_order(pay_date="", revenue=None, pay_amount=199)
    order = normalize_order(row, synced_at="2026-07-13T00:00:00+08:00")
    self.assertFalse(order.purchase_paid)

def test_exchange_without_refund_is_retained_but_has_aftersale_friction(self):
    order = normalize_order(fixture_order(refund_type="换", refund_amount=0))
    self.assertEqual(order.aftersale_type, "exchange")
    self.assertNotEqual(order.retention_state, "lost")
```

fixture 还要覆盖：取消、退、退芋圆、补、其他、部分退款、异常巨大退款、1970 日期、未来退款日、样品/代发和重复 tracking number。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_orders -v`

Expected: FAIL，模块不存在。

**Step 3: 实现订单规范化**

```python
@dataclass(frozen=True)
class CanonicalOrder:
    order_line_id: str
    record_id: str
    phone_hmac: Optional[str]
    paid_on: Optional[date]
    revenue: Optional[Decimal]
    platform: Optional[str]
    refund_type: Optional[str]
    refund_amount: Optional[Decimal]
    refund_on: Optional[date]
    return_status: Optional[str]
    quality_flags: Tuple[str, ...]
```

规则：

- 默认读取 `orders_live.json`，因为 `orders_realtime.json` 当前按 tracking number 合并会丢行。
- `order_line_id = HMAC(source_id, record_id)`；tracking number 不是唯一键。
- 客户付款：有效 `pay_date` 且 `revenue > 0`。
- 厂家 `pay_amount/pay_date_actual/is_paid` 不参与客户成交。
- `取消`：保留付款事实，但有效成交/留存为 false。
- `退/退芋圆`：按退款售后；全退、部分退由 `refund_amount / revenue` 决定。
- `换`：单列 exchange；无退款且流程完成时可保留成交。
- `补`：单列 compensation/recovery，不直接判输。
- `其他` 或事实不完整：outcome unknown。
- 观察窗口不足、日期异常或售后未结束均为 unknown，不能当成功。

**Step 4: 添加 CLI 并持久化**

```text
python3 -m wechat_cs import-orders \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json
```

命令以临时事务导入，失败时保留旧订单快照；输出 source hash、同步时间、记录数、质量 flag 计数。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_orders -v`

Expected: PASS。

Run: `python3 -m unittest discover -s tests -v`

Expected: 全部 PASS。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/orders.py wechat_cs/store.py wechat_cs/__main__.py DATA_FORMAT.md tests/fixtures/orders tests/test_orders.py
git commit -m "feat: normalize order and refund truth"
```

### Task 5: 从 turn/episode 生成无未来信息的售前决策卡

**Files:**
- Create: `wechat_cs/cards.py`
- Create: `tests/test_cards.py`
- Modify: `wechat_cs/core.py:267-380`
- Modify: `wechat_cs/build.py:369-383`
- Modify: `wechat_cs/__main__.py`

**Step 1: 写失败测试覆盖卡片颗粒度**

```python
def test_decision_card_contains_only_information_available_at_as_of(self):
    cards = build_decision_cards(messages, episode_gap_hours=24)
    card = cards[0]
    self.assertTrue(all(item["timestamp"] <= card.as_of_at for item in card.context))
    self.assertNotIn("future-order", json.dumps(card.context))

def test_no_reply_and_delayed_reply_are_actions_not_missing_rows(self):
    self.assertEqual(cards[0].action["type"], "no_reply")
    self.assertEqual(cards[1].action["type"], "delayed_reply")
```

增加同一客户跨三天、一次客服主动跟进、一次售后重启的 fixture，证明不会整段历史混成一张卡。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_cards -v`

Expected: FAIL，模块不存在。

**Step 3: 实现 episode 和 card**

```python
@dataclass(frozen=True)
class DecisionCard:
    card_id: str
    customer_key: str
    episode_id: str
    card_type: str
    as_of_at: str
    context: List[Dict[str, str]]
    action: Dict[str, Any]
    evidence_message_keys: List[str]
    split: str

def segment_episodes(turns: Sequence[Turn], gap_hours: int = 24) -> List[List[Turn]]: ...
def build_decision_cards(..., action_window_hours: int = 24) -> List[DecisionCard]: ...
```

第一版 card 类型：

- `inbound_presales`：客户发言形成决策点。
- `proactive_followup`：长间隔后客服主动触达，用于研究“什么时候跟进”。

action 类型：`immediate_reply`、`delayed_reply`、`no_reply`、`proactive_followup`。保留延迟秒数，但付款只有日期粒度时不得做分钟级因果归因。

上下文最多取当前 episode 最近 8 个 turn；超长文本使用现有 redaction 和长度上限。`stable_split()` 继续按 customer 隔离 train/validation/test。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs build-cards \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --episode-gap-hours 24 \
  --action-window-hours 24
```

重复运行必须得到同样 card IDs 并幂等更新，不复制审核结果。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_cards tests.test_core -v`

Expected: PASS；测试明确断言 context 不含 `as_of_at` 之后的消息。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/cards.py wechat_cs/core.py wechat_cs/build.py wechat_cs/__main__.py tests/test_cards.py
git commit -m "feat: build blind presales decision cards"
```

### Task 6: 在模型分析后挂接 1/3/7/30 天订单结果

**Files:**
- Create: `wechat_cs/outcomes.py`
- Create: `tests/test_outcomes.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `wechat_cs/store.py`

**Step 1: 写失败测试覆盖三态结果与归因歧义**

```python
def test_outcome_window_uses_calendar_dates_and_unknown_for_unobserved_30d(self):
    result = attach_outcome(card, orders, snapshot_on=date(2026, 7, 13))
    self.assertEqual(result.paid_3d, True)
    self.assertIsNone(result.retained_30d)

def test_same_day_multiple_cards_are_associated_not_claimed_causal(self):
    self.assertEqual(result.attribution_state, "ambiguous_same_day")
```

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_outcomes -v`

Expected: FAIL，模块不存在。

**Step 3: 实现规则 outcome**

```python
def attach_outcome(
    card: DecisionCard,
    orders: Sequence[CanonicalOrder],
    *,
    snapshot_on: date,
) -> CardOutcome:
    ...
```

连接条件：approved `conversation_link.phone_hmac == orders.phone_hmac`。计算：

- `paid_1d/3d/7d`
- `retained_30d`
- `aftersale_30d`
- `exchange_30d`
- `compensation_30d`
- `refund_loss_ratio`
- `attribution_state = none | associated | ambiguous_same_day | multiple_orders | identity_unverified`

Skill 成功率只使用 `associated` 或明确允许的多订单聚合；`ambiguous_same_day` 可用于相关性观察，不能声称某个分钟级回复促成付款。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs attach-outcomes \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --snapshot-on 2026-07-13
```

输出各 outcome 可观察数与 unknown 数，不输出客户明细。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_outcomes tests.test_orders tests.test_cards -v`

Expected: PASS。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/outcomes.py wechat_cs/store.py wechat_cs/__main__.py tests/test_outcomes.py
git commit -m "feat: attach post-decision order outcomes"
```

### Task 7: 冻结 Decision Card Annotation v1 与首批金标流程

**Files:**
- Create: `config/decision_card_labels.v1.json`
- Create: `docs/DECISION_CARD_SCHEMA.md`
- Create: `wechat_cs/gold.py`
- Create: `tests/test_gold.py`
- Modify: `wechat_cs/__main__.py`

**Step 1: 写失败测试锁定严格标签和证据引用**

```python
def test_annotation_requires_evidence_for_every_non_unknown_claim(self):
    with self.assertRaisesRegex(ValueError, "evidence"):
        validate_annotation({
            "intent_stage": "ready_to_buy",
            "signals": [{"type": "purchase_commitment", "direction": "positive"}],
        }, allowed_message_keys={"m1"})
```

再测：证据 ID 不属于卡片、单一 `sentiment_score`、未知枚举、模型写入付款结果均拒收。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_gold -v`

Expected: FAIL。

**Step 3: 定义 v1 标签协议**

最小输出：

```json
{
  "eligibility": "presales|mixed|not_presales|unknown",
  "intent_stage": "browsing|considering|ready_to_buy|delayed_commitment|declined|unknown",
  "information_items": [
    {"type": "need|preference|budget|timing|constraint|question|commitment|noise",
     "value_class": "new_fact|state_change|confirmation|repetition|unknown",
     "explicitness": "explicit|inferred",
     "scope": "current_turn|prior_context",
     "evidence_message_keys": ["message_x"]}
  ],
  "signals": [
    {"type": "price_inquiry|price_objection|stock_or_size_inquiry|explicit_purchase_intent|explicit_future_commitment|trust_or_quality_concern|policy_or_return_concern|transaction_friction|decline|other|unknown",
     "rank": "primary|secondary",
     "scope": "current_turn|prior_context",
     "direction": "positive|negative|mixed|neutral",
     "strength": "weak|medium|strong",
     "evidence_message_keys": ["message_x"]}
  ],
  "recommended_action": "reply_now|ask_clarifying_question|wait|schedule_followup|handoff|unknown",
  "action_reason_codes": ["answer_known_fact|missing_product_fact|explicit_future_hook|high_risk|declined|unknown"],
  "required_facts": ["price|inventory|size|policy|order_state|none"],
  "blockers": [],
  "followup": {
    "hook_type": "none|exact_time|relative_time|condition|unspecified",
    "time_bucket": "none|same_day|within_3d|within_7d|over_7d|known_date|unspecified",
    "explicitness": "explicit|inferred|none",
    "evidence_message_keys": ["message_x"]
  },
  "risk_flags": [],
  "confidence": 0.0,
  "uncertainties": []
}
```

不得出现订单 outcome 字段。`explicitness=inferred` 必须降低 confidence；没有证据只能 unknown。`followup.has_hook` 由程序按 `hook_type != none` 派生，模型不能直接填写。`hook_type=none` 时必须 `time_bucket=none`。自由文本 signal type/strength/time bucket 一律 Schema 失败；新增类型必须修改 label dictionary、升版本并重跑盲测。

标签说明必须给出以下硬边界：

- `ready_to_buy`：客户有明确购买承诺，且当前没有未解决的价格、库存、款式或政策前置条件。
- `considering`：有兴趣，但购买仍依赖至少一个未解决条件。
- `delayed_commitment`：客户明确给出未来购买/联系时间或条件；普通“我再看看”不能自动算。
- action 优先级：高风险或业务域不匹配 -> `handoff`；域内但缺关键事实 -> `ask_clarifying_question`；事实足够且可安全回答 -> `reply_now`；有明确未来 hook 且当下无需回答 -> `schedule_followup/wait`。

**Step 4: 实现分层抽样与金标导入导出**

```text
python3 -m wechat_cs sample-gold --db ... --output .wechat-cs/gold/cards.v1.jsonl --limit 500
python3 -m wechat_cs import-gold --db ... --input .wechat-cs/gold/reviewed.v1.jsonl
```

抽样覆盖账号、月份、平台、付款/未付款、取消/退款、不同回复动作；这些结果只用于抽样分层，不进入待标注卡片正文。首批 500 张；若任一核心标签独立客户不足 30，再扩到 1000。

盲测集必须冻结且不参与 prompt 修改。

在 500 张金标前先执行 20 张 schema pilot：两名独立标注者对 `recommended_action`、`hook_type` 和 primary signal 的一致率均达到 80%，否则继续改标签说明，不扩大样本。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_gold -v`

Expected: PASS；导出 JSONL 不包含 phone、raw ID、订单结果或未来消息。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add config/decision_card_labels.v1.json docs/DECISION_CARD_SCHEMA.md wechat_cs/gold.py wechat_cs/__main__.py tests/test_gold.py
git commit -m "feat: define decision card annotation v1"
```

### Task 8: 添加可替换国产模型的批量标注执行器

**Files:**
- Create: `wechat_cs/annotations.py`
- Create: `tests/test_annotations.py`
- Modify: `.env.example`
- Modify: `wechat_cs/__main__.py`
- Modify: `wechat_cs/api.py:1031-1101`

**Step 1: 写失败测试覆盖盲输入、严格 JSON 和幂等**

```python
def test_model_payload_never_contains_outcome_or_future_messages(self):
    payload = build_annotation_payload(card)
    serialized = json.dumps(payload, ensure_ascii=False)
    self.assertNotIn("paid_7d", serialized)
    self.assertNotIn("refund", serialized)
    self.assertNotIn("future-message", serialized)

def test_model_payload_has_no_structured_contact_field_values(self):
    payload = build_annotation_payload(card_with_semantic_address)
    serialized = json.dumps(payload, ensure_ascii=False)
    self.assertNotIn("某学校", serialized)
    self.assertIn("[地址]", serialized)

def test_invalid_evidence_is_rejected_and_not_marked_complete(self):
    ...
```

mock provider 返回非法 JSON、未知枚举、伪造 message ID、429、500 和超时，分别验证拒收或有限重试。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_annotations -v`

Expected: FAIL。

**Step 3: 提取通用 OpenAI-compatible 客户端**

```python
class OpenAICompatibleClient:
    def complete_json(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]: ...

def analyze_card(card: DecisionCard, client, schema, prompt_version: str) -> AnnotationResult: ...
```

新增环境变量：

```text
WECHAT_CS_ANALYZER_API_KEY=
WECHAT_CS_ANALYZER_BASE_URL=
WECHAT_CS_ANALYZER_MODEL=
WECHAT_CS_ANALYZER_PROMPT_VERSION=decision-card-v1
```

现有 Kimi 可作为第一个 provider，但实现不得把批量分析锁死为单一品牌。`temperature=0`，严格 JSON；存储 model、prompt、schema、input hash。相同组合已成功时幂等跳过。

失败策略：

- 429/5xx/网络超时有限退避重试。
- Schema/证据失败不盲重试，记录 `invalid_schema` 并进入复核。
- 高风险、低置信度、新标签和 provider 分歧进入 teacher/人工队列。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs analyze-cards \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --split train \
  --limit 50 \
  --mode batch
```

默认 dry-run 只打印计划数量和 token 估算；必须显式 `--execute` 才调用外部模型。输出不得打印聊天正文。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_annotations tests.test_api -v`

Expected: PASS，测试不需要真实 API key。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/annotations.py wechat_cs/api.py wechat_cs/__main__.py .env.example tests/test_annotations.py
git commit -m "feat: add evidence-bound batch annotation runner"
```

### Task 9: 建立盲测评估与模型放量门槛

**Files:**
- Create: `wechat_cs/evaluation.py`
- Create: `tests/test_evaluation.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `README.md`

**Step 1: 写失败测试验证按标签指标而非总准确率**

```python
def test_evaluation_reports_macro_f1_and_evidence_fidelity(self):
    report = evaluate_annotations(gold, predicted)
    self.assertIn("intent_macro_f1", report)
    self.assertIn("critical_risk_recall", report)
    self.assertIn("evidence_fidelity", report)
    self.assertIn("schema_valid_rate", report)
```

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_evaluation -v`

Expected: FAIL。

**Step 3: 实现评估报告**

报告至少包含：

- 每个 intent/signal/action 标签的 precision、recall、F1、support。
- intent macro-F1。
- evidence reference validity：引用 ID 是否属于卡片。
- evidence entailment fidelity：人工/金标是否确认该证据支持结论。
- Schema 有效率、unknown 率、人工升级率。
- 双模型/双标注者对 recommended action、hook type、primary signal 的一致率。
- confidence calibration：高 confidence 是否真的更准确；不能只统计模型自报分数。
- 高风险 recall。
- 账号、平台、月份分层结果，识别模型漂移。
- 模型版本、prompt 版本、schema 版本、盲测集 hash。

第一版放量门槛：

```text
schema_valid_rate >= 99%
evidence_reference_valid_rate >= 99%
evidence_entailment_fidelity >= 90%
critical_risk_recall >= 95%
intent_macro_f1 >= 85%
recommended_action_agreement >= 80%
followup_hook_agreement >= 80%
primary_signal_agreement >= 80%
low_confidence_routed_to_review == 100%
model_disagreement_routed_to_review == 100%
```

门槛是上线闸门，不是“模型真理”；标签定义变更必须升 schema 版本并重跑盲测。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs evaluate-analyzer \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --split test \
  --output .wechat-cs/reports/analyzer-v1.json
```

同时生成同名 Markdown 摘要，只含聚合指标。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_evaluation -v`

Expected: PASS。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/evaluation.py wechat_cs/__main__.py README.md tests/test_evaluation.py
git commit -m "feat: add blind annotation quality gates"
```

### Task 10: 从通过质检的卡片聚合候选客服 Skill

**Files:**
- Create: `wechat_cs/skill_cards.py`
- Create: `tests/test_skill_cards.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `wechat_cs/store.py`

**Step 1: 写失败测试锁定统计来源与发布门槛**

```python
def test_skill_stats_are_computed_not_accepted_from_model(self):
    candidate = build_skill_candidate(cards, annotations, outcomes)
    self.assertEqual(candidate.stats["independent_customers"], 40)
    self.assertNotIn("model_claimed_success_rate", candidate.stats)

def test_small_or_unobserved_sample_cannot_publish(self):
    self.assertEqual(candidate.review_status, "insufficient_evidence")
```

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_skill_cards -v`

Expected: FAIL。

**Step 3: 实现候选 Skill 聚合**

候选定义包含：

```text
trigger_conditions
exclusion_conditions
required_facts
customer_intent
positive/negative/mixed signals
recommended_action
recommended_wait_window
reply_structure
forbidden_promises
applicable_platforms
```

统计全部由程序计算：

```text
independent_customers
decision_cards
paid_1d/3d/7d rate + Wilson interval
retained_30d rate + observable denominator
refund/return/exchange/compensation rate
platform/month stability
ambiguous attribution count
```

默认至少 30 个独立客户才能进入 `candidate_review`；不足时 `insufficient_evidence`。任何 Skill 都必须人工审核后才能 `published`。强模型可总结名称和解释，但不得提供成功率。

**Step 4: 添加 CLI**

```text
python3 -m wechat_cs build-skills \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --min-customers 30 \
  --output .wechat-cs/exports/skill-candidates.v1.json
```

**Step 5: 验证**

Run: `python3 -m unittest tests.test_skill_cards tests.test_outcomes -v`

Expected: PASS；Skill JSON 不含聊天正文、手机号或原始会话 ID。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/skill_cards.py wechat_cs/store.py wechat_cs/__main__.py tests/test_skill_cards.py
git commit -m "feat: aggregate evidence-based customer service skills"
```

### Task 11: 在工作台增加决策卡与 Skill 人工复核

**Files:**
- Modify: `wechat_cs/api.py:227-1101`
- Modify: `wechat_cs/static/index.html`
- Modify: `wechat_cs/static/app.js`
- Modify: `wechat_cs/static/styles.css`
- Modify: `tests/test_api.py`

**Step 1: 写失败 API 测试**

```python
def test_decision_card_endpoint_separates_annotation_and_outcome(self):
    status, _, payload = self.request("GET", "/v1/decision-cards?status=review")
    self.assertEqual(status, 200)
    self.assertIn("annotation", payload["items"][0])
    self.assertIn("outcome", payload["items"][0])
    self.assertNotIn("phone", set(keys_in(payload)))
```

再测 Skill 审核状态只能是 `candidate_review/approved/rejected/published`，高风险或低样本 Skill 不能直接 published。

**Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_api.ApiIntegrationTests -v`

Expected: FAIL，新 endpoint 不存在。

**Step 3: 实现 API**

新增：

```text
GET   /v1/decision-cards
GET   /v1/decision-cards/{card_id}
PATCH /v1/decision-cards/{card_id}/review
GET   /v1/skills
GET   /v1/skills/{skill_id}
PATCH /v1/skills/{skill_id}/review
```

列表默认只返回脱敏摘要和证据 ID；详情在授权后返回卡片内脱敏文本。所有响应 `Cache-Control: no-store`，保留现有 bearer token 边界。

**Step 4: 实现最小 UI**

增加两个导航页：

- “决策卡审核”：并排显示模型标签、证据消息、程序 outcome、人工 verdict。
- “Skill 候选”：显示触发/排除条件、建议动作、样本量、付款/留存/退款区间和版本。

明确视觉区分：`事实`、`模型判断`、`推测`、`结果未知`。

**Step 5: 验证**

Run: `python3 -m unittest tests.test_api -v`

Expected: PASS。

Run: `node --check wechat_cs/static/app.js`

Expected: exit 0。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/api.py wechat_cs/static/index.html wechat_cs/static/app.js wechat_cs/static/styles.css tests/test_api.py
git commit -m "feat: add decision and skill review workbench"
```

### Task 12: 端到端 dry-run、文档与首批金标交付

**Files:**
- Modify: `README.md`
- Modify: `DATA_FORMAT.md`
- Modify: `.env.example`
- Create: `docs/OPERATIONS.md`
- Create: `tests/test_end_to_end_pipeline.py`

**Step 1: 写端到端虚构 fixture 测试**

测试链：

```text
synthetic events
-> build
-> import-bindings
-> import-orders
-> build-cards
-> mocked analyze-cards
-> attach-outcomes
-> evaluate-analyzer
-> build-skills
```

断言：源 fixture hash 未变、无未来信息进入模型 payload、同客户不跨 split、未知结果保持 NULL、输出无 PII。

**Step 2: 运行测试确认失败后补齐缺口**

Run: `python3 -m unittest tests.test_end_to_end_pipeline -v`

Expected before fixes: FAIL；补齐命令编排和健康状态后 PASS。

**Step 3: 更新文档**

`README.md` 增加三阶段命令：

1. 本地真值构建。
2. 首批 500 张金标与盲测。
3. 通过门槛后批量标注和 Skill 候选审核。

`docs/OPERATIONS.md` 必须写明：

- 三个源目录只读。
- 每次运行记录 source/model/prompt/schema hash。
- 外部模型调用默认 dry-run。
- 失败恢复和幂等重跑方法。
- 不自动发微信消息。
- 观察窗口不足不能算“未退款”。

**Step 4: 全量验证**

Run: `python3 -m py_compile wechat_cs/*.py`

Expected: exit 0。

Run: `python3 -m unittest discover -s tests -v`

Expected: 全部 PASS，无真实数据依赖。

Run: `node --check wechat_cs/static/app.js`

Expected: exit 0。

Run: `node --check dashboard_integration/wechat_cs_proxy.js`

Expected: exit 0。

**Step 5: 真实数据只读 smoke（单独执行，不进自动测试）**

先运行所有导入命令的 `--dry-run`，确认源 hash 和预计数量；再只写 `.wechat-cs/`：

```bash
python3 -m wechat_cs build \
  --input-format live-inbox \
  --input /Volumes/GPFS/wechat-live-inbox/events.jsonl \
  --accounts-config .wechat-cs/config/accounts.local.json \
  --db .wechat-cs/data/wechat_cs.sqlite3
```

之后抽样核验：

- 100 个身份 link，重点检查冲突和第四账号低覆盖问题。
- 100 张 decision card，检查角色、切段、未来信息泄漏和 no-reply。
- 100 个订单 outcome，检查取消、退、换、补和观察不足。
- 首批 500 张金标，冻结至少 20% 为盲测集。

真实 smoke 前后比较三个源文件/目录的 size、mtime 和 hash；发生变化立即停止。

**Step 6: 提交检查点（仅 Git 可用时）**

```bash
git add README.md DATA_FORMAT.md .env.example docs/OPERATIONS.md tests/test_end_to_end_pipeline.py
git commit -m "docs: add decision skill pipeline operations"
```

## 3. 完成定义

只有同时满足以下条件，MVP 才算完成：

- live-inbox、绑定 CSV、orders_live 均保持只读且有 hash 证明。
- 当前四个账号的角色映射已完成 200 条、99% 的人工校准门槛。
- 决策卡不包含 `as_of_at` 之后的消息或任何订单 outcome。
- 身份和订单结果完全由规则生成；模型不能覆盖。
- 500 张首批金标已完成，盲测集没有参与 prompt 调整。
- 国产/普通模型通过 Task 9 的质量门槛后才允许批量跑。
- Skill 成功率、留存率、退款率由程序计算并显示独立客户数。
- 所有 Skill 先人工审核；回复仅建议/复制，不自动发送。
- 全部单元测试、Python 编译检查和前端语法检查通过。

## 4. 推荐实施顺序与暂停点

先只执行 Task 1-6，交付“真值数据集 v1”并人工抽查；抽查通过再执行 Task 7-9。不要在身份、订单和防泄漏尚未验证时提前大规模调用模型。

第一个业务验收点应是：随机打开一张售前 decision card，能看到当时上下文、客服实际动作、模型盲标签、1/3/7 天付款和 30 天留存/退款结果，而且每项都能追溯到证据，同时没有把未来结果放进模型输入。
