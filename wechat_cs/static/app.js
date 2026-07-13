'use strict';

const isDashboard = location.pathname === '/wechat-cs' || location.pathname.startsWith('/wechat-cs/');
const apiRoot = isDashboard ? '/api/wechat-cs' : '/v1';
const state = {
  health: null,
  customers: [],
  aftersales: [],
  currentCustomer: null,
  currentDetail: null,
  currentDraft: null,
  opportunityLevel: '',
  sampleStatus: 'pending',
};

const viewMeta = {
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
  if (name === 'draft') syncDraftCustomer();
  if (name === 'samples') void loadReviewQueues();
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

async function sendFeedback(outcome) {
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
    $('#feedbackStatus').textContent = '已复制；请人工检查后发送。';
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
  $('#openDraftView').addEventListener('click', () => switchView('draft'));
  $('#draftForm').addEventListener('submit', submitDraft);
  $('#copyDraft').addEventListener('click', copyDraft);
  $$('[data-feedback]').forEach(button => button.addEventListener('click', () => void sendFeedback(button.dataset.feedback)));
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
  await loadHealth();
  await loadCustomers();
  void loadReviewQueues();
}

document.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  if (isDashboard) {
    $('#tokenForm').closest('.status-card').classList.add('hidden');
  } else {
    $('#apiToken').value = token();
  }
  void initialize();
});
