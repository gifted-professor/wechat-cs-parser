# M0 真值链路（Task 1-6） Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在不调用模型、不自动发送消息、也不改动微信/绑定/订单源文件的前提下，构建可重复、可审计的“微信消息 -> 确定性身份 -> 订单事实 -> 盲决策卡 -> 1/3/7/30 天结果”M0 真值数据集。

**Architecture:** 三类真实源都以只读稳定快照方式进入项目，原始手机号和微信 raw ID 只在内存中短暂参与确定性连接，派生库只保存 HMAC 标识。实际客服动作、订单结果和模型可见的盲上下文分层保存；所有未完成观察窗口、低置信身份、异常退款和同日多卡归因都保留为 `NULL/unknown/ambiguous`，不能为了提高覆盖率强行给结论。

**Tech Stack:** Python 3.9+ 标准库、SQLite、JSON/JSONL/CSV、`Decimal`、`zoneinfo`、`unittest`，现有 `wechat_cs` CLI 与 `.wechat-cs/` 本地派生目录。

---

## 0. 执行边界

本计划是总计划中 Task 1-6 的独立执行版；若两份文档在 M0 接口细节上冲突，以本文件为准。它只交付 M0 真值链路；固定信号枚举、主次信号、跟进钩子、推荐动作、双模型标注和 Skill 聚合仍属于 Task 7 以后。

本阶段绝对不做：

- 不调用任何大模型。
- 不自动填写或发送微信消息。
- 不写入飞书、Dashboard 或 Syncthing 目录。
- 不把昵称、姓名或模型猜测用于身份连接。
- 不把厂家打款字段当作顾客付款。
- 不将“相关”描述成“某句话导致成交”。
- 不覆盖当前工作台数据库；真实 M0 首跑先写 `.wechat-cs/runs/<run_id>/`，验收后只发布到独立的 `.wechat-cs/data/wechat_cs_m0.sqlite3`。

只读源：

```text
/Volumes/GPFS/wechat-live-inbox/events.jsonl
/Volumes/GPFS/wechat-live-inbox/state.json
/Volumes/GPFS/Users/a1234/Desktop/Coding/Old/wechat-local-service-kit/out/accounts/customer-phone-binding/高置信可直接使用表.csv
/Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json
```

所有可写产物必须位于：

```text
/Volumes/GPFS/Users/a1234/Desktop/Coding/wechat-cs-parser/.wechat-cs/
```

当前目录不是 Git 仓库。每个 Task 仍保留提交检查点，但只有以后初始化 Git 或迁入正式仓库时才能执行，不得声称已经提交。

## 1. 已确认的当前快照与不能硬编码的事实

以下数字只用来帮助设计测试和人工抽查，执行时必须重新统计，不得写死到业务逻辑：

- `events.jsonl` 当前约 189,245 行、4 个 `account_profile`，历史从 2025-03-11 延续至 2026-07-13。
- 绑定 CSV 当前 2,298 行，其中 2,227 行置信度为 `0.95`，71 行为 `0.82`；文件名不能替代逐行置信度判断。
- live-inbox 有 4 个 profile；`aolai1`、`aolai2`、`service` 使用确认后的绑定 CSV。`aolai4` 先通过“下单客户/相册客户”资格门，再使用客户发送的唯一手机号或唯一单号对应手机号与飞书订单做确定性连接。
- `orders_live.json` 当前约 20,927 条，顶层是含 `records`、`synced_at` 等字段的 envelope，不是裸数组。
- tracking number 存在大量重复组，不能作为订单唯一键。
- 缺手机号、退款字段不全、退款额大于收入、1970 日期和未来退款日都真实存在；`unknown` 是正常结果，不是程序失败。

## 2. M0 数据流和发布边界

```text
read-only events.jsonl
  -> stable source snapshot
  -> normalized messages + conversation refs
  -> 15-minute turns -> 24-hour episodes
  -> blind decision cards + separately stored observed actions

read-only binding CSV
  -> confirmed account map + account-scoped raw ID
  -> global phone HMAC -> approved/review/conflict links

read-only orders_live.json
  -> canonical order facts + quality flags
  -> approved phone HMAC join
  -> tri-state 1/3/7/30-day outcomes + attribution state

all writes -> temporary M0 database
  -> integrity_check + foreign_key_check + acceptance report
  -> atomic publish as .wechat-cs/data/wechat_cs_m0.sqlite3
```

同一批次的所有命令必须使用同一个 HMAC 密钥。数据库只保存不可逆 `hmac_key_fingerprint`；后续命令发现指纹不同必须硬停止，不能静默产生“零连接”。更换 HMAC 密钥只能创建新数据库或全量重建。

下文分步 CLI 中的 `$RUN_DB` 均指 `init-m0-run` 创建的 `.wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3`；在 `publish-m0` 之前不得直接写正式 `.wechat-cs/data/wechat_cs_m0.sqlite3`。

## 3. 四个验收检查点

| 检查点 | 包含任务 | 放行目标 |
|---|---|---|
| M0-A | Task 1-2 | Schema 可回滚；微信只读适配、角色、时区和幂等正确 |
| M0-B | Task 3 | 账号映射经过确认；身份只用确定性证据；低置信和冲突不误连 |
| M0-C | Task 4 | 顾客付款、退款、换货、补偿及异常订单语义正确 |
| M0-D | Task 5-6 | 决策卡不泄漏未来；动作和结果观察窗三态正确；归因不夸大 |

任何检查点失败时只丢弃本次临时产物，旧数据库、人工审核记录和三个只读源保持不变。

### Task 1: 建立 M0 Schema v2、运行快照和密钥一致性门禁

**Files:**
- Modify: `wechat_cs/store.py:16-171`
- Modify: `wechat_cs/build.py:237-342`
- Modify: `wechat_cs/__main__.py:19-97`
- Create: `wechat_cs/source_snapshot.py`
- Create: `tests/test_schema_v2.py`
- Create: `tests/fixtures/schema/v1.sql`

**Step 1: 写失败测试，锁定 M0 表结构**

新增测试要求以下表存在：

