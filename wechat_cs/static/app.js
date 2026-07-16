'use strict';

const isDashboard = location.pathname === '/wechat-cs' || location.pathname.startsWith('/wechat-cs/');
const apiRoot = isDashboard ? '/api/wechat-cs' : '/v1';
const state = {
  health: null,
  actionQueue: null,
  currentAction: null,
  currentActionEdited: false,
  customers: [],
  aftersales: [],
  currentCustomer: null,
  currentDetail: null,
  currentDraft: null,
  opportunityLevel: '',
  sampleStatus: 'pending',
};

const viewMeta = {
  actions: ['今日客服行动队列', '今天先联系谁'],
  opportunities: ['今日优先', '客户机会'],
  aftersales: ['需要处理', '售后待办'],
  customer: ['客户上下文', '客户详情'],
  draft: ['人工确认', '手动起草'],
  samples: ['质量闸门', '样本审核'],
  system: ['运行情况', '系统状态'],
};

const $ = selector => document.querySelector(selector);
const $$ = selector => Array.from(document.querySelectorAll(selector));

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function token() {
  return sessionStorage.getItem('wechatCsToken') || '';
}

function errorMessage(payload, fallback = '请求失败，请稍后重试') {
  return payload?.error?.message || payload?.message || fallback;
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  const storedToken = token();
  if (storedToken && isDashboard) headers['X-WeChat-CS-Dashboard-Token'] = storedToken;
  if (storedToken && !isDashboard) headers.Authorization = `Bearer ${storedToken}`;
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(`${apiRoot}${path}`, { ...options, headers, cache: 'no-store' });
  } catch (error) {
    throw new Error('无法连接本地分析服务');
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const apiError = new Error(errorMessage(payload, `请求失败（${response.status}）`));
    apiError.status = response.status;
    apiError.payload = payload;
    throw apiError;
  }
  return payload;
}

async function fetchAll(path, maxItems = 1000) {
  const items = [];
  let offset = 0;
  let total = Infinity;
  while (offset < total && items.length < maxItems) {
    const separator = path.includes('?') ? '&' : '?';
    const payload = await api(`${path}${separator}limit=200&offset=${offset}`);
    items.push(...(payload.items || []));
    total = Number(payload.total || items.length);
    if (!payload.items?.length) break;
    offset += payload.items.length;
  }
  return { items: items.slice(0, maxItems), total };
}

