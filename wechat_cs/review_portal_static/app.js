'use strict';

const state = { token: '', profiles: [], current: null, scores: {} };
const $ = selector => document.querySelector(selector);

const labels = {
  customer_value: '客户价值', summary: '结论', facts: '依据', best_contact_time: '最佳联系时间',
  items: '偏好明细', current_opportunity: '当前机会', natural_opening: '建议开场',
};
const stratumLabels = {
  complex_risk: '售后关怀', future_return_wait: '回访等待', high_frequency: '高频客户',
  high_value: '高价值客户', dormant_repeat: '沉睡复购', control: '普通对照',
};
const verdictLabels = { approved: '可以直接用', edited: '改完可以用', rejected: '不建议使用' };
const eventLabels = {
  brand_preference: '品牌偏好', product_preference: '商品偏好', aftersales: '售后信号',
  delayed_purchase: '延迟购买', price_hesitation: '价格犹豫', stock_wait: '等待到货',
  birthday_clue: '生日线索', relationship_signal: '关系信号',
  promotion_or_payday_wait: '活动 / 发薪等待', future_return: '未来回访', contact_refusal: '拒绝联系',
};
const scoreDefinitions = [
  ['fact_accuracy', '事实准确度', '历史事实有没有说错'],
  ['insight_usefulness', '洞察实用性', '能不能帮助销售判断'],
  ['sales_realism', '销售真实感', '像不像真实熟客销售'],
  ['timing_quality', '联系时机', '联系时间和理由是否合理'],
  ['evidence_quality', '证据质量', '结论是否有证据支撑'],
];

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
      'X-Review-Access-Code': state.token,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function formatDate(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

function renderValue(container, value, empty = '证据不足，暂不判断') {
  container.replaceChildren();
  if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) {
    container.append(element('p', 'empty-copy', empty)); return;
  }
  if (Array.isArray(value)) {
    const list = element('ul');
    value.forEach(item => list.append(element('li', '', compact(item))));
    container.append(list); return;
  }
  if (typeof value === 'object') {
    const summary = value.summary || value.best_contact_time;
    if (summary) container.append(element('p', '', compact(summary)));
    const rest = Object.entries(value).filter(([key]) => !['summary', 'best_contact_time'].includes(key));
    if (rest.length) {
      const list = element('ul');
      rest.forEach(([key, item]) => list.append(element('li', '', `${labels[key] || key}：${compact(item)}`)));
      container.append(list);
    }
    return;
  }
  container.append(element('p', '', compact(value)));
}

