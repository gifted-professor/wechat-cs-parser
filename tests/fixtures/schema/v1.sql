PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE customers (
    customer_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    opportunity_score INTEGER NOT NULL,
    opportunity_level TEXT NOT NULL,
    aftersales_priority TEXT,
    summary TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    source_file TEXT NOT NULL
);

CREATE TABLE messages (
    message_key TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    role TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    text TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL
);

CREATE TABLE style_pairs (
    pair_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    trigger_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    context_json TEXT NOT NULL,
    intent_stage TEXT NOT NULL,
    risk_json TEXT NOT NULL,
    review_status TEXT NOT NULL,
    review_reasons_json TEXT NOT NULL,
    split TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL REFERENCES style_pairs(pair_id) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE role_calibration (
    calibration_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    message_key TEXT NOT NULL REFERENCES messages(message_key) ON DELETE CASCADE,
    source_status INTEGER NOT NULL,
    expected_role TEXT NOT NULL,
    reviewer_role TEXT,
    reviewed_at TEXT
);

CREATE TABLE drafts (
    draft_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    request_text TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    intent TEXT NOT NULL,
    needs_clarification INTEGER NOT NULL,
    needs_human INTEGER NOT NULL,
    risk_json TEXT NOT NULL,
    grounding_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    final_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE identity_bindings (
    binding_id TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    phone_hmac TEXT,
    masked_hint TEXT NOT NULL,
    state TEXT NOT NULL,
    evidence_message_keys_json TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE evidence (
    evidence_key TEXT PRIMARY KEY,
    customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    message_keys_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE build_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO customers VALUES (
    'customer_fixture','fixture','2026-07-01T10:00:00+08:00',50,'medium',NULL,
    'fixture summary','[]','[]','{}','fixture.jsonl'
);
INSERT INTO messages VALUES (
    'message_fixture','customer_fixture','customer','2026-07-01T10:00:00+08:00',
    '[已脱敏消息]','fixture.jsonl',1
);
INSERT INTO style_pairs VALUES (
    'pair_fixture','customer_fixture','虚构问题','虚构回复','[]','general','{}',
    'approved','["human-reviewed"]','train','2026-07-01T10:01:00+08:00'
);
INSERT INTO reviews VALUES (
    'review_fixture','pair_fixture','approved','[]','fixture-reviewer',
    '2026-07-01T11:00:00+08:00'
);
INSERT INTO role_calibration VALUES (
    'calibration_fixture','customer_fixture','message_fixture',3,'customer','customer',
    '2026-07-01T11:00:00+08:00'
);
INSERT INTO drafts VALUES (
    'draft_fixture','customer_fixture','虚构请求','虚构草稿','general',0,1,'[]','[]',
    'generated','2026-07-01T11:00:00+08:00'
);
INSERT INTO feedback VALUES (
    'feedback_fixture','draft_fixture','customer_fixture','accepted','虚构最终文本',
    '2026-07-01T11:01:00+08:00'
);
INSERT INTO build_meta VALUES ('schema_version','1','2026-07-01T00:00:00+08:00');