function formatDate(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function showNotice(message, kind = 'warning') {
  const notice = $('#globalNotice');
  notice.textContent = message;
  notice.className = `notice ${kind}`;
}

function clearNotice() {
  $('#globalNotice').className = 'notice hidden';
  $('#globalNotice').textContent = '';
}

function setLoading(container, text = '正在读取…') {
  container.replaceChildren(node('div', 'loading-state', text));
}

function setEmpty(container, text) {
  container.replaceChildren(node('div', 'inline-empty', text));
}

function switchView(name) {
  if (!viewMeta[name]) return;
  $$('.view').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
  $$('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  $('#pageEyebrow').textContent = viewMeta[name][0];
  $('#pageTitle').textContent = viewMeta[name][1];
  clearNotice();
  if (name === 'actions' && !state.actionQueue) void loadActionQueue();
  if (name === 'draft') syncDraftCustomer();
  if (name === 'samples') void loadReviewQueues();
}

const laneMeta = {
  reply_now: { label: '立即回复', className: 'reply', list: '#replyNowList' },
  proactive_today: { label: '今日跟进', className: 'proactive', list: '#proactiveList' },
  suppressed: { label: '暂不联系', className: 'suppressed', list: '#suppressedList' },
};

const reasonLabels = {
  action_queue_unavailable: '行动队列服务不可用',
  empty_queue_status_unknown: '无法确认空队列的数据状态',
  collector_stopped: '聊天采集已停止',
  collector_unhealthy: '聊天采集状态异常',
  message_collection_unhealthy: '聊天采集状态异常',
  message_snapshot_stale: '消息快照超过 15 分钟',
  stale_message_snapshot: '消息快照超过 15 分钟',
  message_snapshot_missing: '缺少消息快照',
  order_snapshot_stale: '订单快照超过 24 小时',
  stale_order_snapshot: '订单快照超过 24 小时',
  recent_order: '客户刚下单，避免重复打扰',
  recently_ordered: '客户刚下单，避免重复打扰',
  aftersales_risk: '存在售后或争议，应先修复体验',
  aftersales_open: '存在未解决售后，应先修复体验',
  explicit_refusal: '客户已明确拒绝',
  explicit_rejection: '客户已明确拒绝',
  repeated_no_response: '连续联系后未回复',
  consecutive_no_reply: '连续联系后未回复',
  contact_cooldown: '仍在 7 天联系冷却期',
  proactive_cooldown: '仍在 7 天联系冷却期',
  identity_conflict: '客户身份存在冲突',
  insufficient_facts: '事实不足，暂不能给出建议',
  facts_insufficient: '事实不足，暂不能给出建议',
  required_facts_missing: '联系前所需事实尚未核实',
  phone_identity_missing: '尚未完成人工身份绑定',
  duplicate_phone_day: '同一客户今天已有行动',
  duplicate_phone_today: '同一客户今天已有行动',
  proactive_daily_limit: '已达到今日主动联系上限',
  daily_proactive_limit: '已达到今日主动联系上限',
  order_snapshot_stale_for_proactive: '订单快照过期，暂停主动跟进',
  not_actionable_today: '今天没有足够的联系理由',
  unresolved_inbound: '有未回复的客户消息',
  unanswered_inbound: '有未回复的客户消息',
  promised_followup: '已到约定跟进时间',
  repurchase_window: '进入历史复购窗口',
  repurchase_signal: '进入历史复购窗口',
  high_customer_value: '历史价值较高',
  customer_value_signal: '历史价值较高',
  active_intent: '近期意向较明确',
  positive_intent: '近期表达了积极意向',
  mixed_intent: '近期意向混合，需轻量确认',
  product_candidate_signal: '存在历史商品偏好候选',
  proactive_eligible: '进入人工跟进窗口',
  historical_snapshot_only: '基于历史快照，不代表实时状态',
  contact_precheck_required: '联系前必须人工核对最新状态',
};

const actionLabels = {
  reply_to_inbound: '回复客户当前问题',
  proactive_followup: '轻量跟进此前需求',
  follow_up_promise: '按约定时间回访',
  follow_up_as_promised: '按约定时间回访',
  restore_message_collection: '先恢复聊天采集',
  refresh_message_snapshot: '先刷新消息快照',
  resolve_identity: '先人工核对客户身份',
  route_to_human_aftersales: '先人工处理售后',
  verify_facts: '先核对动态事实',
  human_review: '交由人工判断',
  suppress_contact: '暂不联系',
  do_not_contact: '暂不联系',
};

const factLabels = {
  current_price: '当前价格',
  inventory: '实时库存',
  size_availability: '尺码可用情况',
  discount: '优惠',
  promotion: '活动',
  delivery_estimate: '发货或到货时效',
  order_status: '订单状态',
  aftersales_policy: '售后政策',
  policy: '当前政策',
  unverified_price: '不得承诺未经核实的价格',
  unverified_inventory: '不得承诺未经核实的库存',
  unverified_size: '不得承诺未经核实的尺码',
  unverified_discount: '不得承诺未经核实的优惠',
  unverified_policy: '不得承诺未经核实的政策',
  guaranteed_delivery: '不得保证发货或到货时效',
  guaranteed_outcome: '不得保证成交、退款或售后结果',
};

function readableCode(value, dictionary = {}) {
  const code = typeof value === 'string' ? value : value?.code || value?.reason || value?.label || '';
  if (!code) return '';
  return dictionary[code] || String(code).replaceAll('_', ' ');
}

function listValues(value) {
  return Array.isArray(value) ? value : [];
}

function shanghaiDateValue(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const fields = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}`;
}

function formatPercent(value) {
  if (typeof value === 'string' && ['high', 'medium', 'low'].includes(value)) {
    return ({ high: '高', medium: '中', low: '低' })[value];
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '待人工判断';
  return `${Math.round((numeric <= 1 ? numeric * 100 : numeric))}%`;
}

function formatContactWindow(windowValue) {
  if (typeof windowValue === 'string' && windowValue.trim()) return windowValue.trim();
  if (!windowValue || typeof windowValue !== 'object') return '工作时段人工选择';
  if (windowValue.label) return String(windowValue.label);
  if (windowValue.mode === 'none') return '暂停联系';
  if (windowValue.mode === 'as_soon_as_possible') return '尽快人工处理';
  if (windowValue.mode === 'work_hours_manual_choice') return '工作时段人工选择';
  if (windowValue.start_hour !== undefined && windowValue.end_hour !== undefined) {
    const range = `${String(windowValue.start_hour).padStart(2, '0')}:00–${String(windowValue.end_hour).padStart(2, '0')}:00`;
    return windowValue.mode === 'personal_history' ? `历史活跃时段 ${range}` : range;
  }
  if (windowValue.start && windowValue.end) return `${windowValue.start}–${windowValue.end}`;
  if (windowValue.window) return String(windowValue.window);
  return '工作时段人工选择';
}

function actionDisplayName(item) {
  return String(item?.display_name || '匿名客户');
}

function queueIsBlocked() {
  return state.actionQueue?.status === 'blocked';
}

function normalizeQueue(payload) {
  const lanes = payload?.lanes || {};
  const normalizedLanes = {
    reply_now: listValues(lanes.reply_now),
    proactive_today: listValues(lanes.proactive_today),
    suppressed: listValues(lanes.suppressed),
  };
  const allItems = Object.values(normalizedLanes).flat();
  const inferredFreshness = allItems.find(item => item?.freshness)?.freshness || {};
  const hardBlockCodes = new Set(['message_collection_unhealthy', 'message_snapshot_stale']);
  const inferredBlockReasons = [...new Set(normalizedLanes.suppressed.flatMap(item => listValues(item.reason_codes)).filter(code => hardBlockCodes.has(code)))];
  const explicitStatus = payload?.status || payload?.queue_status;
  let status = explicitStatus || 'ready';
  if (!explicitStatus && inferredBlockReasons.length && !normalizedLanes.reply_now.length && !normalizedLanes.proactive_today.length) {
    status = 'blocked';
  } else if (!explicitStatus && !allItems.length) {
    status = 'blocked';
    inferredBlockReasons.push('empty_queue_status_unknown');
  } else if (!explicitStatus && inferredFreshness?.orders?.state !== 'fresh') {
    status = 'degraded_order_data';
  }
  return {
    profile_id: payload?.profile_id || payload?.profile || $('#actionProfile').value,
    queue_date: payload?.queue_date || payload?.date || $('#actionDate').value,
    status,
    block_reasons: listValues(payload?.block_reasons).length ? listValues(payload?.block_reasons) : inferredBlockReasons,
    lane_restrictions: payload?.lane_restrictions && typeof payload.lane_restrictions === 'object' ? payload.lane_restrictions : {},
    freshness: payload?.freshness && typeof payload.freshness === 'object' ? payload.freshness : inferredFreshness,
    data_mode: payload?.data_mode || (status === 'historical_snapshot_ready' ? 'historical_snapshot' : 'current_snapshot'),
    snapshot_cutoff: payload?.snapshot_cutoff || payload?.freshness?.messages?.snapshot_at || null,
    realtime_reply_available: payload?.realtime_reply_available === true,
    contact_precheck_required: payload?.contact_precheck_required === true,
    counts: payload?.counts || {},
    lanes: normalizedLanes,
  };
}

function setActionListsLoading(text = '正在读取今日行动…') {
  Object.values(laneMeta).forEach(meta => setLoading($(meta.list), text));
}

function renderQueueStatus(queue) {
  const counts = {
    reply_now: Number(queue.counts?.reply_now ?? queue.lanes.reply_now.length),
    proactive_today: Number(queue.counts?.proactive_today ?? queue.lanes.proactive_today.length),
    suppressed: Number(queue.counts?.suppressed ?? queue.lanes.suppressed.length),
  };
  $('#replyNowCount').textContent = counts.reply_now;
  $('#proactiveCount').textContent = counts.proactive_today;
  $('#suppressedCount').textContent = counts.suppressed;
  $('#replyNowBadge').textContent = counts.reply_now;
  $('#proactiveBadge').textContent = counts.proactive_today;
  $('#suppressedBadge').textContent = counts.suppressed;
  $('#navActionCount').textContent = counts.reply_now + counts.proactive_today;

  const freshness = queue.freshness || {};
  const messageState = freshness.messages?.state;
  const orderState = freshness.orders?.state;
  const freshnessLabel = freshness.label
    || (queue.status === 'blocked'
      ? '不可用于行动'
      : queue.status === 'historical_snapshot_ready'
        ? '历史快照促单候选；实时回复已关闭'
      : orderState && orderState !== 'fresh'
        ? '消息可用；价值与商品建议已隐藏'
        : messageState === 'fresh' ? '消息与订单快照可用' : '可用于人工判断');
  $('#queueFreshness').textContent = freshnessLabel;
  const snapshotCutoff = queue.snapshot_cutoff || freshness.messages?.snapshot_at || freshness.as_of_at || freshness.message_snapshot_at;
  $('#queueGeneratedAt').textContent = snapshotCutoff
    ? `数据截至 ${formatDate(snapshotCutoff)}`
    : `队列日期 ${queue.queue_date}`;
  $('#queueOverview').classList.toggle('is-blocked', queue.status === 'blocked');

  const blocked = $('#queueBlocked');
  blocked.classList.toggle('hidden', queue.status !== 'blocked');
  const reasons = $('#queueBlockReasons');
  reasons.replaceChildren();
  if (queue.status === 'blocked') {
    const values = queue.block_reasons.length ? queue.block_reasons : ['collector_unhealthy'];
    values.forEach(value => reasons.append(node('span', '', readableCode(value, reasonLabels))));
    $('#queueBlockedSummary').textContent = '当前数据不能支持真实联系建议，所有候选已自动移入“暂不联系”。';
  }
}

function makeActionCard(item) {
  const button = node('button', `action-card action-${laneMeta[item.lane]?.className || 'suppressed'}`);
  button.type = 'button';
  button.dataset.actionId = item.action_id || '';
  const head = node('span', 'action-card-head');
  const identity = node('span', 'action-identity');
  const avatar = node('span', 'avatar small', actionDisplayName(item).slice(0, 1));
  const identityCopy = node('span');
  identityCopy.append(node('strong', '', actionDisplayName(item)));
  const privateMeta = [item.owner, item.account_label, item.contact_hint].filter(Boolean).join(' · ');
  identityCopy.append(node('small', '', privateMeta || '本地匿名客户'));
  identity.append(avatar, identityCopy);
  const priority = node('span', 'action-priority');
  priority.append(node('b', '', item.priority_score ?? '—'), node('small', '', '优先分'));
  head.append(identity, priority);

  const why = listValues(item.reason_codes).map(value => readableCode(value, reasonLabels)).filter(Boolean)[0];
  const body = node('span', 'action-card-body');
  body.append(node('span', 'action-window', '',));
  body.querySelector('.action-window').append(node('small', '', item.lane === 'suppressed' ? '暂停原因' : '建议时间'), node('strong', '', item.lane === 'suppressed' ? (why || '规则暂停') : formatContactWindow(item.contact_window)));
  body.append(node('span', 'action-reason', why || readableCode(item.recommended_action, actionLabels) || '等待人工判断'));
  const footer = node('span', 'action-card-footer');
  footer.append(node('span', `lane-chip ${laneMeta[item.lane]?.className || 'suppressed'}`, laneMeta[item.lane]?.label || '待处理'));
  if (item.contact_precheck_required) footer.append(node('span', 'muted-tag', '历史快照 · 联系前复核'));
  footer.append(node('span', 'detail-link', '查看详情 →'));
  button.append(head, body, footer);
  button.addEventListener('click', () => void selectAction(item));
  return button;
}

function renderActionLane(lane, items) {
  const container = $(laneMeta[lane].list);
  container.replaceChildren();
  if (!items.length) {
    const empty = lane === 'suppressed' ? '没有被安全规则暂停的客户。' : '当前没有需要处理的客户。';
    setEmpty(container, empty);
    return;
  }
  items.forEach(item => container.append(makeActionCard({ ...item, lane: item.lane || lane })));
}

function renderActionQueue(queue) {
  renderQueueStatus(queue);
  Object.keys(laneMeta).forEach(lane => renderActionLane(lane, queue.lanes[lane]));
  $('#actionDetailEmpty').classList.remove('hidden');
  $('#actionDetailContent').classList.add('hidden');
  state.currentAction = null;
}

function renderActionQueueFailure(error) {
  const queue = normalizeQueue({
    status: 'blocked',
    block_reasons: [error?.payload?.error?.code || 'action_queue_unavailable'],
    lanes: { reply_now: [], proactive_today: [], suppressed: [] },
    freshness: { label: '行动队列不可用' },
  });
  state.actionQueue = queue;
  renderActionQueue(queue);
  $('#queueBlockedSummary').textContent = `${error.message}。系统没有生成替代建议。`;
}

async function loadActionQueue() {
  const profile = $('#actionProfile').value || 'aolai1';
  const date = $('#actionDate').value || shanghaiDateValue();
  $('#actionDate').value = date;
  setActionListsLoading();
  try {
    const path = `/action-queue?profile=${encodeURIComponent(profile)}&date=${encodeURIComponent(date)}&limit=20`;
    const payload = await api(path);
    state.actionQueue = normalizeQueue(payload);
    renderActionQueue(state.actionQueue);
  } catch (error) {
    renderActionQueueFailure(error);
  }
}

function appendDetailSection(container, title, values, className = '') {
  const section = node('section', `detail-section ${className}`.trim());
  section.append(node('h3', '', title));
  const list = node('div', 'detail-tags');
  values.filter(Boolean).forEach(value => list.append(node('span', '', value)));
  if (!list.childElementCount) list.append(node('span', 'muted-detail', '无额外项目'));
  section.append(list);
  container.append(section);
}

function draftFromAction(item) {
  const draft = item?.draft && typeof item.draft === 'object' ? item.draft : {};
  return {
    ...draft,
    text: String(draft.text || draft.draft_text || item?.draft_text || ''),
  };
}

function humanStateLabel(value) {
  return ({ pending: '待人工确认', adopted: '已采纳', edited: '已修改采纳', rejected: '已拒绝' })[value] || '待人工确认';
}

function renderActionDetail(item) {
  state.currentAction = item;
  state.currentActionEdited = false;
  $$('.action-card').forEach(card => card.classList.toggle('selected', card.dataset.actionId === String(item.action_id || '')));
  $('#actionDetailEmpty').classList.add('hidden');
  const content = $('#actionDetailContent');
  content.classList.remove('hidden');
  content.replaceChildren();

  const header = node('div', 'detail-header');
  const identity = node('div', 'detail-identity');
  identity.append(node('span', 'avatar', actionDisplayName(item).slice(0, 1)));
  const identityText = node('div');
  identityText.append(node('p', 'eyebrow', laneMeta[item.lane]?.label || '行动详情'));
  identityText.append(node('h2', '', actionDisplayName(item)));
  const localDetails = [item.owner && `负责人 ${item.owner}`, item.account_label, item.contact_hint].filter(Boolean).join(' · ');
  identityText.append(node('p', '', localDetails || '本地匿名映射未配置'));
  identity.append(identityText);
  const stateBadge = node('span', `confirmation-state state-${item.human_confirmation_state || 'pending'}`, humanStateLabel(item.human_confirmation_state));
  header.append(identity, stateBadge);
  content.append(header);

  const scoreRow = node('div', 'detail-score-row');
  const actionName = item.lane === 'suppressed' ? '暂不联系' : readableCode(item.recommended_action, actionLabels) || '人工判断';
  [
    ['建议动作', actionName],
    ['联系时间', item.lane === 'suppressed' ? '暂停' : formatContactWindow(item.contact_window)],
    ['优先分', item.priority_score ?? '—'],
    ['建议置信度', formatPercent(item.confidence)],
  ].forEach(([label, value]) => {
    const cell = node('div');
    cell.append(node('small', '', label), node('strong', '', value));
    scoreRow.append(cell);
  });
  content.append(scoreRow);

  appendDetailSection(content, '为什么排在这里', listValues(item.reason_codes).map(value => readableCode(value, reasonLabels)), 'reason-detail');
  const required = listValues(item.required_facts).map(value => readableCode(value, factLabels));
  const missing = listValues(item.missing_facts).map(value => `待确认：${readableCode(value, factLabels)}`);
  if (item.contact_precheck_required) {
    missing.unshift('联系前人工核对：新消息、刚下单、售后、拒绝联系');
  }
  appendDetailSection(content, '联系前要核对', [...required, ...missing], 'fact-detail');
  appendDetailSection(content, '禁止承诺', listValues(item.prohibited_claims).map(value => readableCode(value, factLabels)), 'prohibited-detail');

  const freshness = item.freshness && typeof item.freshness === 'object' ? item.freshness : state.actionQueue?.freshness || {};
  const freshnessSection = node('section', 'detail-section freshness-detail');
  freshnessSection.append(node('h3', '', '数据与人工状态'));
  const freshnessCopy = node('p', '', freshness.label || (queueIsBlocked()
    ? '当前数据不可用于行动'
    : item.contact_precheck_required
      ? `历史快照候选；数据截至 ${formatDate(item.snapshot_cutoff || freshness.messages?.snapshot_at)}，联系前必须人工复核最新状态`
      : '数据通过当前规则检查'));
  freshnessSection.append(freshnessCopy, node('small', '', `策略 ${item.strategy_version || '规则版'} · 排序 ${item.priority_version || '规则版'}`));
  content.append(freshnessSection);

  const safetyViolation = item.send_allowed !== false || item.human_confirmation_required !== true;
  if (item.lane === 'suppressed' || queueIsBlocked() || safetyViolation) {
    const pause = node('div', 'detail-pause');
    pause.append(node('strong', '', safetyViolation ? '安全字段异常，已停止操作' : '这位客户当前不提供回复建议'));
    pause.append(node('p', '', safetyViolation ? '请检查本地服务版本和安全配置。' : '先处理上面的暂停原因；数据恢复后再由人工重新判断。'));
    content.append(pause);
    return;
  }

  renderActionDraft(content, item, draftFromAction(item));
}

function renderActionDraft(content, item, draft) {
  content.querySelector('.action-draft-box')?.remove();
  const box = node('section', 'action-draft-box');
  const head = node('div', 'action-draft-head');
  const title = node('div');
  title.append(node('p', 'eyebrow', draft.model_used ? 'Kimi 白名单内润色' : '规则骨架'));
  title.append(node('h3', '', '可审核的回复建议'));
  const generation = node('button', 'secondary-button compact-button', draft.text ? '重新生成建议' : '生成回复建议');
  generation.type = 'button';
  generation.addEventListener('click', () => void generateActionDraft(item, generation));
  head.append(title, generation);

  const textarea = node('textarea', 'action-draft-text');
  textarea.rows = 8;
  textarea.maxLength = 12000;
  textarea.readOnly = true;
  textarea.value = draft.text || '';
  textarea.placeholder = '点击“生成回复建议”，系统会先使用事实白名单；失败时退回规则骨架。';
  const requiredFacts = listValues(item.required_facts);
  let factInputs = null;
  if (requiredFacts.length) {
    factInputs = node('div', 'draft-fact-inputs');
    const factIntro = node('p', '', '如已从当前事实源逐项核实，可填写后再润色；留空时只使用规则骨架。不要填写手机号或聊天标识。');
    factInputs.append(factIntro);
    const grid = node('div');
    requiredFacts.forEach(codeValue => {
      const code = typeof codeValue === 'string' ? codeValue : codeValue?.code;
      if (!code) return;
      const label = node('label', '', readableCode(code, factLabels));
      const input = node('input');
      input.type = 'text';
      input.maxLength = 400;
      input.dataset.factCode = code;
      input.placeholder = '确认后填写；未确认请留空';
      label.append(input);
      grid.append(label);
    });
    factInputs.append(grid);
  }
  const actions = node('div', 'action-draft-actions');
  const copy = node('button', 'secondary-button', '复制建议');
  const edit = node('button', 'secondary-button', '编辑建议');
  const adopt = node('button', 'success-button', '采纳');
  const reject = node('button', 'ghost-danger', '拒绝');
  [copy, edit, adopt, reject].forEach(button => { button.type = 'button'; });
  copy.disabled = !draft.text;
  adopt.disabled = !draft.text;
  copy.addEventListener('click', () => void copyActionDraft(textarea, box));
  edit.addEventListener('click', () => {
    textarea.readOnly = false;
    textarea.classList.add('is-editing');
    textarea.focus();
    state.currentActionEdited = true;
    adopt.textContent = '采纳修改';
    setActionDraftStatus(box, '可以修改文字；采纳前仍需核对事实。');
  });
  adopt.addEventListener('click', () => void feedbackAction(item, state.currentActionEdited ? 'edited' : 'adopted', textarea.value, box));
  reject.addEventListener('click', () => void feedbackAction(item, 'rejected', '', box));
  actions.append(copy, edit, adopt, reject);
  const status = node('p', 'action-draft-status');
  box.append(head);
  if (factInputs) box.append(factInputs);
  box.append(textarea, actions, status);
  content.append(box);
}

function setActionDraftStatus(box, message, kind = '') {
  const status = box.querySelector('.action-draft-status');
  status.textContent = message;
  status.className = `action-draft-status ${kind}`.trim();
}

async function selectAction(queueItem) {
  renderActionDetail(queueItem);
  const content = $('#actionDetailContent');
  content.classList.add('is-loading');
  try {
    const payload = await api(`/action-queue/${encodeURIComponent(queueItem.action_id)}`);
    const detail = payload?.item && typeof payload.item === 'object' ? payload.item : payload;
    renderActionDetail({ ...queueItem, ...detail, lane: detail.lane || queueItem.lane });
  } catch (error) {
    showNotice(`详情读取失败：${error.message}`, 'error');
  } finally {
    content.classList.remove('is-loading');
  }
}

async function generateActionDraft(item, button) {
  button.disabled = true;
  button.textContent = '正在安全起草…';
  try {
    const facts = {};
    button.closest('.action-draft-box')?.querySelectorAll('[data-fact-code]').forEach(input => {
      const value = input.value.trim();
      if (value) facts[input.dataset.factCode] = value;
    });
    const payload = await api(`/action-queue/${encodeURIComponent(item.action_id)}/draft`, { method: 'POST', body: { facts } });
    const draft = payload?.draft && typeof payload.draft === 'object'
      ? payload.draft
      : { text: payload?.draft_text || payload?.text || '', mode: payload?.mode, model_used: payload?.model_used };
    const merged = {
      ...item,
      draft,
      human_confirmation_required: payload?.human_confirmation_required ?? item.human_confirmation_required,
      send_allowed: payload?.send_allowed ?? item.send_allowed,
    };
    renderActionDetail(merged);
  } catch (error) {
    setActionDraftStatus(button.closest('.action-draft-box'), `${error.message}；没有生成模拟建议。`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = '重新生成建议';
  }
}

async function copyActionDraft(textarea, box) {
  if (!textarea.value.trim()) return;
  try {
    await navigator.clipboard.writeText(textarea.value);
    setActionDraftStatus(box, '已复制，请人工核对后使用。', 'success');
  } catch (error) {
    textarea.focus();
    textarea.select();
    setActionDraftStatus(box, '浏览器未允许自动复制，已为你选中文字。');
  }
}

async function feedbackAction(item, outcome, text, box) {
  const body = { outcome };
  if (outcome === 'edited') body.final_text = String(text || '').trim();
  box.querySelectorAll('button').forEach(button => { button.disabled = true; });
  try {
    const payload = await api(`/action-queue/${encodeURIComponent(item.action_id)}/feedback`, { method: 'POST', body });
    const nextState = payload?.human_confirmation_state || outcome;
    const updated = { ...item, human_confirmation_state: nextState };
    renderActionDetail(updated);
    const nextBox = $('#actionDetailContent .action-draft-box');
    if (nextBox) setActionDraftStatus(nextBox, outcome === 'rejected' ? '已记录为拒绝。' : '人工反馈已保存到本机。', 'success');
  } catch (error) {
    box.querySelectorAll('button').forEach(button => { button.disabled = false; });
    setActionDraftStatus(box, error.message, 'error');
  }
}

function levelLabel(level) {
  return ({ high: '高机会', medium: '中机会', low: '低机会' })[level] || '机会';
}

function healthWarningLabel(code) {
  return ({
    default_hmac_secret: '仍在使用开发用 HMAC 密钥',
    weak_hmac_secret: 'HMAC 密钥强度不足',
    snapshot_stale: '聊天快照已超过 14 天',
    role_calibration_required: '角色校准尚未通过',
  })[code] || code;
}

function renderHealth() {
  const health = state.health;
  if (!health) return;
  const pill = $('#snapshotPill');
  pill.classList.toggle('danger', health.status === 'unavailable');
  pill.classList.toggle('warning', health.status === 'degraded');
  const label = health.status === 'ok' ? '本机数据正常' : health.status === 'degraded' ? '安全闸门未解除' : '本机数据不可用';
  pill.querySelector('b').textContent = label;
  $('#systemDataStatus').textContent = label;

  const details = $('#healthDetails');
  details.replaceChildren();
  details.parentElement.querySelector('.system-warnings')?.remove();
  const entries = [
    ['快照时间', health.snapshot_at ? formatDate(health.snapshot_at) : '未生成'],
    ['快照年龄', health.age_days === null ? '未知' : `${health.age_days} 天`],
    ['客户', health.counts?.customers ?? 0],
    ['严格文本', health.counts?.messages ?? 0],
    ['风格样本', health.counts?.style_pairs ?? 0],
    ['已审核通过', health.counts?.approved_style_pairs ?? 0],
    ['草稿 / 反馈', `${health.counts?.drafts ?? 0} / ${health.counts?.feedback ?? 0}`],
  ];
  entries.forEach(([term, value]) => {
    details.append(node('dt', '', term), node('dd', '', value));
  });
  if (health.warnings?.length) {
    const warnings = node('div', 'system-warnings');
    health.warnings.forEach(item => warnings.append(node('span', '', healthWarningLabel(item))));
    details.after(warnings);
  }
  const progress = health.role_calibration || {};
  $('#calibrationProgress').textContent = `${progress.reviewed || 0} / ${progress.total || 0}`;
  const percent = progress.total ? Math.round((progress.reviewed / progress.total) * 100) : 0;
  $('#calibrationProgressBar').style.width = `${percent}%`;
}

function makeCustomerRow(customer) {
  const fragment = $('#customerRowTemplate').content.cloneNode(true);
  const button = fragment.querySelector('.customer-row');
  button.dataset.customerKey = customer.customer_key;
  fragment.querySelector('.avatar').textContent = (customer.display_name || '客').slice(0, 1);
  fragment.querySelector('strong').textContent = customer.display_name || '未命名客户';
  fragment.querySelector('.summary').textContent = customer.summary || '暂无摘要';
  fragment.querySelector('.timestamp').textContent = formatDate(customer.last_active_at);
  const badge = fragment.querySelector('.priority-badge');
  badge.textContent = customer.aftersales_priority || levelLabel(customer.opportunity_level);
  badge.classList.add(customer.aftersales_priority ? `priority-${customer.aftersales_priority.toLowerCase()}` : `level-${customer.opportunity_level}`);
  fragment.querySelector('.customer-score b').textContent = customer.opportunity_score ?? 0;
  button.addEventListener('click', () => void selectCustomer(customer.customer_key));
  return fragment;
}

function renderCustomerLists() {
  const opportunities = $('#opportunityList');
  const visible = state.opportunityLevel
    ? state.customers.filter(item => item.opportunity_level === state.opportunityLevel)
    : state.customers;
  opportunities.replaceChildren();
  if (!visible.length) setEmpty(opportunities, '当前筛选条件下没有客户。');
  else visible.forEach(customer => opportunities.append(makeCustomerRow(customer)));

  const aftersales = $('#aftersalesList');
  aftersales.replaceChildren();
  if (!state.aftersales.length) setEmpty(aftersales, '目前没有未闭环售后。');
  else state.aftersales.forEach(customer => aftersales.append(makeCustomerRow(customer)));

  const levels = { high: 0, medium: 0, low: 0 };
  state.customers.forEach(customer => { levels[customer.opportunity_level] = (levels[customer.opportunity_level] || 0) + 1; });
  const cards = $$('#opportunityStats .stat-card');
  cards[0].querySelector('strong').textContent = levels.high;
  cards[0].querySelector('span').textContent = '优先人工查看';
  cards[1].querySelector('strong').textContent = levels.medium;
  cards[1].querySelector('span').textContent = '建议近期跟进';
  cards[2].querySelector('strong').textContent = state.customers.length;
  cards[2].querySelector('span').textContent = '可分析私聊客户';
  $('#navAftersalesCount').textContent = state.aftersales.length;
}

async function selectCustomer(customerKey) {
  switchView('customer');
  $('#customerEmpty').classList.add('hidden');
  $('#customerWorkspace').classList.remove('hidden');
  setLoading($('#conversationTimeline'), '正在读取客户上下文…');
  try {
    const detail = await api(`/customers/${encodeURIComponent(customerKey)}`);
    state.currentDetail = detail;
    state.currentCustomer = detail.customer;
    renderCustomerDetail(detail);
  } catch (error) {
    showNotice(error.message, 'error');
    $('#customerEmpty').classList.remove('hidden');
    $('#customerWorkspace').classList.add('hidden');
  }
}

function renderCustomerDetail(detail) {
  const customer = detail.customer;
  $('#customerAvatar').textContent = (customer.display_name || '客').slice(0, 1);
  $('#customerName').textContent = customer.display_name || '未命名客户';
  $('#customerLastActive').textContent = `最近活跃：${formatDate(customer.last_active_at)}`;
  $('#customerScore').textContent = customer.opportunity_score ?? 0;
  $('#customerSummary').textContent = customer.summary || '暂无摘要';

  const reasons = $('#customerReasons');
  reasons.replaceChildren();
  (customer.reasons || []).forEach(reason => {
    const text = typeof reason === 'string' ? reason : reason.reason || reason.label || JSON.stringify(reason);
    reasons.append(node('span', '', text));
  });
  if (!reasons.childElementCount) reasons.append(node('span', 'muted-tag', '暂无额外评分原因'));

  const timeline = $('#conversationTimeline');
  timeline.replaceChildren();
  (detail.messages || []).forEach(message => {
    const item = node('div', `message ${message.role}`);
    const meta = node('div', 'message-meta');
    meta.append(node('strong', '', message.role === 'studio' ? '工作室' : '客户'), node('time', '', formatDate(message.timestamp)));
    item.append(meta, node('p', '', message.text));
    timeline.append(item);
  });
  if (!timeline.childElementCount) setEmpty(timeline, '没有可显示的严格文本消息。');
  renderIdentityReview(detail.identity_candidates || []);
  syncDraftCustomer();
}

function renderIdentityReview(candidates) {
  const container = $('#identityReview');
  container.replaceChildren();
  if (!candidates.length) {
    container.classList.add('hidden');
    return;
  }
  container.classList.remove('hidden');
  container.append(node('h3', '', '客户身份候选（需人工复核）'));
  candidates.forEach(candidate => {
    const row = node('div', 'identity-row');
    const copy = node('div');
    copy.append(node('strong', '', candidate.masked_hint || '已脱敏候选'), node('small', '', `状态：${candidate.state}`));
    const actions = node('div', 'inline-actions');
    if (!['approved', 'rejected'].includes(candidate.state)) {
      const approve = node('button', 'success-button compact-button', '采用');
      const reject = node('button', 'ghost-danger compact-button', '拒绝');
      approve.addEventListener('click', () => void reviewIdentity(candidate.binding_id, 'approve'));
      reject.addEventListener('click', () => void reviewIdentity(candidate.binding_id, 'reject'));
      actions.append(approve, reject);
    }
    row.append(copy, actions);
    container.append(row);
  });
}

async function reviewIdentity(bindingId, action) {
  if (!state.currentCustomer) return;
  try {
    await api(`/customers/${encodeURIComponent(state.currentCustomer.customer_key)}/identity-binding`, {
      method: 'PATCH', body: { binding_id: bindingId, action },
    });
    await selectCustomer(state.currentCustomer.customer_key);
  } catch (error) {
    showNotice(error.message, 'error');
  }
}

function syncDraftCustomer() {
  const hasCustomer = Boolean(state.currentCustomer);
  $('#draftEmpty').classList.toggle('hidden', hasCustomer);
  $('#draftWorkspace').classList.toggle('hidden', !hasCustomer);
  if (hasCustomer) $('#draftCustomerName').textContent = state.currentCustomer.display_name || '未命名客户';
}

function renderDraftResult(result) {
  state.currentDraft = result;
  $('#draftResult').classList.remove('hidden');
  $('#draftText').value = result.draft_text || '';
  $('#draftIntent').textContent = result.intent || '待判断';
  $('#feedbackStatus').textContent = '';
  const warnings = $('#draftWarnings');
  warnings.replaceChildren();
  if (result.needs_human) warnings.append(node('span', 'warning-chip danger', '需要人工确认'));
  if (result.needs_clarification) warnings.append(node('span', 'warning-chip', '需要补充信息'));
  (result.risk_flags || []).forEach(flag => warnings.append(node('span', 'warning-chip', flag)));
  if (!warnings.childElementCount) warnings.append(node('span', 'warning-chip safe', '未发现额外风险标记'));
}

async function submitDraft(event) {
  event.preventDefault();
  if (!state.currentCustomer) return switchView('opportunities');
  const button = $('#generateDraft');
  button.disabled = true;
  button.textContent = '正在安全起草…';
  $('#draftResult').classList.add('hidden');
  try {
    const result = await api('/drafts', {
      method: 'POST',
      body: {
        customer_key: state.currentCustomer.customer_key,
        latest_message: $('#latestMessage').value.trim(),
      },
    });
    renderDraftResult(result);
  } catch (error) {
    const suffix = error.payload?.grounding_missing ? '；系统没有生成模拟回复。' : '';
    showNotice(`${error.message}${suffix}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = '生成回复建议';
  }
}