```python
def test_schema_v2_creates_m0_truth_tables(self):
    connection = open_store(self.db_path)
    initialize_schema(connection)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    self.assertTrue({
        "schema_migrations",
        "pipeline_runs",
        "source_snapshots",
        "profile_observations",
        "account_registry",
        "conversation_refs",
        "conversation_links",
        "order_snapshots",
        "orders",
        "decision_cards",
        "card_outcomes",
    }.issubset(names))
```

Schema v2 最小字段：

```text
schema_migrations(version, applied_at, checksum)

pipeline_runs(
  run_id, state, parser_version, hmac_key_fingerprint,
  account_config_hash, order_rule_version, card_rule_version,
  started_at, completed_at, quality_json
)

source_snapshots(
  snapshot_id, run_id, source_kind, source_path_hash,
  device, inode, size, mtime_ns, sha256, record_count,
  first_at, last_at, observed_until, captured_at,
  consistency_state, quality_json
)

profile_observations(
  snapshot_id, profile_id, observed_until, initialized,
  last_error_code, consistency_state
)

account_registry(
  profile_id, canonical_account_id, state, confidence,
  evidence_json, config_hash, version
)

conversation_refs(
  customer_key, profile_id, canonical_account_id,
  raw_wechat_id_hash, source_snapshot_id
)

conversation_links(
  link_id, customer_key, profile_id, raw_wechat_id_hash,
  phone_hmac, match_method, confidence, state,
  source_hash, version, reviewed_at
)

order_snapshots(
  order_snapshot_id, source_snapshot_id, synced_at,
  record_count, state, quality_json
)

orders(
  order_line_id, order_snapshot_id, source_namespace, record_id, phone_hmac,
  paid_on, revenue_minor, currency, platform, refund_type, refund_reason,
  refund_amount_minor, refund_on, return_status,
  source_hash, quality_flags_json
)

decision_cards(
  card_id, customer_key, episode_id, card_type, as_of_at,
  boundary_ordinal, source_snapshot_id, action_window_end,
  observation_until, blind_context_json, observed_action_json,
  context_message_keys_json, action_message_keys_json,
  split, review_status, rule_version, created_at
)

card_outcomes(
  card_id, paid_1d, paid_3d, paid_7d,
  retained_30d, aftersale_30d, exchange_30d,
  compensation_30d, refund_loss_ratio,
  attribution_state, attribution_flags_json,
  matched_orders_json, computed_at
)
```

所有布尔结果使用 `0/1/NULL`；SQLite `CHECK` 必须拒绝其他整数。

**Step 2: 写失败测试，锁定角色证据兼容性**

live-inbox 没有旧导出中的 `raw_payload.status=2/3`。修改 `role_calibration`：

- 旧 `source_status` 允许 `NULL`。
- 新增 `source_role_evidence_json`。
- 旧导出保存 status 证据；live-inbox 保存 `profile_id + sender_match` 证据。
- 不允许为了通过旧约束给 live 数据伪造 status。

Task 1 只测试两种角色证据都能落库；200 条 live 分层抽样依赖 Task 2 适配器，在 Task 2 完成并验收。

**Step 3: 写失败测试，锁定迁移、人工状态和可重算数据边界**

测试顺序：

1. `initialize_schema()` 重复执行幂等，并通过 `schema_migrations` / `PRAGMA user_version` 证明真实 v1 -> v2 迁移已执行。
2. v1 人工角色校准、样本审核、草稿反馈完整保留。
3. 稳定 card 仍存在时，人工 `review_status` 保留。
4. card 消失时才清理孤儿审核。
5. `account_registry`、`conversation_refs`、人工确认的 `conversation_links`，以及 active `order_snapshots + orders` 整组在消息重建后保留。
6. `card_outcomes` 永远重算，不从旧库恢复。
7. 故障注入发生在临时库，旧库字节不变。
8. `PRAGMA integrity_check` 返回 `ok`。
9. `PRAGMA foreign_key_check` 无结果。

注意：Task 1 只能建立“延迟恢复”能力，不能在 Task 5 重新生成稳定 card ID 之前清理 card 相关人工状态。

**Step 4: 写失败测试，锁定 HMAC 密钥门禁**

```python
def test_existing_database_rejects_different_hmac_key(self):
    initialize_run(db, secret="first-test-secret-with-at-least-32-chars")
    with self.assertRaisesRegex(RuntimeError, "HMAC key fingerprint mismatch"):
        initialize_run(db, secret="different-test-secret-with-32-plus-chars")
```

指纹只用于一致性比较，不能由它还原密钥。默认开发密钥或短密钥不允许发布真实 M0 数据集。

**Step 5: 运行测试确认失败**

Run:

```bash
python3 -m unittest tests.test_schema_v2 -v
```

Expected: FAIL，提示 M0 表、角色证据字段或密钥门禁尚不存在。

**Step 6: 最小实现 Schema v2 和原子发布辅助函数**

在 `wechat_cs/store.py` 中：

- 将 `SCHEMA_VERSION` 升为 2。
- 用显式迁移记录和 `PRAGMA user_version` 升级真实 v1 库，不能只对空库建表后直接改版本号。
- 保留全部 v1 表和现有 API。
- 创建 M0 表、索引、外键和三态约束。
- 为 `order_snapshots(state='active')` 增加唯一活动快照约束；订单行必须通过外键归属快照。
- 扩展 `_snapshot_existing()` / `_restore_existing()`。
- 只有临时库完整通过检查后才允许 `os.replace()` 发布。
- `schema_version` 只在事务最后写入。

在 `wechat_cs/source_snapshot.py` 中提供通用只读快照和输出路径保护：

```python
def assert_project_output(path: Path, project_root: Path) -> None: ...
def hmac_key_fingerprint(secret: str) -> str: ...
def read_stable_bytes(path: Path, *, retries: int = 1) -> StableBytes: ...
```

`assert_project_output()` 必须拒绝任何解析后落在微信、绑定 CSV 所在目录或 Dashboard 目录中的输出路径。
生产 CLI 使用真实项目根目录；单元测试必须允许显式注入临时 `project_root`，不能把所有 `tempfile` 输出误判为越界。

