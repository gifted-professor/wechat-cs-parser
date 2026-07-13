# Kimi 50 人销售画像试点实施计划

## 目标与边界

在固定历史截止点上，为 50 位分层客户生成可人工审核的“销售作战卡”。Kimi 读取入选客户的完整原始会话，并结合订单、会员生日资料和确定性行为统计，输出购买偏好、时间节律、历史承诺、联系时机和自然开场建议。

首版只做离线画像和人工验收：不训练模型、不扩到 3,712 人、不自动发送、不把模型结论写回 `customers.memory_json`。

固定首轮输入：

- 数据运行：`20260713T140730+0800-833c3257`
- 截止时间：`2026-07-13T20:14:37+08:00`
- 默认模型：`kimi-k2.7-code`（用户后续确认升级；保留旧 K2.6 run）

## 实施批次

### 1. 保护基线

- 以已通过 128 项测试的行动队列为本地检查点。
- 在 `feat/kimi-sales-profile-pilot` 独立 worktree 开发。
- 真实库先备份；迁移只做 additive，保留 M0-A/M0-B/M0-C 和既有人工反馈。

### 2. 补齐订单和会员事实

- SQLite 升级至 schema v4。
- `m0-order-v3` 新增 `ordered_at`、`paid_at`、`order_note`，保留 `paid_on` 兼容既有结果归因。
- 只读重新导入 `dashboard/orders_live.json`，使 SKU、工厂、品类、颜色、尺码和时间真正落库；旧订单快照转为 `superseded`。
- 新增版本化 `customer_aux_facts`，只读导入 `birthday_members.json` 的生日、偏好风格、期望礼物和门店，仅通过已批准、无冲突的手机号关系关联。
- 工厂不等同品牌；品牌和活动偏好必须保留明确证据来源。

### 3. 确定性节律和 50 人抽样

- `customer-features-v2` 增加客户发言小时、客户回复小时、回复延迟、下单/付款小时，以及上中下旬分布。
- 客户回复定义为客服消息后 7 天内的下一次客户回复；月度分桶为 1–10、11–20、21–月底。
- 少于 5 次观察或最高时段占比低于 40% 时标记“证据不足”。
- 候选必须身份关系已批准、无冲突，且至少有一笔有效付款。
- 按互斥顺序抽取：5 位复杂售后/拒绝、10 位未来回访或等待、10 位高频、10 位高客单/高消费、10 位沉睡复购、5 位普通对照。
- 用稳定 HMAC 排序解决并列，确保 50 人不重复、四个账号都有样本，且至少 5 位匹配会员生日资料。

### 4. Kimi 多阶段画像

- 从 `/Volumes/GPFS/wechat-live-inbox/events.jsonl` 只读重扫 50 位客户，以相同 HMAC 规则恢复归属、原始文本和 `message_key`，不改变脱敏 M0 消息表。
- 第一阶段提取带证据的销售事件；第二阶段把验证后的事件、确定性统计、订单和会员事实合成为作战卡。
- 事件必须引用真实 `message_key` 或 `order_line_id`；不存在、原文不匹配或数值不一致的证据直接拒绝。
- 作战卡固定包含：客户价值、商品偏好、时间节律、购买驱动力、历史承诺、当前机会、建议联系理由、自然开场、风险、未知项和证据。
- 超过 24,000 字或 300 条消息时按时间切片，不拆开同一会话轮次。
- 提取温度 `0`，综合温度 `0.2`，并发数 2；仅对 408、429、5xx 和网络超时最多重试 3 次。
- 幂等键为 `input_hash + model + prompt_version + schema_version`；`--resume` 只重跑失败客户。同一冻结名单切换模型时创建独立版本化 run，不覆盖旧结果。

### 5. 离线执行和页面验收

- `prepare-sales-profile-pilot` 冻结 50 人名单，不调用 Kimi。
- `run-sales-profile-pilot --resume` 离线生成画像，单人失败不影响其余客户。
- 新增 `sales_profile_runs`、`sales_profile_subjects`、`sales_profile_events`、`sales_profiles`、`sales_profile_reviews`。
- 工作台新增“50 人画像验收”，展示完整作战卡、可展开证据和固定评分控件；无发送按钮，并显示截止时间和“联系前核对最新状态”。

## 公开接口

- `GET /v1/sales-profile-pilot?run_id=latest&status=&stratum=&limit=&offset=`
- `GET /v1/sales-profile-pilot/{sales_profile_id}`
- `POST /v1/sales-profile-pilot/{sales_profile_id}/review`

审核请求固定为：

```json
{
  "card_version": "input-model-prompt-schema hash",
  "verdict": "approved|edited|rejected",
  "scores": {
    "fact_accuracy": 1,
    "insight_usefulness": 1,
    "sales_realism": 1,
    "timing_quality": 1,
    "evidence_quality": 1
  },
  "corrections": {},
  "notes": "",
  "reviewer": "operator_id"
}
```

同一审核人重复提交使用 UPSERT；旧 `card_version` 返回 409。Dashboard 代理只精确放行上述接口，不开放模型触发或发送接口。

## 验收门槛

- 验证 v3→v4、重复初始化、旧反馈、外键、数据库完整性和 M0 门禁不变。
- 验证订单商品字段、订单/付款时间和备注真正落库，未来数据不进入截止点画像。
- 验证分层数量、去重、账号覆盖、生日覆盖和稳定复跑。
- Mock Kimi 验证切片完整、伪造证据拒绝、重试分类、单客失败隔离、幂等和断点续跑。
- 验证 API 分页、审核 UPSERT、版本冲突、页面渲染、代理 allowlist 和无发送能力。
- 全量扩展的 Go 条件：50/50 完成人工审核；关键事实幻觉为 0；至少 80% 通过或修改后通过；事实准确度均值不低于 4.5，其余四项均值不低于 4.0。
- 未达门槛时只新建版本化试点并修订 Prompt、Schema 或数据事实，不覆盖旧结果。

## 运行前提

- 订单、生日和微信源始终只读。
- 生日只来自会员表或客户明确表达；品牌、活动、时间承诺无证据时显示“未知”。
- 当前环境未配置 `KIMI_API_KEY`；代码和 Mock 测试可先完成，真实 50 人运行在凭据配置前保持阻断。