async function saveDraftFeedback(outcome) {
  if (!state.currentDraft) return;
  const body = { outcome };
  if (outcome === 'edited') body.final_text = $('#draftText').value.trim();
  try {
    await api(`/drafts/${encodeURIComponent(state.currentDraft.draft_id)}/feedback`, { method: 'POST', body });
    $('#feedbackStatus').textContent = outcome === 'rejected' ? '已记录为放弃。' : '反馈已安全保存到本地。';
    await loadHealth();
  } catch (error) {
    $('#feedbackStatus').textContent = error.message;
  }
}

async function copyDraft() {
  const text = $('#draftText').value;
  try {
    await navigator.clipboard.writeText(text);
    $('#feedbackStatus').textContent = '已复制；请人工检查后使用。';
  } catch (error) {
    $('#draftText').focus();
    $('#draftText').select();
    $('#feedbackStatus').textContent = '浏览器未允许自动复制，已选中文本。';
  }
}

async function loadCalibration() {
  const card = $('#calibrationCard');
  setLoading(card, '正在读取校准样本…');
  try {
    const payload = await api('/role-calibration?pending=true&limit=1');
    const progress = payload.progress || {};
    $('#calibrationProgress').textContent = `${progress.reviewed || 0} / ${progress.total || 0}`;
    $('#calibrationProgressBar').style.width = `${progress.total ? (progress.reviewed / progress.total) * 100 : 0}%`;
    card.replaceChildren();
    const sample = payload.items?.[0];
    if (!sample) {
      card.append(node('div', 'review-complete', progress.passed ? '角色校准已通过，可以继续审核风格样本。' : '全部样本已复核，但准确率闸门尚未通过。'));
      return;
    }
    card.append(node('p', 'calibration-speaker', sample.display_name || '匿名客户'));
    card.append(node('blockquote', '', sample.message_text));
    card.append(node('time', '', formatDate(sample.timestamp)));
    const actions = node('div', 'calibration-actions');
    const customer = node('button', 'secondary-button', '这是客户发的');
    const studio = node('button', 'primary-button', '这是工作室发的');
    customer.addEventListener('click', () => void reviewCalibration(sample.calibration_id, 'customer'));
    studio.addEventListener('click', () => void reviewCalibration(sample.calibration_id, 'studio'));
    actions.append(customer, studio);
    card.append(actions);
  } catch (error) {
    setEmpty(card, error.message);
  }
}