Task 1 同时增加 `init-m0-run`、`validate-m0` 和 `publish-m0` 三个命令：所有分步任务先写 `.wechat-cs/runs/<run_id>/wechat_cs_m0.sqlite3`，只有完整检查通过后，`publish-m0` 才以原子替换方式发布到 `.wechat-cs/data/wechat_cs_m0.sqlite3`。

**Step 7: 验证 Task 1**

Run:

```bash
python3 -m unittest tests.test_schema_v2 tests.test_core -v
python3 -m py_compile wechat_cs/*.py
```

Expected: PASS；现有 v1 功能无回归。

**Step 8: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/store.py wechat_cs/build.py wechat_cs/__main__.py wechat_cs/source_snapshot.py tests/test_schema_v2.py tests/fixtures/schema/v1.sql
git commit -m "feat: add M0 truth schema and run guards"
```

### Task 2: 添加 live-inbox 只读稳定快照适配器

**Files:**
- Create: `wechat_cs/live_inbox.py`
- Create: `config/accounts.example.json`
- Create: `tests/fixtures/live_inbox/events.jsonl`
- Create: `tests/fixtures/live_inbox/state.json`
- Create: `tests/fixtures/live_inbox/accounts.json`
- Create: `tests/test_live_inbox.py`
- Modify: `wechat_cs/build.py:34-186`
- Modify: `wechat_cs/core.py:41-189`
- Modify: `wechat_cs/__main__.py:19-97`
- Modify: `DATA_FORMAT.md`

**Step 1: 写 12 行虚构 fixture 和第一组失败测试**

fixture 必须包含：

- 4 条有效私聊文本。
- 1 条完全重复 `event_id`。
- 1 条相同 `event_id` 但内容冲突。
- 1 条图片。
- 1 条公众号/非 private。
- 1 条群聊。
- 1 条未知 sender。
- 1 条未知 profile。
- 1 条 `message_timestamp` 与 `message_time` 偏差超过 60 秒。

测试断言：

```python
snapshot = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET)
self.assertEqual(len(snapshot.messages), 4)
self.assertEqual(snapshot.quarantine_counts["duplicate_conflict"], 1)
self.assertEqual(snapshot.quarantine_counts["unknown_sender"], 1)
self.assertTrue(all(item.timestamp.endswith("+08:00") for item in snapshot.messages))
```

**Step 2: 定义足够支持 Task 3 的 SourceSnapshot**

不能只返回 `Message`。最小结构还需要：

```python
@dataclass(frozen=True)
class ConversationRef:
    customer_key: str
    profile_id: str
    canonical_account_id: str
    raw_wechat_id: str          # 仅本次进程内存中使用，不持久化
    raw_wechat_id_hash: str

@dataclass(frozen=True)
class RoleEvidence:
    message_key: str
    source_kind: str
    profile_id: str
    evidence_type: str
    evidence_value: str

@dataclass
class SourceSnapshot:
    messages: list[Message]
    messages_by_customer: dict[str, list[Message]]
    conversations: dict[str, ConversationRef]
    role_evidence: dict[str, RoleEvidence]
    first_at: Optional[datetime]
    last_at: Optional[datetime]
    observed_until_by_profile: dict[str, Optional[datetime]]
    event_source_hash: str
    state_source_hash: str
    account_config_hash: str
    quarantine_counts: dict[str, int]
    consistency_state: str
```

`raw_wechat_id` 不得进入日志、SQLite、报告或模型 payload。

live-inbox 原文只在本次只读解析进程内存在；写入 M0 SQLite 的 `messages.text` 必须先经过 `redact_text()`。需要回看原文时按 `message_key/source_ordinal` 回到只读源，不在派生库复制第二份明文聊天。

`observed_until` 不得用“最后一条消息时间”代替。live-inbox 从只读 `state.json.accounts.<profile>.last_poll_at` 得到每个 profile 的观察边界，并要求该 profile `initialized=true` 且 `last_error` 为空；全局 `last_success_at` 只作交叉检查。某 profile 缺失、失败、时间无法解析或观察边界早于其消息时间时，该 profile 保存 `None` 和 quality flag，后续卡片只能是 `unobserved`。`state.event_count` 只是最近一次运行数量，不能当总游标。

当前采集器是批次运行，成功结束后顶层 `status=stopped` 可以是正常状态，不能单独判失败；必须结合 `last_success_at`、逐 profile `last_poll_at` 和 `last_error`。

Task 2 必须把每个会话的 `ConversationRef` 持久化到 `conversation_refs`，供独立的 Task 3 CLI 使用。双方统一使用：

```text
raw_wechat_id_hash = HMAC(secret, "raw-wechat-id", canonical_account_id, normalized_raw_wechat_id)
```

`normalized_raw_wechat_id` 使用 Unicode NFKC、去首尾空白、拒绝空值和控制字符，但不擅自大小写折叠。数据库只保存 hash，不保存 raw ID 明文。

**Step 3: 锁定只读稳定快照行为**

真实 `events.jsonl` 和 `state.json` 可能被 Syncthing 同时更新。适配器必须对两个文件分别：

1. 以只读方式打开源。
2. 记录读取前 `device/inode/size/mtime_ns`。
3. 只处理本次快照边界内的完整 JSONL 行。
4. 读取后重新检查同一文件，并将两份 stat/hash 分别写入 `source_snapshots`。
5. 文件被替换、截断或追加时，将本次派生结果丢弃并只读重试一次。
6. 第二次仍变化则返回 `source_changed_during_run`，不得发布数据库。

fixture 测试必须证明适配前后 size、mtime_ns 和 SHA-256 完全不变；真实源若被外部同步更新，报告必须写“外部变化导致本次未发布”，不能指控为程序写入。

**Step 4: 锁定角色、时间和幂等规则**

接受规则：

- `chat_type == private`
- `message_type == 文本`
- `text.strip()` 非空
- `event_id` 全局幂等
- `account_profile` 必须出现在本地账号配置
- `sender == configured self_sender` -> `studio`
- `sender == ""` -> `customer`
- 其他 sender -> `unknown_sender` 隔离

时间规则：

- `message_timestamp` 是 Unix epoch 秒真值。
- 转换为 `Asia/Shanghai` 且持久化为带 `+08:00` 的 ISO-8601。
- `message_time` 只作交叉检查。
- 两者偏差 `<=60` 秒接受；超过 60 秒隔离为 `timestamp_mismatch`。
- `observed_at` 只用于接收延迟审计，不能替代消息发生时间。

ID 规则：

```text
customer_key = HMAC(canonical_account_id, conversation_id)
message_key  = HMAC(event_id, normalized timestamp, content digest)
```

profile 名称只用于 live-inbox 路由；稳定客户 ID 必须使用内部 `canonical_account_id`，避免 profile 重命名导致 ID 变化。四个 profile 都必须有稳定的内部 canonical ID；Task 3 另行配置可选的绑定 CSV 账号别名。`aolai4` 不使用绑定账号别名，而使用资格门后的飞书确定性证据桥。

**Step 5: 补结构化语义 PII 脱敏测试**

`redact_text()` 必须整段替换以下字段，即使值中没有数字：

```text
详细地址：某学校
收货地址：某机构
所在地区：某园区
手机号码：虚构值
联系人：某某
```

blind card 中不得出现字段后的原值。测试之外不要把真实消息文本写入报告。

**Step 6: 实现适配器和 CLI 参数**

给 `build` 增加：

```text
--input-format auto|export|live-inbox
--accounts-config .wechat-cs/config/accounts.local.json
--state /Volumes/GPFS/wechat-live-inbox/state.json
```

`auto` 规则：

- 目录含 `conversation_index.json + messages.jsonl` -> 旧 export 适配器。
- 文件名为 `events.jsonl` -> live-inbox 适配器。
- live-inbox 默认自动读取同目录 `state.json`，也允许用 `--state` 显式指定；两者都只读。
- 两条路径最终返回同一规范快照接口，不复制后续分析逻辑。

真实账号配置只能放 `.wechat-cs/config/accounts.local.json`；仓库中的 example 只放虚构值。

**Step 7: 完成 200 条分层角色校准门禁**

按 profile、角色和历史/近期时间段分层抽取 200 条。M0-A 要求 200/200 正确；未知 sender 保持隔离，不通过“多数投票”猜角色。

**Step 8: 验证 Task 2**

Run:

```bash
python3 -m unittest tests.test_live_inbox -v
python3 -m unittest tests.test_live_inbox tests.test_schema_v2 tests.test_core -v
python3 -m wechat_cs build --help
```

Expected: PASS；帮助信息显示新参数且不打印账号名、raw ID 或文本。

**Step 9: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/live_inbox.py wechat_cs/build.py wechat_cs/core.py wechat_cs/__main__.py config/accounts.example.json DATA_FORMAT.md tests/fixtures/live_inbox tests/test_live_inbox.py
git commit -m "feat: add stable read-only live inbox adapter"
```

