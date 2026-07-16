# 成交归因与复购方法论审核 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 8898 销售跟进工作台中增加独立的成交归因审核区，让人工复核主动咨询、价格阻力、报价后沉默、实际回复方式、成交与 90 天复购信号，同时不改动原 50 人画像审核结果，也不自动训练或发送消息。

**Architecture:** 工作台继续使用原画像 SQLite 保存 50 人开场审核；新增模块只读加载主项目产出的 `report.json` 和 `episode_samples.jsonl`，并只读查询对应归因快照数据库中的脱敏聊天上下文。新的人工结论写入归因目录下独立的 `manual_reviews.sqlite3`，通过新的 `/api/conversion/*` 接口提供摘要、列表、详情和审核保存，前端以独立页签呈现。

**Tech Stack:** Python 标准库 HTTP/SQLite/JSON、原生 HTML/CSS/JavaScript、`unittest`

---

### Task 1: 固化归因审核数据边界

**Files:**
- Create: `wechat_cs/conversion_review.py`
- Test: `tests/test_conversion_review.py`

**Step 1: Write the failing tests**

- 构造最小 `report.json`、`episode_samples.jsonl` 和只读聊天数据库。
- 断言只把 `eligible_for_sales_method=true` 的样本放进默认方法论审核队列。
- 断言价格阻力、报价后沉默、主动咨询和复购信号进入优先排序。
- 断言公共输出不包含 `customer_key`、原始购买事件编号或其他内部关联键。

**Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_conversion_review -v`

Expected: FAIL because `wechat_cs.conversion_review` does not exist.

**Step 3: Write minimal implementation**

- 严格校验归因目录文件、样本编号和可选标签。
- 提供摘要、筛选列表、单条详情和结束日前 7 天的聊天上下文。
- 所有聊天文本经过现有工作台同等级的手机号、身份证号和技术编号清洗。

**Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_conversion_review -v`

Expected: PASS.

### Task 2: 增加独立人工审核存储和接口

**Files:**
- Modify: `wechat_cs/conversion_review.py`
- Modify: `wechat_cs/review_portal.py`
- Modify: `tests/test_review_portal.py`

**Step 1: Write the failing API tests**

- `GET /api/conversion/summary` 返回全量规模、可审核数、已审核数和 `weights_trained=false`。
- `GET /api/conversion/samples` 支持待审核、正样本、负样本和信号筛选。
- `GET /api/conversion/samples/{episode_id}` 返回标签、近似聊天上下文和已有人工结论。
- `POST /api/conversion/samples/{episode_id}/review` 只接受 `approved`、`corrected`、`rejected`，修正结论必须填写修正标签或说明。
- 保存后原 `sales_profile_opening_reviews` 行数不变。

**Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_review_portal.ReviewPortalTests -v`

Expected: FAIL with missing `/api/conversion/*` routes.

**Step 3: Implement the API and storage**

- 新增 `--conversion-audit-dir` 和 `--conversion-db` 参数。
- 在归因目录创建独立 `manual_reviews.sqlite3`，按 `episode_id + reviewer_key` 幂等更新。
- API 继续沿用 Host/Origin、无发送能力和 no-store 安全边界。

**Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_review_portal.ReviewPortalTests -v`

Expected: PASS.

### Task 3: 在 8898 增加“成交方法论审核”页签

**Files:**
- Modify: `wechat_cs/review_portal_static/index.html`
- Modify: `wechat_cs/review_portal_static/app.js`
- Modify: `wechat_cs/review_portal_static/styles.css`
- Test: `tests/test_review_portal.py`

**Step 1: Add failing page-contract assertions**

- 页面包含“客户跟进审核”和“成交方法论审核”两个入口。
- 新页面明确展示“历史相关性，不代表因果”“未训练权重”“不会自动发消息”。

**Step 2: Implement the UI**

- 摘要区展示可审核样本、正/负样本、人工进度和训练门状态。
- 列表优先显示价格阻力、报价后沉默、客户主动咨询和复购样本。
- 详情区展示标签、近似聊天上下文和三档人工结论；修正后可用时允许改标签。

**Step 3: Run backend and static contract tests**

Run: `python -m unittest tests.test_conversion_review tests.test_review_portal -v`

Expected: PASS.

### Task 4: 全量验证并切换常驻服务

**Files:**
- Modify: `README.md`

**Step 1: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass; expected skips remain skips.

**Step 2: Start a temporary local server**

- 使用原 50 人数据库、7 月 15 日归因目录和归因快照数据库启动临时端口。
- 实测摘要、列表、详情和保存接口，确认旧审核计数未改变。

**Step 3: Visually verify the page**

- 检查两个页签、筛选、聊天上下文、审核保存、窄屏布局和错误状态。

**Step 4: Restart port 8898 with the new read-only sources**

- 保持原 `--db` 与 `--run-id` 不变。
- 追加 `--conversion-audit-dir` 和 `--conversion-db`。
- 实测 `http://100.84.194.46:8898/` 与新接口。

**Step 5: Document the exact launch command and boundaries**

- README 记录两个数据源、独立人工审核库、无自动训练和无发送能力。

### Task 5: 把延期意向落成准备充分的人工回访

**Decision:** 采用人工回访计划，不采用只显示一句提醒，也不采用自动定时触达。

- 成交方法论审核从客户聊天中识别“过几天再问、两天后再看、下周再联系”等未来时间钩子。
- 审核人明确标注后续动作，并记录回访前要准备的活动、价格、商品、库存和上次顾虑。
- 客户跟进审核保存回访日期、任务状态、延期原因和准备事项；同一客户继续幂等更新同一条人工审核记录。
- `scheduled` 必须有合法日期；到期、逾期、完成和取消只作为人工状态，不触发发送、日历或模型训练。
- 旧审核表通过加列迁移保留原记录，归因人工审核库继续与 50 人画像审核表隔离。

### Task 6: 超过一年未付款的客户不进入主动促单

- 最近一次有效付款距冻结数据截止日超过 `365` 天时，固定扣 `60` 分并进入 `excluded`。
- 排除原因和列表标签明确显示“超过一年，不跟进”，默认可促销列表不再出现这些客户。
- 恰好 `365` 天仍允许进入后续门槛；无有效付款、拒绝联系和高售后规则继续叠加显示原因。
- 该门槛仅约束主动促单，不改变新入站消息的回复链路，不删除画像、订单或人工审核。