async function reviewCalibration(id, reviewerRole) {
  try {
    await api(`/role-calibration/${encodeURIComponent(id)}`, { method: 'PATCH', body: { reviewer_role: reviewerRole } });
    await Promise.all([loadCalibration(), loadHealth()]);
  } catch (error) {
    showNotice(error.message, 'error');
  }
}

async function loadSamples() {
  const list = $('#sampleList');
  setLoading(list, '正在读取风格样本…');
  try {
    const payload = await api(`/style-pairs?status=${encodeURIComponent(state.sampleStatus)}&limit=30`);
    list.replaceChildren();
    (payload.items || []).forEach(sample => list.append(makeSampleCard(sample)));
    if (!list.childElementCount) setEmpty(list, '这个状态下没有样本。');
    if (state.sampleStatus === 'pending') $('#navPendingCount').textContent = payload.total || 0;
  } catch (error) {
    setEmpty(list, error.message);
  }
}

function makeSampleCard(sample) {
  const fragment = $('#sampleTemplate').content.cloneNode(true);
  const card = fragment.querySelector('.sample-card');
  fragment.querySelector('.stage-badge').textContent = sample.intent_stage || 'general';
  fragment.querySelector('.split-badge').textContent = sample.split || 'unassigned';
  fragment.querySelector('time').textContent = formatDate(sample.created_at);
  fragment.querySelector('.trigger').textContent = sample.trigger_text || '';
  fragment.querySelector('.reply').textContent = sample.reply_text || '';
  const risks = fragment.querySelector('.risk-tags');
  const risk = sample.risk || {};
  const flags = Array.isArray(risk) ? risk : (risk.flags || []);
  if (risk.level) risks.append(node('span', `risk-${risk.level}`, `风险：${risk.level}`));
  flags.forEach(flag => risks.append(node('span', '', flag)));
  if (!risks.childElementCount) risks.append(node('span', 'risk-low', '低风险'));
  card.querySelectorAll('[data-review]').forEach(button => {
    button.addEventListener('click', () => void reviewSample(sample.pair_id, button.dataset.review, card));
  });
  return fragment;
}