### Task 3: 建立账号范围 raw ID -> 全局 phone HMAC 身份桥

**Files:**
- Create: `wechat_cs/identity.py`
- Create: `tests/fixtures/identity/safe_phone_bindings.csv`
- Create: `tests/test_identity.py`
- Modify: `wechat_cs/build.py:344-490`
- Modify: `wechat_cs/store.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `DATA_FORMAT.md`

**Step 1: 写失败测试覆盖真实 CSV 契约**

fixture 使用 UTF-8 BOM 和中文表头，覆盖：

- 两个账号、不同 raw ID、同一手机号。
- 同一账号 + raw ID 对应两个手机号。
- 只有昵称、没有 raw ID。
- 未知账号。
- 非法手机号。
- `0.95` 和 `0.82` 两种置信度。

核心测试：

```python
def test_account_scoped_raw_id_and_global_phone_hmac(self):
    rows = load_binding_csv(FIXTURE, registry=REGISTRY, secret=SECRET)
    self.assertEqual(rows[("account-a", "raw-1")].state, "approved")
    self.assertEqual(rows[("account-a", "raw-1")].match_method, "account_raw_exact")
    self.assertEqual(
        rows[("account-a", "raw-1")].phone_hmac,
        rows[("account-b", "raw-9")].phone_hmac,
    )
```

**Step 2: 锁定身份批准规则**

M0 只允许：

```text
人工确认的 profile -> canonical account 映射
+ 同账号 raw_wechat_id 精确相等
+ 行置信度 >= 0.95
+ 该复合键只对应一个合法手机号
= approved
```

其他规则：

- `0.82` 全部进入 `review`，不得因文件名为“高置信”而自动批准。
- `(account, raw ID)` 对应多个手机号 -> `conflict`。
- raw ID 跨账号出现不同手机号 -> 禁止 global fallback；账号内 exact 可独立保留。
- 昵称、姓名和显示名不能单独批准。
- `aolai4` 只有名称含“下单客户”或“相册客户”才有订单资格；客户发送的唯一手机号精确命中飞书订单，或唯一单号只映射到一个手机号时才可批准。姓名/备注相似不得自动批准。
- 覆盖率不是放行指标；错连为零优先于多连接。

身份状态统一为 `approved|review|conflict|rejected`；“精确匹配”属于 `match_method=account_raw_exact`，不能同时被当成另一套 state。

`import-bindings` 不依赖 Task 2 进程内对象：它读取数据库里的 `conversation_refs`，按同一 `raw_wechat_id_hash` 公式匹配 CSV 行，再写 `conversation_links`。找不到 conversation ref 的绑定行只计入 unmatched 聚合，不自动创建客户。

**Step 3: 实现手机号规范化和全局 HMAC**

```python
def normalize_phone(value: str) -> Optional[str]: ...

def global_phone_hmac(secret: str, phone: str) -> str:
    normalized = normalize_phone(phone)
    if normalized is None:
        raise ValueError("invalid phone")
    return hmac_id(secret, "phone", normalized)
```

手机号明文只在函数局部内存中存在。数据库、日志、异常、CLI 输出和报告均不得保存手机号、raw ID、姓名或地址。

**Step 4: 添加幂等导入 CLI**

```text
python3 -m wechat_cs import-bindings \
  --db "$RUN_DB" \
  --bindings /read-only/path/高置信可直接使用表.csv \
  --accounts-config .wechat-cs/config/accounts.local.json