function compact(value) {
  if (value === null || value === undefined || value === '') return '未知';
  if (Array.isArray(value)) return value.map(compact).join('；');
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${labels[key] || key}：${compact(item)}`).join('；');
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function renderScoreFields() {
  const container = $('#scoreFields');
  scoreDefinitions.forEach(([name, label, hint]) => {
    const field = element('fieldset', 'score-field');
    const legend = element('legend');
    legend.append(element('b', '', label), element('small', '', hint));
    field.append(legend);
    const buttons = element('div', 'score-buttons');
    for (let value = 1; value <= 5; value += 1) {
      const button = element('button', '', String(value));
      button.type = 'button';
      button.dataset.score = name;
      button.dataset.value = String(value);
      button.setAttribute('aria-label', `${label} ${value} 分`);
      button.addEventListener('click', () => setScore(name, value));
      buttons.append(button);
    }
    field.append(buttons);
    container.append(field);
  });
}

function setScore(name, value) {
  state.scores[name] = Number(value);
  document.querySelectorAll(`[data-score="${name}"]`).forEach(button => {
    button.classList.toggle('selected', Number(button.dataset.value) === Number(value));
  });
}

function resetReviewForm() {
  state.scores = {};
  document.querySelectorAll('[data-score]').forEach(button => button.classList.remove('selected'));
  document.querySelectorAll('[name="verdict"]').forEach(input => { input.checked = false; });
  $('#openingCorrection').value = '';
  $('#factCorrection').value = '';
  $('#reviewNotes').value = '';
  $('#saveStatus').textContent = '';
}

function fillOwnReview(detail) {
  resetReviewForm();
  const reviewer = $('#reviewer').value.trim();
  const review = detail.reviews.find(item => item.reviewer === reviewer);
  if (!review) return;
  Object.entries(review.scores || {}).forEach(([name, value]) => setScore(name, value));
  const verdict = document.querySelector(`[name="verdict"][value="${review.verdict}"]`);
  if (verdict) verdict.checked = true;
  $('#openingCorrection').value = review.corrections?.natural_opening || '';
  $('#factCorrection').value = review.corrections?.fact_correction || '';
  $('#reviewNotes').value = review.notes || '';
}

async function login(event) {
  event.preventDefault();
  const code = $('#accessCode').value.trim();
  if (!code) return;
  state.token = code;
  $('#loginStatus').textContent = '正在验证…';
  try {
    await api('/api/summary');
    sessionStorage.setItem('reviewAccessCode', code);
    $('#loginGate').classList.add('hidden');
    $('#portal').classList.remove('hidden');
    await refreshAll();
  } catch (error) {
    state.token = '';
    $('#loginStatus').textContent = error.message;
  }
}

async function refreshAll() {
  const [summary] = await Promise.all([loadSummary(), loadProfiles(false)]);
  return summary;
}

async function loadSummary() {
  const summary = await api('/api/summary');
  $('#generatedCount').textContent = summary.generated;
  $('#reviewedCount').textContent = summary.reviewed;
  $('#reviewProgress').textContent = `${summary.reviewed} / ${summary.total} 已有审核`;
  $('#verdictCounts').textContent = `${summary.verdicts.approved || 0} / ${summary.verdicts.edited || 0} / ${summary.verdicts.rejected || 0}`;
  $('#progressBar').style.width = `${summary.total ? Math.round(summary.reviewed / summary.total * 100) : 0}%`;
  $('#runMeta').textContent = `数据截至 ${formatDate(summary.run.as_of_at)} · ${summary.run.model} · 仅用于人工验收`;
  return summary;
}

async function loadProfiles(keepSelection = true) {
  const params = new URLSearchParams();
  if ($('#stratumFilter').value) params.set('stratum', $('#stratumFilter').value);
  if ($('#statusFilter').value) params.set('status', $('#statusFilter').value);
  const payload = await api(`/api/profiles?${params}`);
  state.profiles = payload.items || [];
  renderProfileList();
  if (!keepSelection && state.profiles.length) await selectProfile(state.profiles[0].sales_profile_id);
}

function renderProfileList() {
  const list = $('#sampleList');
  list.replaceChildren();
  if (!state.profiles.length) {
    list.append(element('div', 'loading', '当前筛选下没有样本')); return;
  }
  state.profiles.forEach((profile, index) => {
    const button = element('button', 'sample-row');
    button.type = 'button';
    button.dataset.id = profile.sales_profile_id;
    if (state.current?.sales_profile_id === profile.sales_profile_id) button.classList.add('selected');
    const number = element('span', 'sample-number', String(index + 1).padStart(2, '0'));
    const copy = element('span', 'sample-copy');
    copy.append(element('b', '', profile.label), element('small', '', stratumLabels[profile.stratum] || '客户画像'));
    const status = profile.review_count ? verdictLabels[profile.latest_verdict] || '已审核' : '待审核';
    button.append(number, copy, element('em', profile.review_count ? `status ${profile.latest_verdict}` : 'status', status));
    button.addEventListener('click', () => selectProfile(profile.sales_profile_id));
    list.append(button);
  });
}

async function selectProfile(id) {
  $('#detailEmpty').classList.add('hidden');
  $('#detailContent').classList.remove('hidden');
  $('#detailContent').classList.add('loading-state');
  try {
    const detail = await api(`/api/profiles/${encodeURIComponent(id)}`);
    state.current = detail;
    renderDetail(detail);
    renderProfileList();
  } catch (error) {
    $('#saveStatus').textContent = error.message;
  } finally {
    $('#detailContent').classList.remove('loading-state');
  }
}

function renderDetail(detail) {
  const card = detail.card || {};
  $('#sampleStratum').textContent = stratumLabels[detail.stratum] || '客户画像';
  $('#sampleTitle').textContent = detail.label;
  $('#sampleMeta').textContent = `历史快照截至 ${formatDate(detail.as_of_at)} · ${detail.model} · 默认满库存`;
  renderValue($('#customerValue'), card.customer_value);
  renderValue($('#timeRhythm'), card.time_rhythm);
  renderValue($('#currentOpportunity'), card.current_opportunity);
  renderValue($('#naturalOpening'), card.natural_opening);
  renderValue($('#productPreferences'), card.product_preferences);
  renderValue($('#purchaseDrivers'), card.purchase_drivers);
  renderValue($('#historicalCommitments'), card.historical_commitments);
  renderValue($('#contactReason'), card.contact_reason);
  renderValue($('#risks'), card.risks);
  renderValue($('#unknowns'), card.unknowns);
  renderFacts(detail.facts || {});
  renderEvidence(detail.events || []);
  renderHistory(detail.reviews || []);
  fillOwnReview(detail);
}

function renderFacts(facts) {
  const container = $('#factSummary');
  container.replaceChildren();
  const entries = [
    ['价值等级', facts.value_level], ['历史有效订单', facts.historical_orders],
    ['历史消费', facts.historical_spend_yuan === null ? null : `¥${facts.historical_spend_yuan}`],
    ['距上次下单', facts.days_since_last_order === null ? null : `${facts.days_since_last_order} 天`],
    ['建议联系时间', facts.recommended_contact_window], ['回复延迟中位数', facts.median_reply_seconds === null ? null : `${facts.median_reply_seconds} 秒`],
    ['偏好商品', facts.preferred_products], ['偏好颜色', facts.preferred_colors],
    ['偏好尺码', facts.preferred_sizes], ['会员资料', facts.member_profile_matched ? '已匹配' : '未匹配'],
    ['库存口径', facts.inventory_assumption],
  ];
  entries.forEach(([name, value]) => {
    const item = element('div');
    item.append(element('span', '', name), element('b', '', compact(value)));
    container.append(item);
  });
}

function renderEvidence(events) {
  const list = $('#evidenceList');
  list.replaceChildren();
  const evidenceTotal = events.reduce((total, event) => total + (event.evidence?.length || 0), 0);
  $('#evidenceCount').textContent = `${events.length} 个事件 · ${evidenceTotal} 条引用`;
  events.forEach(event => {
    const article = element('article', 'evidence-item');
    const head = element('div');
    head.append(element('b', '', eventLabels[event.event_type] || event.event_type), element('span', '', event.confidence ? `${Math.round(event.confidence * 100)}%` : ''));
    article.append(head, element('p', '', event.summary || '已验证销售事件'));
    (event.evidence || []).forEach(item => {
      const quote = element('blockquote');
      quote.append(element('small', '', item.label), document.createTextNode(item.quote));
      article.append(quote);
    });
    list.append(article);
  });
}

function renderHistory(reviews) {
  const container = $('#reviewHistory');
  container.replaceChildren();
  if (!reviews.length) return;
  container.append(element('h4', '', '已有验收记录'));
  reviews.forEach(review => {
    const item = element('article');
    item.append(element('b', '', `${review.reviewer} · ${verdictLabels[review.verdict] || review.verdict}`));
    item.append(element('small', '', `${Object.values(review.scores).join(' / ')} · ${formatDate(review.updated_at)}`));
    if (review.notes) item.append(element('p', '', review.notes));
    container.append(item);
  });
}

async function saveReview(event) {
  event.preventDefault();
  if (!state.current) return;
  const reviewer = $('#reviewer').value.trim();
  const verdict = document.querySelector('[name="verdict"]:checked')?.value || '';
  if (!reviewer || scoreDefinitions.some(([name]) => !state.scores[name]) || !verdict) {
    $('#saveStatus').textContent = '请填写称呼、五项评分和总体结论'; return;
  }
  const corrections = {};
  if ($('#openingCorrection').value.trim()) corrections.natural_opening = $('#openingCorrection').value.trim();
  if ($('#factCorrection').value.trim()) corrections.fact_correction = $('#factCorrection').value.trim();
  if (verdict === 'edited' && !Object.keys(corrections).length) {
    $('#saveStatus').textContent = '选择“改完可以用”时，请填写具体修改建议'; return;
  }
  const button = $('#saveReview');
  button.disabled = true;
  $('#saveStatus').textContent = '正在保存…';
  try {
    await api(`/api/profiles/${encodeURIComponent(state.current.sales_profile_id)}/review`, {
      method: 'POST',
      body: JSON.stringify({
        card_version: state.current.card_version, verdict, scores: state.scores, corrections,
        notes: $('#reviewNotes').value.trim(), reviewer,
      }),
    });
    localStorage.setItem('reviewerName', reviewer);
    $('#saveStatus').textContent = '已保存，可以继续下一张';
    await Promise.all([loadSummary(), loadProfiles(true)]);
    await selectProfile(state.current.sales_profile_id);
  } catch (error) {
    $('#saveStatus').textContent = error.status === 409 ? '卡片已更新，请刷新后重新审核' : error.message;
  } finally {
    button.disabled = false;
  }
}

async function nextUnreviewed() {
  const currentIndex = state.profiles.findIndex(item => item.sales_profile_id === state.current?.sales_profile_id);
  const ordered = [...state.profiles.slice(currentIndex + 1), ...state.profiles.slice(0, currentIndex + 1)];
  const next = ordered.find(item => !item.review_count) || ordered[0];
  if (next) await selectProfile(next.sales_profile_id);
}

async function boot() {
  renderScoreFields();
  $('#reviewer').value = localStorage.getItem('reviewerName') || '';
  $('#loginForm').addEventListener('submit', login);
  $('#reviewForm').addEventListener('submit', saveReview);
  $('#stratumFilter').addEventListener('change', () => loadProfiles(false));
  $('#statusFilter').addEventListener('change', () => loadProfiles(false));
  $('#nextUnreviewed').addEventListener('click', nextUnreviewed);
  $('#reviewer').addEventListener('change', () => state.current && fillOwnReview(state.current));
  const saved = sessionStorage.getItem('reviewAccessCode') || '';
  if (saved) {
    $('#accessCode').value = saved;
    state.token = saved;
    try {
      await api('/api/summary');
      $('#loginGate').classList.add('hidden');
      $('#portal').classList.remove('hidden');
      await refreshAll();
    } catch (_) { state.token = ''; }
  }
}

document.addEventListener('DOMContentLoaded', boot);