async function reviewSample(pairId, reviewStatus, card) {
  const note = card.querySelector('.review-note').value.trim();
  card.classList.add('is-saving');
  try {
    await api(`/style-pairs/${encodeURIComponent(pairId)}`, {
      method: 'PATCH',
      body: { review_status: reviewStatus, review_reasons: note ? [note] : [], reviewer: 'dashboard' },
    });
    card.remove();
    await Promise.all([loadSamples(), loadHealth()]);
  } catch (error) {
    card.classList.remove('is-saving');
    showNotice(error.message, 'error');
  }
}

async function loadReviewQueues() {
  await Promise.all([loadCalibration(), loadSamples()]);
}

async function loadHealth() {
  try {
    state.health = await api('/health');
    renderHealth();
  } catch (error) {
    state.health = { status: 'unavailable', warnings: [], counts: {} };
    renderHealth();
    showNotice(error.message, 'error');
    if (error.status === 401) switchView('system');
  }
}

async function loadCustomers() {
  setLoading($('#opportunityList'));
  setLoading($('#aftersalesList'));
  try {
    const [customers, aftersales] = await Promise.all([
      fetchAll('/customer-insights', 1000),
      fetchAll('/customer-insights?aftersales=true', 1000),
    ]);
    state.customers = customers.items;
    state.aftersales = aftersales.items.sort((a, b) => String(a.aftersales_priority).localeCompare(String(b.aftersales_priority)) || b.opportunity_score - a.opportunity_score);
    renderCustomerLists();
  } catch (error) {
    setEmpty($('#opportunityList'), error.message);
    setEmpty($('#aftersalesList'), error.message);
  }
}