```

输出只能包含：source hash、总数、置信度分层数、各 state 数量、未知账号数和冲突数。

**Step 5: 验证 Task 3**

Run:

```bash
python3 -m unittest tests.test_identity -v
python3 -m unittest tests.test_identity tests.test_live_inbox tests.test_schema_v2 tests.test_core -v
```

Expected: PASS；序列化数据库检查不含虚构明文手机号和 raw ID；不同 HMAC 密钥导入明确失败。

**Step 6: M0-B 人工验收**

- 分层抽查 100 个 approved link，要求 100% 符合账号 + raw ID + 手机号规则。
- 全查低置信 `0.82`、复合键冲突和跨账号冲突。
- 单独确认 `aolai4` 的自动连接全部经过订单资格门，且多手机号、单号多手机号和证据冲突均被隔离。
- 如果账号映射尚未人工确认，Task 3 可以生成候选报告，但 M0-B 不放行。

**Step 7: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/identity.py wechat_cs/build.py wechat_cs/store.py wechat_cs/__main__.py DATA_FORMAT.md tests/fixtures/identity tests/test_identity.py
git commit -m "feat: add deterministic customer identity bridge"
```

### Task 4: 从 orders_live envelope 构建规范订单事实

**Files:**
- Create: `wechat_cs/orders.py`
- Create: `tests/fixtures/orders/orders_live.json`
- Create: `tests/test_orders.py`
- Modify: `wechat_cs/source_snapshot.py`
- Modify: `wechat_cs/store.py`
- Modify: `wechat_cs/__main__.py`
- Modify: `DATA_FORMAT.md`

**Step 1: 创建与真实文件形状一致的虚构 fixture**

```json
{
  "synced_at": "2026-07-13T12:00:00+08:00",
  "total_records": 11,
  "primary_records": 11,
  "extra_sources": [],
  "records": []
}
```

`records` 至少覆盖：正常付款、未付款但有厂家打款、取消、全退、部分退、退芋圆、换、补、其他、重复 tracking number、异常日期和退款额大于收入。

**Step 2: 写失败测试锁定顾客付款语义**

```python
def test_supplier_payment_never_creates_customer_purchase(self):
    row = fixture_order(pay_date="", revenue=None, pay_amount=199, is_paid=True)
    order = normalize_order(
        row,
        synced_at=datetime.fromisoformat("2026-07-13T12:00:00+08:00"),
    )
    self.assertIsNone(order.paid_on)
    self.assertIsNone(order.revenue_minor)
```

规则：

- 顾客付款严格要求有效 `pay_date` 且 `revenue > 0`。
- `pay_amount/pay_date_actual/is_paid` 永不参与顾客成交。
- `order_line_id = HMAC(source_namespace, record_id)`；source hash 不进入稳定 ID。
- tracking number 只作事实字段，绝不作唯一键。
- 缺失或重复 `record_id` 进入隔离，不能互相覆盖。

**Step 3: 写失败测试锁定退款和异常字段规范化**

Task 4 只保存规范事实和 quality flags，不提前计算 `retained_30d`。

```text
cancel        <- 取消
return        <- 退
return_taro   <- 退芋圆
exchange      <- 换
compensation  <- 补
other         <- 其他/无法识别
```

解析时使用 `Decimal`，持久化为整数最小货币单位 `revenue_minor/refund_amount_minor`，避免源浮点数产生精度噪声。以下情况必须添加 quality flag，相关结果留给 Task 6 输出 `NULL`：

- 1970 或无法解析的付款日。
- 晚于订单快照日的退款日。
- `return/return_taro` 缺退款日期。
- `return/return_taro` 缺退款金额。
- 退款额大于收款额。
- 售后状态仍未结束。

`exchange/compensation` 正常情况下可以没有退款日期和退款金额，不因此触发“退款字段缺失”；若它们同时出现非零退款，则按实际退款事实记录并校验完整性。

**Step 4: 实现稳定快照和事务导入**

读取 `orders_live.json` 时也使用 `read_stable_bytes()`。如果读取期间被 Dashboard 替换或修改，本次导入不发布并只读重试一次。

CLI：

```text
python3 -m wechat_cs import-orders \
  --db "$RUN_DB" \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json
```

每次导入先创建带时区 `synced_at` 的 `order_snapshots` 行，所有订单行通过 `order_snapshot_id` 归属该快照。完整解析、对账和质量统计通过后，才在一个事务内把新快照设为唯一 `active`；失败必须保留上一版 active 订单快照。Task 6 只能读取 active 快照及其精确 `synced_at`。

**Step 5: 验证 Task 4**

Run:

```bash
python3 -m unittest tests.test_orders -v
python3 -m unittest tests.test_orders tests.test_identity tests.test_schema_v2 -v
python3 -m unittest discover -s tests -v
```

Expected: PASS；接受数 + 隔离数等于 envelope 的 `records` 数；重复 tracking 行仍为独立订单。

**Step 6: M0-C 人工验收**

- 分层抽查 100 条：正常付款、未付款、取消、全退、部分退、退芋圆、换和异常。
- 全查当前少量的 `补` 与 `其他`。
- 顾客付款、退款类型和异常 quality flag 要求 100% 符合规则。
- 任意使用厂家打款字段判顾客成交都是一票否决。

**Step 7: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/orders.py wechat_cs/source_snapshot.py wechat_cs/store.py wechat_cs/__main__.py DATA_FORMAT.md tests/fixtures/orders tests/test_orders.py
git commit -m "feat: normalize customer payment and refund facts"
```

### Task 5: 从 turn/episode 生成盲决策卡和独立 observed action

**Files:**
- Create: `wechat_cs/cards.py`
- Create: `tests/test_cards.py`
- Modify: `wechat_cs/core.py:267-382`
- Modify: `wechat_cs/store.py`
- Modify: `wechat_cs/__main__.py`

**Step 1: 写失败测试锁定 episode 和决策边界**

规则：

- 同角色连续消息在 15 分钟内合并为一个 turn。
- turn 间隔 `<=24h` 属于同一 episode；`>24h` 开新 episode。
- inbound card 的 `as_of_at` 是客户 turn 结束时刻。
- proactive followup card 的边界位于客服主动触达之前。
- 边界排序使用 `(timestamp, source_ordinal, message_key)`，不能只比较秒级 timestamp。
- blind context 最多取当前 episode 最近 8 个 turn。
- proactive followup 因 `>24h` 已进入新 episode；它的 blind context 明确取“动作边界之前的上一 episode 尾部最多 8 个 turn”，不能得到空上下文，也不能包含主动触达本身。

测试必须覆盖同一秒内先后两条消息，证明后续 ordinal 的消息不会泄漏进卡片。

**Step 2: 写失败测试锁定 observed action 五态**

```text
immediate_reply    reply_delay_seconds <= 1800
delayed_reply      1800 < reply_delay_seconds <= 86400
no_reply           已完整观察 86400 秒仍无客服回复
proactive_followup 长间隔后客服主动触达
unobserved         快照尚未覆盖完整动作观察窗
```

精确 `reply_delay_seconds` 是事实；immediate/delayed 是带版本的规则派生。以后调整阈值时不能丢失原始延迟。

`no_reply` 只能在完整观察 24 小时后写入；最新客户消息离快照不足 24 小时必须是 `unobserved`。

动作观察边界按 card 所属 profile 使用 Task 2 的 `profile_observations.observed_until`。每张卡保存 `source_snapshot_id`、`action_window_end` 和实际 `observation_until` 以便复核。如果该 profile 的边界未知，禁止回退到“全库最后一条消息时间”；即使客户消息很早，也只能输出 `unobserved`。

**Step 3: 分开 blind payload 和 observed action**

数据库可保存两层：

- `blind_context_json`：决策时刻已经可见且完成脱敏的上下文。
- `observed_action_json`：之后真实发生的客服动作，只保存脱敏回复、message keys、精确延迟和观察状态。

模型输入只能通过 allowlist 函数生成：

```python
def to_blind_payload(card: DecisionCard) -> dict:
    return {
        "card_id": card.card_id,
        "card_type": card.card_type,
        "as_of_at": card.as_of_at,
        "context": card.blind_context,
    }
```

不得包含：

- `observed_action`
- `action_message_keys`
- `as_of_at` 之后的客户或客服消息
- 手机号、raw ID、姓名、地址
- 平台、订单、收入、付款、退款和 outcome

实际动作以后可以单独用于“真实客服采用了什么策略”的标注，但绝不能与模型推荐动作混为一个字段。

**Step 4: 实现稳定 card ID 和幂等重建**

```text
card_id = HMAC(card_rule_version, customer_key, card_type, trigger_message_keys)
```

card ID 不得包含后续回复、订单或 outcome。因此未来追加消息可以把 `unobserved` 更新为真实动作，但不能改变原卡 ID。

`stable_split()` 继续按 customer 分组，保证同一客户永不跨 train/validation/test。

**Step 5: 添加 CLI**

```text
python3 -m wechat_cs build-cards \
  --db "$RUN_DB" \
  --episode-gap-hours 24 \
  --action-window-hours 24 \
  --immediate-reply-minutes 30
```

重复运行必须幂等；Task 1 暂存的 card 人工状态只在稳定 card 重建后恢复；旧 outcome 不恢复。

**Step 6: 验证 Task 5**

Run:

```bash
python3 -m unittest tests.test_cards -v
python3 -m unittest tests.test_cards tests.test_live_inbox tests.test_core -v
```

Expected: PASS，重点包括：

- 30 分钟边界为 immediate。
- 超过 30 分钟且不超过 24 小时为 delayed。
- 观察满 24 小时才是 no_reply。
- 观察不足是 unobserved。
- 加入未来消息不改变既有 card ID。
- blind payload 不包含 observed action 和订单字段。

**Step 7: M0-D 决策卡人工抽查**

按历史/近期、账号、immediate/delayed/no_reply/unobserved 分层抽查 100 张：

- 未来消息泄漏：0。
- observed action 泄漏：0。
- 跨客户混入：0。
- 结构化 PII 泄漏：0。
- episode 或动作观察窗口边界错误：0。

任意一项出现错误都返工，不以“总体准确率较高”放行。

**Step 8: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/cards.py wechat_cs/core.py wechat_cs/store.py wechat_cs/__main__.py tests/test_cards.py
git commit -m "feat: build blind cards and observed actions"
```

### Task 6: 批量挂接三态 1/3/7/30 天订单结果

**Files:**
- Create: `wechat_cs/outcomes.py`
- Create: `tests/test_outcomes.py`
- Modify: `wechat_cs/store.py`
- Modify: `wechat_cs/__main__.py`

**Step 1: 写失败测试锁定批处理接口**

单卡函数无法知道同日是否有其他卡，也拿不到 approved identity link。使用批处理：

```python
def attach_outcomes(
    cards: Sequence[DecisionCard],
    conversation_links: Sequence[ConversationLink],
    orders: Sequence[CanonicalOrder],
    *,
    orders_observed_until: datetime,
) -> dict[str, CardOutcome]:
    ...
```

只有 `conversation_link.state == approved` 且 `conversation_order_eligibility` 为 `order_customer` 或 `album_customer` 才能连接订单。低置信、冲突、未映射和订单资格不足都返回 `identity_unverified`，结果字段为 `NULL`。函数按 `phone_hmac` 汇总全部 customer_key 和 cards，不能分别处理后让同一订单重复成为多个客户的成功样本。

**Step 2: 锁定全部付款窗口的三态语义**

窗口按上海时区日历日期计算：

```text
paid_1d:  card 当日到次日日终
paid_3d:  card 当日到第 3 日日终
paid_7d:  card 当日到第 7 日日终
```

规则：

- 窗口内已出现有效顾客付款 -> `True`。
- active 订单快照的带时区 `orders_observed_until` 已晚于对应窗口日终，且没有付款 -> `False`。
- 订单快照尚未覆盖窗口 -> `NULL`。
- card 之前的订单不算新付款。
- 同日付款可记 `paid=True`，但因订单只有日期而没有分钟，归因为 `attribution_state=ambiguous` 并添加 `same_day` flag。