function bindEvents() {
  $$('.nav-item').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $$('[data-go-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.goView)));
  $$('[data-level]').forEach(button => button.addEventListener('click', () => {
    $$('[data-level]').forEach(item => item.classList.toggle('active', item === button));
    state.opportunityLevel = button.dataset.level;
    renderCustomerLists();
  }));
  $$('[data-sample-status]').forEach(button => button.addEventListener('click', () => {
    $$('[data-sample-status]').forEach(item => item.classList.toggle('active', item === button));
    state.sampleStatus = button.dataset.sampleStatus;
    void loadSamples();
  }));
  $('#actionQueueFilters').addEventListener('submit', event => {
    event.preventDefault();
    void loadActionQueue();
  });
  $('#openDraftView').addEventListener('click', () => switchView('draft'));
  $('#draftForm').addEventListener('submit', submitDraft);
  $('#copyDraft').addEventListener('click', copyDraft);
  $$('[data-feedback]').forEach(button => button.addEventListener('click', () => void saveDraftFeedback(button.dataset.feedback)));
  $('#tokenForm').addEventListener('submit', event => {
    event.preventDefault();
    const value = $('#apiToken').value.trim();
    if (value) sessionStorage.setItem('wechatCsToken', value);
    else sessionStorage.removeItem('wechatCsToken');
    void initialize();
  });
  $('#clearToken').addEventListener('click', () => {
    sessionStorage.removeItem('wechatCsToken');
    $('#apiToken').value = '';
    void initialize();
  });
}

async function initialize() {
  clearNotice();
  $('#actionDate').value = $('#actionDate').value || shanghaiDateValue();
  await Promise.all([loadHealth(), loadActionQueue()]);
  if (!isDashboard) {
    void loadCustomers();
    void loadReviewQueues();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  if (isDashboard) {
    $$('.nav-item').forEach(item => {
      if (!['actions', 'system'].includes(item.dataset.view)) item.classList.add('hidden');
    });
  }
  $('#apiToken').value = token();
  void initialize();
});