测试必须覆盖第 0/1/3/7 天边界和快照未结束的情况。

**Step 3: 锁定 30 天留存、退款和售后语义**

30 天观察窗口从付款日期开始；只有 `orders_observed_until` 晚于第 30 天日终时，缺失事件才允许写 `False`。未付款时这些字段为不适用 `NULL`。

- `取消`：保留曾付款事实，`retained_30d=False`。
- `退/退芋圆`：全退为 false；部分退在事实完整时按净留存金额判断，同时保存 `refund_loss_ratio`。
- `return/return_taro` 的退款字段缺失、退款额异常或售后未结束 -> 相关字段 `NULL`；正常无退款的 exchange/compensation 不套用该缺失规则。
- `换`：`exchange_30d=True`；仅无退款且状态明确完成时可保留成交，否则 retained 为 `NULL`。
- `补`：`compensation_30d=True`，不直接判成交失败。
- `其他`：事实不完整时保持 `NULL`。
- 付款后未完整观察 30 天时，`retained_30d/aftersale_30d/exchange_30d/compensation_30d` 均不能写 false。

**Step 4: 锁定归因主状态与可并存 flags**

```text
attribution_state:
  none | associated | ambiguous | identity_unverified | quality_unknown

attribution_flags:
  same_day | multiple_cards | multiple_orders |
  shared_phone_multiple_conversations
```

flags 可以并存，因此“同日 + 多卡”不会被单一枚举覆盖。主状态优先级为：`identity_unverified` -> `quality_unknown` -> `ambiguous` -> `associated` -> `none`。同一 phone HMAC 跨多个 customer_key 时先把全部候选卡放进同一个归因组；只要一个订单对应多个候选卡，就添加 `multiple_cards`，跨会话时再添加 `shared_phone_multiple_conversations`，不得把一次付款重复计入多个成功样本。Skill 统计以后只允许 `attribution_state=associated` 且 flags 为空的样本自动进入主统计。

**Step 5: 锁定 blind payload 不被结果挂接污染**

```python
before = sha256_json([to_blind_payload(card) for card in cards])
attach_outcomes(cards, links, orders, orders_observed_until=ORDERS_OBSERVED_UNTIL)
after = sha256_json([to_blind_payload(card) for card in cards])
self.assertEqual(before, after)
```

outcome 只能写 `card_outcomes`，不能修改 card blind context。

**Step 6: 添加 CLI**

```text
python3 -m wechat_cs attach-outcomes \
  --db "$RUN_DB"
```

默认 `orders_observed_until` 必须来自 active 订单快照的带时区 `synced_at`；`--orders-observed-until` 只允许测试或明确历史回放时覆盖。CLI 只输出各窗口 `true/false/unknown` 数、退款/换/补计数和 attribution state/flags 数量，不输出客户明细。

**Step 7: 验证 Task 6**

Run:

```bash
python3 -m unittest tests.test_outcomes -v
python3 -m unittest tests.test_outcomes tests.test_orders tests.test_cards tests.test_identity -v
```

Expected: PASS；重复挂接幂等，blind payload hash 前后完全一致。

**Step 8: M0-D outcome 人工抽查**

分层抽查 100 个 outcome，覆盖：付款、不付款、取消、退、部分退、换、补、窗口不足、同日、多卡、多订单和身份未验证。付款日、金额、退款类型、三态结果和归因状态要求 100% 符合规则。

**Step 9: 提交检查点（仅 Git 可用时）**

```bash
git add wechat_cs/outcomes.py wechat_cs/store.py wechat_cs/__main__.py tests/test_outcomes.py
git commit -m "feat: attach tri-state order outcomes"
```

## 4. Task 1-6 共用集成夹具与端到端测试

**Files:**
- Create: `tests/fixtures/m0/accounts.json`
- Create: `tests/fixtures/m0/events.jsonl`
- Create: `tests/fixtures/m0/state.json`
- Create: `tests/fixtures/m0/safe_phone_bindings.csv`
- Create: `tests/fixtures/m0/orders_live.json`
- Create: `tests/test_m0_pipeline.py`
- Modify: `README.md`
- Modify: `DATA_FORMAT.md`

集成 fixture 至少包含：

| 客户 | 会话动作 | 身份 | 订单结果 | 核心断言 |
|---|---|---|---|---|
| A | 10 分钟回复；同日两张卡 | approved/account_raw_exact | 同日付款、换货无退款 | immediate；same_day + multiple_cards |
| B | 40 分钟回复 | approved/account_raw_exact | 第 3 天付款、部分退款 | delayed；paid_1d=false、paid_3d=true |
| C | profile 观察边界已满 24 小时且无回复 | approved/account_raw_exact | 只有厂家打款字段 | no_reply；不能算顾客付款 |
| D | 客户消息存在 | conflict | 存在手机号订单 | identity_unverified；结果均 NULL |
| E | profile 观察边界前 2 小时才发消息 | approved/account_raw_exact | 无订单 | unobserved；不能误判 no_reply |
| F | 第二账号、不同 raw ID | approved/account_raw_exact，与 A 同手机号 | 与 A 同一订单组 | phone HMAC 一致且 shared_phone flag，不重复计成功 |

端到端测试按真实 CLI 顺序执行两遍：

1. `init-m0-run`
2. `build --input-format live-inbox`
3. `import-bindings`
4. `import-orders`
5. `build-cards`
6. `attach-outcomes`
7. `validate-m0`
8. 在测试目录内 `publish-m0`
9. 再执行一遍，验证幂等和稳定 ID。

核心断言：

- fixture 中 events、state、账号配置、绑定 CSV 和订单 envelope 的 size、mtime_ns、SHA-256 不变。
- 数据库不含明文手机号、raw ID、姓名和地址。
- 所有 live 消息时间带 `+08:00`。
- blind card 不含 observed action、未来消息、订单、付款和退款。
- card/order/link IDs 重跑稳定。
- 人工审核状态在稳定 card 存在时保留。
- outcome 重新计算而不是从旧库恢复。
- 同一密钥可以连接；换密钥明确失败。
- 第二次运行不增加重复记录。
- 失败的订单导入不破坏上一版订单快照。

Run:

```bash
python3 -m unittest tests.test_m0_pipeline -v
python3 -m unittest discover -s tests -v
python3 -m py_compile wechat_cs/*.py
node --check wechat_cs/static/app.js
node --check dashboard_integration/wechat_cs_proxy.js
```

Expected: 全部 PASS；现有 25 个测试无回归。

## 5. 真实数据 shadow run

只有虚构 fixture 全部通过后，才允许针对真实源执行一次 shadow run。执行者必须人工确认四个 profile 的 canonical ID/self sender，以及三个可靠的绑定 CSV 账号别名。`aolai4` 的绑定账号别名保持空值，并通过独立飞书证据桥连接。

先执行 preflight；密钥缺失、少于 32 字符、仍为默认值、账号配置缺失或输出目录越界都必须停止：

```bash
export WECHAT_CS_HMAC_SECRET='<existing-stable-secret-at-least-32-chars>'

python3 -m wechat_cs validate-m0-config \
  --accounts-config .wechat-cs/config/accounts.local.json \
  --bindings '/Volumes/GPFS/Users/a1234/Desktop/Coding/Old/wechat-local-service-kit/out/accounts/customer-phone-binding/高置信可直接使用表.csv'

python3 -m wechat_cs init-m0-run --runs-dir .wechat-cs/runs
```

`init-m0-run` 返回 `run_id` 和 working DB。将返回值代入本次运行，所有分步命令只写 working DB：

```bash
RUN_ID='<run-id-returned-by-init-m0-run>'
RUN_DB=".wechat-cs/runs/${RUN_ID}/wechat_cs_m0.sqlite3"

python3 -m wechat_cs build \
  --input /Volumes/GPFS/wechat-live-inbox/events.jsonl \
  --input-format live-inbox \
  --state /Volumes/GPFS/wechat-live-inbox/state.json \
  --accounts-config .wechat-cs/config/accounts.local.json \
  --db "$RUN_DB"

python3 -m wechat_cs import-bindings \
  --db "$RUN_DB" \
  --bindings '/Volumes/GPFS/Users/a1234/Desktop/Coding/Old/wechat-local-service-kit/out/accounts/customer-phone-binding/高置信可直接使用表.csv' \
  --accounts-config .wechat-cs/config/accounts.local.json

python3 -m wechat_cs import-orders \
  --db "$RUN_DB" \
  --orders /Volumes/GPFS/Users/a1234/Desktop/dashboard/orders_live.json

python3 -m wechat_cs build-cards \
  --db "$RUN_DB" \
  --episode-gap-hours 24 \
  --action-window-hours 24 \
  --immediate-reply-minutes 30

python3 -m wechat_cs attach-outcomes \
  --db "$RUN_DB"

python3 -m wechat_cs validate-m0 --db "$RUN_DB"

python3 -m wechat_cs publish-m0 \
  --db "$RUN_DB" \
  --output .wechat-cs/data/wechat_cs_m0.sqlite3
```

shadow run 使用 active 订单快照中实际的带时区 `synced_at`，不能用当前系统日期或只有日期的值替代；只有历史回放才显式传 `--orders-observed-until`。

shadow run 只输出聚合报告：

```text
.wechat-cs/runs/<run_id>/m0_run.json
.wechat-cs/runs/<run_id>/m0_acceptance.md
```

报告包含 events/state/绑定/订单 source hashes、记录数、隔离数、身份 state、订单 quality flag、卡片 action state、outcome 三态和 attribution state/flags；不包含客户文本、手机号、raw ID 或订单明细。只有 `pipeline_runs.state=complete` 且报告通过时，`publish-m0` 才能原子替换正式 M0 数据库。

## 6. 一票停止与允许隔离

一票停止：

- 输出路径与任一只读源目录重合。
- 程序以写模式打开、移动、替换或删除源文件。
- 读取期间源变化但仍准备发布本次数据库。
- HMAC key fingerprint 不一致。
- 未确认账号映射却自动批准身份。
- `0.82` 绑定被自动批准。
- 厂家打款字段参与顾客成交。
- observed action、未来消息或订单结果进入 blind payload。
- 观察窗口不足却写入 false/no_reply。
- integrity check、foreign key check 或稳定 ID 测试失败。

允许隔离后继续：

- 非文本、非私聊、未知 sender。
- `aolai4` 资格不足、无确定性飞书证据或证据冲突。
- 低置信绑定、身份冲突和账号冲突。
- 缺手机号、异常日期、退款字段不完整。
- 同日、多卡、多订单归因歧义。
- 动作或付款观察窗口尚未结束。

## 7. M0 完成定义

Task 1-6 只有同时满足以下条件才算完成：

1. 全部单元测试、集成测试和现有回归测试通过。
2. 三类真实源均完成只读稳定快照，未被程序修改。
3. 200 条 live 角色校准达到 200/200。
4. 100 个 approved identity link 抽查 100% 正确，所有低置信与冲突均隔离。
5. 100 条订单规则抽查 100% 正确，厂家打款回退为 0。
6. 100 张决策卡中未来消息、observed action、跨客户和 PII 泄漏均为 0。
7. 100 个 outcome 的付款窗口、退款语义、三态和归因状态 100% 符合规则。
8. 相同快照和密钥重跑后稳定 ID、计数与 blind payload hash 一致。
9. 形成只含聚合数据的 M0 验收报告。
10. 用户确认 M0 真值链路后，才进入 20 卡双模型校准和 Task 7-9。

## 8. 预计执行顺序

```text
Task 1 -> M0 schema / guard
Task 2 -> M0-A review
Task 3 -> M0-B review
Task 4 -> M0-C review
Task 5 -> card leakage review
Task 6 -> M0-D review
integration fixture -> real shadow run -> user acceptance
```

建议使用子代理驱动方式执行：每个 Task 由独立实现代理完成，再由测试/审查代理检查，通过一个检查点后才进入下一个 Task。Task 3、Task 4 和 Task 5 的人工抽查结论属于业务真值，不能由实现代理自评后直接放行。
