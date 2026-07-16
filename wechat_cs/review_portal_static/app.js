'use strict';

const state = {
  profiles: [],
  current: null,
  ordersExpanded: false,
  messages: [],
  messagesLoaded: false,
  messagesLoading: false,
  messagesHasMore: false,
  messagesNextCursor: '',
  messagesTotal: 0,
  messagesSnapshotAt: '',
  messageRequestToken: 0,
  selectedEvidenceRef: '',
  workspace: 'followup',
  conversionLoaded: false,
  conversionSamples: [],
  currentConversion: null,
};

const $ = selector => document.querySelector(selector);

const stratumLabels = {
  complex_risk: '售后关怀',
  future_return_wait: '回访等待',
  high_frequency: '高频客户',
  high_value: '高价值客户',
  dormant_repeat: '沉睡复购',
  control: '普通客户',
};

const verdictLabels = {
  approved: '可以直接用',
  edited: '改完可以用',
  rejected: '不建议使用',
};

const priorityAssessmentLabels = {
  accurate: '基本准确',
  too_high: '优先级太高',
  too_low: '优先级太低',
  not_suitable: '暂不适合促单',
};

const priorityReasonLabels = {
  clear_intent: '清晰意向',
  repurchase_potential: '复购潜力',
  no_recent_need: '近期无需求',
  refuses_marketing: '拒绝营销',
  unresolved_aftersales: '售后未解决',
  recently_purchased: '刚购买',
  price_resistance: '价格阻力',
  purchased_elsewhere: '已在别处购买',
  insufficient_chat_signal: '聊天信号不足',
  other: '其他',
};

const methodLabels = {
  customer_initiated: '客户主动咨询',
  studio_initiated: '我们主动跟进',
  sales_inquiry: '销售咨询',
  general_or_unknown: '一般沟通 / 意图不明确',
  none: '无',
  explicit_price_objection: '明确表示价格贵',
  promotion_wait: '等待活动或降价',
  discount_request: '询问优惠',
  quote_then_silence_suspected: '报价后沉默（疑似）',
  price_quote: '报价',
  promotion_offer: '活动优惠',
  product_recommendation: '商品推荐',
  trust_proof: '信任证明',
  scarcity_or_urgency: '稀缺 / 紧迫',
  question_or_clarification: '提问澄清',
  other_observed_reply: '其他已观察回复',
  no_observed_reply: '未观察到回复',
  converted_7d: '7 天内成交',
  non_converted_7d: '7 天未成交',
  approved: '可纳入方法论',
  corrected: '修正后纳入',
  rejected: '排除样本',
};

const fieldLabels = {
  summary: '',
  reason: '原因',
  recommendation: '建议',
  best_contact_time: '适合联系时间',
  preferred_products: '偏好商品',
  preferred_colors: '偏好颜色',
  preferred_sizes: '偏好尺码',
  items: '',
  facts: '历史依据',
};

const valueLabels = {
  high: '高',
  medium: '中',
  low: '低',
  supported: '有历史记录支持',
  insufficient: '历史记录不足',
  unknown: '暂不确定',
  true: '是',
  false: '否',
  return_taro: '退芋圆',
  return: '退货退款',
  exchange: '换货',
  compensation: '补偿处理',
  cancel: '订单取消（不计售后）',
};

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
}

function setText(selector, value, fallback = '—') {
  const node = $(selector);
  if (node) node.textContent = value === null || value === undefined || value === '' ? fallback : String(value);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
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

function formatDate(value, withTime = false) {
  if (!value) return '时间待补全';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return humanize(value);
  const options = { year: 'numeric', month: 'long', day: 'numeric' };
  if (withTime) Object.assign(options, { hour: '2-digit', minute: '2-digit' });
  return new Intl.DateTimeFormat('zh-CN', options).format(date);
}

function formatChatTime(value) {
  if (!value) return '时间待补全';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return humanize(value);
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function money(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '待补全';
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: amount % 1 ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(amount);
}

function humanize(value) {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'boolean') return value ? '是' : '否';
  const raw = String(value).trim();
  if (!raw) return '';
  const exact = valueLabels[raw.toLowerCase()];
  if (exact) return exact;
  return raw
    .replace(/消息快照/g, '聊天记录')
    .replace(/日期占位/g, '日期记录')
    .replace(/\bRFM\b/gi, '历史购买表现')
    .replace(/\bFrequency\b/gi, '购买次数')
    .replace(/\bRecency\b/gi, '距上次购买')
    .replace(/\bMonetary\b/gi, '累计消费')
    .replace(/quality_flags/gi, '需要核对的数据')
    .replace(/return_taro/gi, '退芋圆')
    .replace(/\bcancel\b/gi, '订单取消（不计售后）')
    .replace(/\bcompensation\b/gi, '补偿处理')
    .replace(/\bexchange\b/gi, '换货')
    .replace(/\breturn\b/gi, '退货退款');
}

function isTechnicalKey(key) {
  return /(?:^|_)(?:id|ids|version|confidence|score|count|rate|ratio|state|rule|evidence|source|hash|raw|minor|seconds|bucket|rank|type|model|field|value|index)(?:_|$)/i.test(key)
    || ['quality_flags', 'parameters', 'metadata'].includes(key);
}

function narrativeLines(value, depth = 0) {
  if (depth > 5 || value === null || value === undefined || value === '') return [];
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    const copy = humanize(value);
    return copy ? [copy] : [];
  }
  if (Array.isArray(value)) return value.flatMap(item => narrativeLines(item, depth + 1));
  if (typeof value !== 'object') return [];

  const entries = Object.entries(value);
  const preferred = ['summary', 'reason', 'recommendation', 'best_contact_time', 'items', 'facts'];
  const ordered = [
    ...preferred.flatMap(key => entries.filter(([entryKey]) => entryKey === key)),
    ...entries.filter(([key]) => !preferred.includes(key)),
  ];
  return ordered.flatMap(([key, item]) => {
    if (isTechnicalKey(key)) return [];
    const lines = narrativeLines(item, depth + 1);
    const label = fieldLabels[key];
    if (!label || !lines.length) return lines;
    return [`${label}：${lines.join('；')}`];
  });
}

function renderNarrative(container, value, empty = '历史依据不足，暂不判断') {
  container.replaceChildren();
  const lines = [...new Set(narrativeLines(value).map(line => line.trim()).filter(Boolean))];
  if (!lines.length) {
    container.append(element('p', 'empty-copy', empty));
    return;
  }
  if (lines.length === 1) {
    container.append(element('p', '', lines[0]));
    return;
  }
  const list = element('ul');
  lines.forEach(line => list.append(element('li', '', line)));
  container.append(list);
}

function renderOpeningCaution(opening) {
  const warning = $('#openingCaution');
  const text = narrativeLines(opening).join(' ');
  const asksAboutReceipt = /顺利收到|收到.{0,8}[吗嘛？?]|还需要跟进/.test(text);
  warning.classList.toggle('hidden', !asksAboutReceipt);
  warning.textContent = asksAboutReceipt
    ? '开场提醒：页面没有显示“未解决售后”时，不要把“是否顺利收到”当作默认寒暄。直接从客户偏好、搭配机会或明确需求切入。'
    : '';
}

function renderCrossSell(data) {
  const card = $('#crossSellCard');
  const recommendations = Array.isArray(data.recommendations) ? data.recommendations : [];
  card.classList.toggle('hidden', !data.available);
  if (!data.available) return;
  setText('#crossSellTitle', `买过「${data.anchor_product || '同类商品'}」的客户还买了`);
  setText(
    '#crossSellSample',
    `${data.buyer_count || 0} 位买家 · 其中 ${data.other_buyer_count || 0} 位可比较`,
  );
  const container = $('#crossSellRecommendations');
  container.replaceChildren();
  if (!recommendations.length) {
    container.append(element('p', 'empty-copy', '暂时没有足够的共同购买记录；可填写人工搭配建议，但不要写成数据结论。'));
  } else {
    recommendations.forEach(item => {
      const entry = element('article');
      const support = Number(item.supporting_buyers) || 0;
      entry.append(
        element('b', '', item.product || '商品待确认'),
        element('small', '', `${support} 位其他买家买过${support < 2 ? ' · 样本较小' : ''}`),
      );
      container.append(entry);
    });
  }
  setText('#crossSellNote', data.method_note, '只展示冻结订单中的真实共同购买记录。');
}

function showOpeningEditor({ focus = true } = {}) {
  const editor = $('#openingEditor');
  editor.classList.remove('hidden');
  const textarea = $('#suggestedOpening');
  if (focus) textarea.focus({ preventScroll: false });
}

function syncPriorityReasonFields({ clear = false } = {}) {
  const assessment = document.querySelector('[name="priority_assessment"]:checked')?.value || '';
  const needsReason = ['too_high', 'too_low', 'not_suitable'].includes(assessment);
  $('#priorityReasonFields').classList.toggle('hidden', !needsReason);
  $('#priorityReasonCode').required = needsReason;
  if (!needsReason && clear) {
    $('#priorityReasonCode').value = '';
    $('#priorityNote').value = '';
    setText('#priorityNoteCount', '0');
  }
}

function renderSelectedEvidenceSummary() {
  const summary = $('#selectedEvidenceSummary');
  summary.replaceChildren();
  if (!state.selectedEvidenceRef) {
    summary.classList.add('hidden');
    return;
  }
  const selected = state.messages.find(item => item.message_ref === state.selectedEvidenceRef);
  const copy = selected
    ? `已选择 ${formatChatTime(selected.timestamp)} 的客户消息作为判断依据。`
    : '已选择一条客户消息作为判断依据；展开聊天后可以查看或取消。';
  summary.append(element('span', '', copy));
  const cancel = element('button', '', '取消选择');
  cancel.type = 'button';
  cancel.addEventListener('click', () => selectEvidenceMessage(''));
  summary.append(cancel);
  summary.classList.remove('hidden');
}

function resetReviewForm() {
  document.querySelectorAll('[name="verdict"]').forEach(input => { input.checked = false; });
  document.querySelectorAll('[name="priority_assessment"]').forEach(input => { input.checked = false; });
  $('#suggestedOpening').value = '';
  $('#openingEditor').classList.add('hidden');
  $('#priorityReasonCode').value = '';
  $('#priorityNote').value = '';
  $('#revisionNotes').value = '';
  setText('#priorityNoteCount', '0');
  setText('#revisionNotesCount', '0');
  $('#priorityReasonFields').classList.add('hidden');
  $('#priorityReasonCode').required = false;
  state.selectedEvidenceRef = '';
  renderSelectedEvidenceSummary();
  setText('#saveStatus', '', '');
}

function fillOwnReview(detail) {
  resetReviewForm();
  const review = (detail.reviews || [])[0];
  if (!review) return;
  const verdict = document.querySelector(`[name="verdict"][value="${review.verdict}"]`);
  if (verdict) verdict.checked = true;
  if (review.suggested_opening) $('#suggestedOpening').value = review.suggested_opening;
  if (['edited', 'rejected'].includes(review.verdict)) {
    showOpeningEditor({ focus: false });
  }
  const assessment = document.querySelector(
    `[name="priority_assessment"][value="${review.priority_assessment || ''}"]`,
  );
  if (assessment) assessment.checked = true;
  if (review.priority_reason_code && priorityReasonLabels[review.priority_reason_code]) {
    $('#priorityReasonCode').value = review.priority_reason_code;
  }
  $('#priorityNote').value = review.priority_note || '';
  $('#revisionNotes').value = review.revision_notes || '';
  setText('#priorityNoteCount', String($('#priorityNote').value.length));
  setText('#revisionNotesCount', String($('#revisionNotes').value.length));
  state.selectedEvidenceRef = review.evidence_message_ref || '';
  syncPriorityReasonFields();
  renderSelectedEvidenceSummary();
}

function resetMessages() {
  state.messageRequestToken += 1;
  state.messages = [];
  state.messagesLoaded = false;
  state.messagesLoading = false;
  state.messagesHasMore = false;
  state.messagesNextCursor = '';
  state.messagesTotal = 0;
  state.messagesSnapshotAt = '';
  $('#chatPanel').open = false;
  $('#chatMessages').replaceChildren();
  $('#chatStatus').replaceChildren();
  $('#loadEarlierMessages').classList.add('hidden');
  $('#retryMessages').classList.add('hidden');
  setText('#chatMeta', '展开后读取最近 20 条');
  setText('#chatCount', '按需查看');
}

function isCustomerMessage(role) {
  return ['customer', 'client', 'buyer', 'user'].includes(String(role || '').toLowerCase());
}

function selectEvidenceMessage(messageRef) {
  state.selectedEvidenceRef = state.selectedEvidenceRef === messageRef ? '' : messageRef;
  document.querySelectorAll('.chat-message').forEach(item => {
    const selected = Boolean(state.selectedEvidenceRef) && item.dataset.messageRef === state.selectedEvidenceRef;
    item.classList.toggle('selected-evidence-message', selected);
    const button = item.querySelector('.evidence-toggle');
    if (button) {
      button.textContent = selected ? '取消作为依据' : '作为判断依据';
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    }
  });
  renderSelectedEvidenceSummary();
}

function renderMessages({ preserveScroll = false } = {}) {
  const container = $('#chatMessages');
  const previousHeight = container.scrollHeight;
  const previousTop = container.scrollTop;
  container.replaceChildren();

  if (!state.messages.length) {
    container.append(element('p', 'chat-empty', '没有可展示的历史文字聊天'));
  }

  state.messages.forEach(message => {
    const customerMessage = isCustomerMessage(message.role);
    const selected = Boolean(state.selectedEvidenceRef)
      && message.message_ref === state.selectedEvidenceRef;
    const item = element(
      'article',
      `chat-message ${customerMessage ? 'customer-message' : 'staff-message'}${selected ? ' selected-evidence-message' : ''}`,
    );
    item.dataset.messageRef = message.message_ref || '';

    const head = element('div', 'message-head');
    head.append(
      element('b', '', customerMessage ? '客户' : '客服'),
      element('time', '', formatChatTime(message.timestamp)),
    );
    item.append(head, element('p', '', message.text || '（无可展示文字）'));

    if (customerMessage && message.message_ref) {
      const evidenceButton = element(
        'button',
        'evidence-toggle',
        selected ? '取消作为依据' : '作为判断依据',
      );
      evidenceButton.type = 'button';
      evidenceButton.setAttribute('aria-pressed', selected ? 'true' : 'false');
      evidenceButton.addEventListener('click', () => selectEvidenceMessage(message.message_ref));
      item.append(evidenceButton);
    }
    container.append(item);
  });

  if (preserveScroll) {
    container.scrollTop = container.scrollHeight - previousHeight + previousTop;
  } else {
    container.scrollTop = container.scrollHeight;
  }
  renderSelectedEvidenceSummary();
}

async function loadMessages({ before = '' } = {}) {
  if (!state.current || state.messagesLoading) return;
  const profileId = state.current.sales_profile_id;
  const requestToken = ++state.messageRequestToken;
  const loadingEarlier = Boolean(before);
  state.messagesLoading = true;
  $('#retryMessages').classList.add('hidden');
  $('#loadEarlierMessages').disabled = true;
  setText('#chatStatus', loadingEarlier ? '正在读取更早的聊天…' : '正在读取最近 20 条聊天…');

  try {
    const params = new URLSearchParams({ limit: '20' });
    if (before) params.set('before', before);
    const payload = await api(
      `/api/profiles/${encodeURIComponent(profileId)}/messages?${params.toString()}`,
    );
    if (requestToken !== state.messageRequestToken || state.current?.sales_profile_id !== profileId) return;

    const incoming = Array.isArray(payload.items) ? payload.items : [];
    const combined = loadingEarlier ? [...incoming, ...state.messages] : incoming;
    const seen = new Set();
    state.messages = combined.filter(message => {
      const ref = String(message.message_ref || '');
      if (!ref || seen.has(ref)) return false;
      seen.add(ref);
      return true;
    });
    state.messagesLoaded = true;
    state.messagesHasMore = Boolean(payload.has_more);
    state.messagesNextCursor = payload.next_cursor || '';
    state.messagesTotal = Number(payload.total) || state.messages.length;
    state.messagesSnapshotAt = payload.snapshot_at || '';

    renderMessages({ preserveScroll: loadingEarlier });
    setText('#chatStatus', '', '');
    setText('#chatCount', `已显示 ${state.messages.length} / ${state.messagesTotal} 条`);
    setText(
      '#chatMeta',
      state.messagesSnapshotAt
        ? `聊天记录截至 ${formatDate(state.messagesSnapshotAt, true)}`
        : '聊天记录来自当前冻结数据',
    );
    $('#loadEarlierMessages').classList.toggle(
      'hidden',
      !state.messagesHasMore || !state.messagesNextCursor,
    );
    $('#loadEarlierMessages').textContent = `加载更早（已显示 ${state.messages.length} 条）`;
  } catch (error) {
    if (requestToken !== state.messageRequestToken) return;
    setText('#chatStatus', `聊天读取失败：${error.message}`);
    $('#retryMessages').classList.remove('hidden');
  } finally {
    if (requestToken === state.messageRequestToken) {
      state.messagesLoading = false;
      $('#loadEarlierMessages').disabled = false;
    }
  }
}

function switchWorkspace(name) {
  state.workspace = name === 'conversion' ? 'conversion' : 'followup';
  document.querySelectorAll('[data-workspace]').forEach(button => {
    button.classList.toggle('active', button.dataset.workspace === state.workspace);
  });
  const isConversion = state.workspace === 'conversion';
  $('#followupProgress').classList.toggle('hidden', isConversion);
  $('#followupWorkspace').classList.toggle('hidden', isConversion);
  $('#conversionWorkspace').classList.toggle('hidden', !isConversion);
  if (isConversion && !state.conversionLoaded) {
    refreshConversion().catch(error => {
      setText('#methodListCount', '读取失败');
      $('#methodSampleList').replaceChildren(element('div', 'loading', `归因样本读取失败：${error.message}`));
    });
  }
}

async function loadConversionSummary() {
  const summary = await api('/api/conversion/summary');
  setText('#methodSampleTotal', summary.method_sample_total);
  setText('#methodConvertedCount', summary.converted_7d);
  setText('#methodNegativeCount', summary.non_converted_7d);
  setText('#methodReviewedCount', summary.reviewed);
  setText('#methodReviewProgress', `${summary.reviewed} / ${summary.method_sample_total} 已审核`);
  const percent = summary.method_sample_total
    ? Math.round(summary.reviewed / summary.method_sample_total * 100)
    : 0;
  $('#methodProgressBar').style.width = `${percent}%`;
  return summary;
}

async function loadConversionSamples(keepSelection = true) {
  const params = new URLSearchParams({ limit: '100' });
  const status = $('#methodStatusFilter').value;
  const sampleState = $('#methodStateFilter').value;
  const signal = $('#methodSignalFilter').value;
  if (status) params.set('status', status);
  if (sampleState) params.set('sample_state', sampleState);
  if (signal) params.set('signal', signal);
  const payload = await api(`/api/conversion/samples?${params.toString()}`);
  state.conversionSamples = payload.items || [];
  renderConversionList(payload.total || 0);
  const currentId = keepSelection ? state.currentConversion?.episode_id : '';
  const target = state.conversionSamples.find(item => item.episode_id === currentId)
    || state.conversionSamples[0];
  if (target) {
    if (!state.currentConversion || state.currentConversion.episode_id !== target.episode_id || !keepSelection) {
      await selectConversionSample(target.episode_id);
    }
  } else {
    state.currentConversion = null;
    $('#methodDetailContent').classList.add('hidden');
    $('#methodDetailEmpty').classList.remove('hidden');
  }
}

function renderConversionList(total = state.conversionSamples.length) {
  const list = $('#methodSampleList');
  list.replaceChildren();
  setText('#methodListCount', `${total} 条`);
  if (!state.conversionSamples.length) {
    list.append(element('div', 'loading', '当前筛选下没有待核对样本'));
    return;
  }
  state.conversionSamples.forEach((sample, index) => {
    const converted = sample.sample_state === 'converted_7d';
    const button = element(
      'button',
      `sample-row method-sample-row ${converted ? 'converted' : 'not-converted'}`,
    );
    button.type = 'button';
    button.dataset.id = sample.episode_id;
    if (state.currentConversion?.episode_id === sample.episode_id) button.classList.add('selected');
    const score = element('span', 'sample-score', converted ? '成交' : '未成');
    const copy = element('span', 'sample-copy');
    const keySignal = sample.explicit_price_barrier !== 'none'
      ? methodLabels[sample.explicit_price_barrier]
      : sample.suspected_barrier !== 'none'
        ? methodLabels[sample.suspected_barrier]
        : methodLabels[sample.talk_track_primary];
    copy.append(
      element('b', '', sample.sample_label),
      element('small', '', `${sample.ended_on} · ${keySignal || '一般样本'}`),
    );
    const status = element(
      'em',
      sample.reviewed ? `status ${sample.latest_verdict}` : 'status',
      sample.reviewed ? (methodLabels[sample.latest_verdict] || '已审核') : '待审核',
    );
    button.append(score, copy, status);
    button.setAttribute('aria-label', `${index + 1}，${sample.sample_label}，${status.textContent}`);
    button.addEventListener('click', () => selectConversionSample(sample.episode_id));
    list.append(button);
  });
}

function methodSignalCard(label, value) {
  const card = element('article');
  card.append(element('span', '', label), element('b', '', value || '无'));
  return card;
}

function fillConversionReview(detail) {
  document.querySelectorAll('[name="method_verdict"]').forEach(input => { input.checked = false; });
  const review = detail.review || null;
  const verdict = review?.verdict || '';
  const verdictInput = verdict
    ? document.querySelector(`[name="method_verdict"][value="${verdict}"]`)
    : null;
  if (verdictInput) verdictInput.checked = true;
  const values = {
    correctedOrigin: review?.corrected_origin || detail.origin,
    correctedIntent: review?.corrected_intent || detail.intent,
    correctedPriceBarrier: review?.corrected_explicit_price_barrier || detail.explicit_price_barrier,
    correctedSuspectedBarrier: review?.corrected_suspected_barrier || detail.suspected_barrier,
    correctedTalkTrack: review?.corrected_talk_track_primary || detail.talk_track_primary,
  };
  Object.entries(values).forEach(([id, value]) => { $(`#${id}`).value = value; });
  $('#methodReviewNote').value = review?.note || '';
  setText('#methodReviewNoteCount', String($('#methodReviewNote').value.length));
  $('#methodCorrectionFields').classList.toggle('hidden', verdict !== 'corrected');
  setText('#methodSaveStatus', '', '');
}

function renderConversionDetail(detail) {
  const converted = detail.sample_state === 'converted_7d';
  setText('#methodOutcome', methodLabels[detail.sample_state]);
  setText('#methodSampleTitle', detail.sample_label);
  setText('#methodSampleMeta', `回合结束于 ${detail.ended_on} · 审核优先级 ${detail.review_priority}`);
  setText(
    '#methodReviewStatus',
    detail.review ? (methodLabels[detail.review.verdict] || '已审核') : '待审核',
  );
  $('#methodReviewStatus').classList.toggle('excluded', detail.review?.verdict === 'rejected');
  $('#methodReviewStatus').classList.toggle('review', detail.review?.verdict === 'corrected');

  const cards = $('#methodSignalCards');
  cards.replaceChildren(
    methodSignalCard('发起方', methodLabels[detail.origin]),
    methodSignalCard('客户意图', methodLabels[detail.intent]),
    methodSignalCard('明确价格信号', methodLabels[detail.explicit_price_barrier]),
    methodSignalCard('疑似阻碍', methodLabels[detail.suspected_barrier]),
    methodSignalCard('客服主要动作', methodLabels[detail.talk_track_primary]),
    methodSignalCard('成交与复购', `${converted ? '7 天内成交' : '7 天未成交'}${detail.repeat_90d === true ? ' · 90 天内复购' : ''}`),
  );

  setText('#methodMessageNote', detail.message_window?.note, '聊天仅用于人工核对。');
  const messages = $('#methodMessages');
  messages.replaceChildren();
  if (!detail.messages?.length) {
    messages.append(element('p', 'empty-copy', '该时间窗口没有可展示的文字聊天'));
  } else {
    detail.messages.forEach(message => {
      const item = element('article', `chat-message ${message.role === 'studio' ? 'staff-message' : ''}`);
      const head = element('div', 'message-head');
      head.append(
        element('b', '', message.role === 'studio' ? '客服' : '客户'),
        element('time', '', formatChatTime(message.timestamp)),
      );
      item.append(head, element('p', '', message.text));
      messages.append(item);
    });
  }
  fillConversionReview(detail);
}

async function selectConversionSample(episodeId) {
  $('#methodDetailEmpty').classList.add('hidden');
  $('#methodDetailContent').classList.remove('hidden');
  $('#methodDetailContent').classList.add('loading-state');
  try {
    const detail = await api(`/api/conversion/samples/${encodeURIComponent(episodeId)}`);
    state.currentConversion = detail;
    renderConversionDetail(detail);
    renderConversionList();
  } catch (error) {
    setText('#methodSaveStatus', error.message);
  } finally {
    $('#methodDetailContent').classList.remove('loading-state');
  }
}

async function saveConversionReview(event) {
  event.preventDefault();
  if (!state.currentConversion) return;
  const verdict = document.querySelector('[name="method_verdict"]:checked')?.value || '';
  if (!verdict) {
    setText('#methodSaveStatus', '请先选择审核结论');
    return;
  }
  const note = $('#methodReviewNote').value.trim();
  if (verdict === 'corrected' && !note) {
    setText('#methodSaveStatus', '修正后纳入时，请填写审核说明');
    $('#methodReviewNote').focus();
    return;
  }
  const button = $('#saveMethodReview');
  button.disabled = true;
  setText('#methodSaveStatus', '正在保存…');
  try {
    await api(`/api/conversion/samples/${encodeURIComponent(state.currentConversion.episode_id)}/review`, {
      method: 'POST',
      body: JSON.stringify({
        audit_version: state.currentConversion.audit_version,
        verdict,
        corrected_origin: verdict === 'corrected' ? $('#correctedOrigin').value : '',
        corrected_intent: verdict === 'corrected' ? $('#correctedIntent').value : '',
        corrected_explicit_price_barrier: verdict === 'corrected' ? $('#correctedPriceBarrier').value : '',
        corrected_suspected_barrier: verdict === 'corrected' ? $('#correctedSuspectedBarrier').value : '',
        corrected_talk_track_primary: verdict === 'corrected' ? $('#correctedTalkTrack').value : '',
        note,
      }),
    });
    const currentId = state.currentConversion.episode_id;
    await Promise.all([loadConversionSummary(), loadConversionSamples(true)]);
    if (state.conversionSamples.some(item => item.episode_id === currentId)) {
      await selectConversionSample(currentId);
    }
    setText('#methodSaveStatus', '已保存，可继续审核下一条');
  } catch (error) {
    setText('#methodSaveStatus', error.message);
  } finally {
    button.disabled = false;
  }
}

async function nextConversionSample() {
  const currentId = state.currentConversion?.episode_id;
  const index = Math.max(0, state.conversionSamples.findIndex(item => item.episode_id === currentId));
  const ordered = [
    ...state.conversionSamples.slice(index + 1),
    ...state.conversionSamples.slice(0, index + 1),
  ];
  const next = ordered.find(item => !item.reviewed && item.episode_id !== currentId);
  if (next) await selectConversionSample(next.episode_id);
  else setText('#methodSaveStatus', '当前列表已全部审核');
}

async function refreshConversion() {
  await Promise.all([loadConversionSummary(), loadConversionSamples(false)]);
  state.conversionLoaded = true;
}

async function refreshAll() {
  await Promise.all([loadSummary(), loadProfiles(false)]);
}

async function loadSummary() {
  const summary = await api('/api/summary');
  setText('#generatedCount', summary.generated);
  setText('#eligibleCount', summary.promotion_eligible);
  setText('#reviewCount', summary.promotion_review);
  setText('#excludedCount', summary.promotion_excluded);
  setText('#reviewedCount', summary.reviewed);
  setText('#reviewProgress', `${summary.reviewed} / ${summary.total} 已验收`);
  const percent = summary.total ? Math.round(summary.reviewed / summary.total * 100) : 0;
  $('#progressBar').style.width = `${percent}%`;
  const identityUpdated = summary.customer_source_synced_at
    ? ` · 客户资料更新于 ${formatDate(summary.customer_source_synced_at, true)}`
    : '';
  setText('#runMeta', `画像数据截至 ${formatDate(summary.run?.as_of_at)}${identityUpdated} · 联系前仍需核对最新状态`);
  return summary;
}

async function loadProfiles(keepSelection = true) {
  const params = new URLSearchParams();
  params.set('promotion', $('#promotionFilter').value || 'eligible');
  if ($('#stratumFilter').value) params.set('stratum', $('#stratumFilter').value);
  if ($('#statusFilter').value) params.set('status', $('#statusFilter').value);
  const payload = await api(`/api/profiles?${params.toString()}`);
  state.profiles = payload.items || [];
  renderProfileList();

  const currentId = keepSelection ? state.current?.sales_profile_id : '';
  const target = state.profiles.find(item => item.sales_profile_id === currentId) || state.profiles[0];
  if (target) {
    if (!state.current || state.current.sales_profile_id !== target.sales_profile_id || !keepSelection) {
      await selectProfile(target.sales_profile_id);
    }
  } else {
    state.current = null;
    $('#detailContent').classList.add('hidden');
    $('#detailEmpty').classList.remove('hidden');
  }
}

function renderProfileList() {
  const list = $('#sampleList');
  list.replaceChildren();
  setText('#listCount', `${state.profiles.length} 人`);
  if (!state.profiles.length) {
    list.append(element('div', 'loading', '当前筛选下没有客户'));
    return;
  }
  state.profiles.forEach((profile, index) => {
    const promotionState = profile.promotion_state || (profile.promotion_eligible ? 'eligible' : 'excluded');
    const button = element('button', `sample-row ${promotionState}`);
    button.type = 'button';
    button.dataset.id = profile.sales_profile_id;
    if (state.current?.sales_profile_id === profile.sales_profile_id) button.classList.add('selected');

    const scoreCopy = promotionState === 'eligible' ? profile.priority_score : (promotionState === 'review' ? '待' : '—');
    const score = element('span', 'sample-score', scoreCopy);
    score.title = promotionState === 'eligible'
      ? `促单优先分 ${profile.priority_score}`
      : (promotionState === 'review' ? '售后事实待确认' : '已排除促销');
    const copy = element('span', 'sample-copy');
    const stateLabel = promotionState === 'eligible'
      ? profile.priority_label
      : (promotionState === 'review' ? '售后待确认' : '仅服务、不促销');
    copy.append(
      element('b', '', profile.label),
      element('small', '', `${profile.phone_hint} · ${stateLabel}`),
    );
    const statusCopy = profile.review_count ? (verdictLabels[profile.latest_verdict] || '已验收') : '待验收';
    const status = element('em', profile.review_count ? `status ${profile.latest_verdict}` : 'status', statusCopy);
    button.append(score, copy, status);
    button.setAttribute('aria-label', `${index + 1}，${profile.label}，${statusCopy}`);
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
    state.ordersExpanded = false;
    renderDetail(detail);
    renderProfileList();
  } catch (error) {
    setText('#saveStatus', error.message);
  } finally {
    $('#detailContent').classList.remove('loading-state');
  }
}

function renderDetail(detail) {
  const customer = detail.customer || {};
  const business = detail.business || {};
  const card = detail.card || {};
  resetMessages();

  setText('#sampleStratum', stratumLabels[detail.stratum] || '客户画像');
  setText('#sampleTitle', customer.name || detail.label || '客户');
  setText('#sampleMeta', `历史数据截至 ${formatDate(detail.as_of_at)} · 默认满库存`);
  setText('#headerPriority', business.priority_label);
  $('#headerPriority').classList.remove('review', 'excluded');
  if (business.promotion_state !== 'eligible') $('#headerPriority').classList.add(business.promotion_state);

  const banner = $('#promotionBanner');
  banner.classList.remove('hidden', 'eligible', 'review', 'excluded');
  if (business.promotion_state === 'eligible') {
    banner.classList.add('eligible');
    banner.textContent = `可进入促销列表 · ${business.priority_label}，列表已按优先分从高到低排列`;
  } else if (business.promotion_state === 'review') {
    banner.classList.add('review');
    banner.textContent = `暂不进入促销 · ${business.exclusion_reason || '售后事实待确认'}`;
  } else {
    banner.classList.add('excluded');
    banner.textContent = `已排除促销 · ${business.exclusion_reason || '当前更适合服务，不适合促销'}`;
  }

  setText('#customerName', customer.name);
  setText('#customerPhone', customer.phone);
  setText('#memberShop', customer.member_shop);
  setText('#lastOrderChannel', customer.last_order_channel);
  setText('#priorityScore', business.promotion_state === 'eligible' ? business.priority_score : (business.promotion_state === 'review' ? '待确认' : '不促销'));
  $('#priorityScore').classList.toggle('textual', business.promotion_state !== 'eligible');
  setText('#priorityLabel', business.priority_label);
  setText('#scoreFactors', (business.score_factors || []).map(humanize).join(' · '));
  setText('#averageOrder', money(business.average_paid_amount_yuan));
  setText('#orderCount', `${business.paid_order_count ?? 0} 条`);
  setText('#repeatCount', `${business.repeat_count ?? 0} 次复购`);
  setText('#totalSpend', money(business.historical_spend_yuan));
  setText('#lastOrderDate', business.last_order_date ? `最近付款 ${business.last_order_date}` : '最近付款日期待补全');
  setText('#aftersalesSummary', business.aftersales_summary);
  setText('#cancelledCount', business.cancelled_count ? `${business.cancelled_count} 条取消订单，不计消费与售后` : '取消订单 0 条');
  setText('#contactHabit', business.contact_habit);
  setText('#orderHabit', business.order_habit);
  setText('#repurchaseCycle', business.median_repurchase_interval_days === null || business.median_repurchase_interval_days === undefined
    ? '复购周期证据不足'
    : `约 ${business.median_repurchase_interval_days} 天`);

  renderNarrative($('#currentOpportunity'), card.current_opportunity, '暂未发现明确的当前机会');
  renderNarrative($('#naturalOpening'), card.natural_opening, '暂无可审核的建议开场');
  renderOpeningCaution(card.natural_opening);
  renderCrossSell(detail.cross_sell || {});
  renderNarrative($('#productPreferences'), {
    preferred_products: detail.facts?.preferred_products || [],
    preferred_colors: detail.facts?.preferred_colors || [],
    preferred_sizes: detail.facts?.preferred_sizes || [],
  });
  renderNarrative($('#purchaseDrivers'), card.purchase_drivers);
  renderNarrative($('#historicalCommitments'), card.historical_commitments);
  renderNarrative($('#contactReason'), card.contact_reason);
  renderNarrative($('#risks'), card.risks, '暂未发现明确风险');
  renderNarrative($('#unknowns'), card.unknowns, '暂无其他待确认项');

  renderOrders(detail.order_history || [], business);
  renderFacts(detail.facts || {}, business);
  renderEvidence(detail.events || []);
  renderHistory(detail.reviews || []);
  fillOwnReview(detail);
}

function renderOrders(orders, business) {
  const body = $('#orderTableBody');
  body.replaceChildren();
  const visible = state.ordersExpanded ? orders : orders.slice(0, 8);
  if (!visible.length) {
    const row = element('tr');
    const cell = element('td', 'table-empty', '没有可展示的历史付款记录');
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
  visible.forEach(order => {
    const row = element('tr');
    row.append(
      element('td', '', order.paid_on || '日期待补全'),
      element('td', 'product-cell', humanize(order.product) || '商品待补全'),
      element('td', '', order.channel || '渠道待补全'),
      element('td', 'money-cell', money(order.amount_yuan)),
      element('td', '', humanize(order.status) || '已付款'),
      element('td', order.status === '有售后' ? 'aftersales-cell' : '', humanize(order.aftersales) || '无售后'),
    );
    body.append(row);
  });
  const rate = business.aftersales_rate_percent;
  const rateCopy = rate === null || rate === undefined ? '售后比例待确认' : `售后率 ${rate}%`;
  const cancelCopy = business.cancelled_count ? ` · 另有 ${business.cancelled_count} 条取消订单` : '';
  setText('#orderSummary', `${business.paid_order_count ?? 0} 条有效付款记录${cancelCopy} · ${money(business.historical_spend_yuan)} · ${rateCopy}`);
  const toggle = $('#toggleOrders');
  toggle.classList.toggle('hidden', orders.length <= 8);
  toggle.textContent = state.ordersExpanded ? '收起订单' : `展开全部 ${orders.length} 条订单`;
}

function renderFacts(facts, business) {
  const container = $('#factSummary');
  container.replaceChildren();
  const entries = [
    ['历史消费', money(facts.historical_spend_yuan)],
    ['客单均额', money(facts.average_paid_amount_yuan)],
    ['有效付款记录', `${facts.historical_orders ?? 0} 条`],
    ['距上次付款', facts.days_since_last_order === null || facts.days_since_last_order === undefined ? '待补全' : `${facts.days_since_last_order} 天`],
    ['微信联系习惯', facts.contact_habit],
    ['历史下单时段', facts.order_habit],
    ['偏好商品', (facts.preferred_products || []).join('、') || '待确认'],
    ['偏好颜色', (facts.preferred_colors || []).join('、') || '待确认'],
    ['偏好尺码', (facts.preferred_sizes || []).join('、') || '待确认'],
    ['促销资格', business.promotion_state === 'eligible' ? '可进入促销列表' : (business.promotion_state === 'review' ? '售后待确认' : '仅服务、不促销')],
  ];
  entries.forEach(([label, value]) => {
    const item = element('div');
    item.append(element('span', '', label), element('b', '', humanize(value) || '待确认'));
    container.append(item);
  });
}

function renderEvidence(events) {
  const container = $('#evidenceList');
  container.replaceChildren();
  let evidenceCount = 0;
  if (!events.length) container.append(element('p', 'empty-copy', '暂无可展示的业务依据'));
  events.forEach(event => {
    const item = element('article', 'evidence-item');
    item.append(element('h4', '', event.label || '销售线索'));
    if (event.summary) item.append(element('p', '', humanize(event.summary)));
    const evidence = Array.isArray(event.evidence) ? event.evidence : [];
    evidenceCount += evidence.length;
    if (evidence.length) {
      const list = element('ul');
      evidence.forEach(entry => list.append(element('li', '', `${entry.label}：${humanize(entry.quote)}`)));
      item.append(list);
    }
    container.append(item);
  });
  setText('#evidenceCount', `${evidenceCount} 条`);
}

function renderHistory(reviews) {
  const container = $('#reviewHistory');
  container.replaceChildren();
  if (!reviews.length) {
    container.append(element('p', 'empty-copy', '还没有开场验收记录'));
    return;
  }
  container.append(element('h4', '', '最近验收记录'));
  reviews.slice(0, 5).forEach(review => {
    const item = element('article', `history-item ${review.verdict || ''}`);
    const head = element('div');
    head.append(
      element('b', '', verdictLabels[review.verdict] || '已验收'),
      element('time', '', formatDate(review.updated_at, true)),
    );
    item.append(head);
    if (review.suggested_opening) item.append(element('p', '', review.suggested_opening));
    if (review.revision_notes) item.append(element('p', 'revision-history-copy', `其他修改：${review.revision_notes}`));
    container.append(item);
  });
}

async function saveReview(event) {
  event.preventDefault();
  if (!state.current) return;
  const verdict = document.querySelector('[name="verdict"]:checked')?.value || '';
  if (!verdict) {
    setText('#saveStatus', '请先选择总体结论');
    return;
  }
  const suggestedOpening = $('#suggestedOpening').value.trim();
  const priorityAssessment = document.querySelector('[name="priority_assessment"]:checked')?.value || '';
  const priorityReasonCode = $('#priorityReasonCode').value;
  if (['too_high', 'too_low', 'not_suitable'].includes(priorityAssessment) && !priorityReasonCode) {
    setText('#saveStatus', '请为优先级调整选择一个主要原因');
    return;
  }
  if (['edited', 'rejected'].includes(verdict) && !suggestedOpening) {
    showOpeningEditor();
    setText('#saveStatus', '请写一版更合适的开场');
    return;
  }

  const button = $('#saveReview');
  button.disabled = true;
  setText('#saveStatus', '正在保存…');
  try {
    await api(`/api/profiles/${encodeURIComponent(state.current.sales_profile_id)}/review`, {
      method: 'POST',
      body: JSON.stringify({
        card_version: state.current.card_version,
        verdict,
        suggested_opening: suggestedOpening,
        revision_notes: $('#revisionNotes').value.trim(),
        priority_assessment: priorityAssessment,
        priority_reason_code: priorityReasonCode,
        priority_note: $('#priorityNote').value.trim(),
        evidence_message_ref: state.selectedEvidenceRef,
      }),
    });
    setText('#saveStatus', '已保存，可继续验收下一位');
    await Promise.all([loadSummary(), loadProfiles(true)]);
    await selectProfile(state.current.sales_profile_id);
    setText('#saveStatus', '已保存，可继续验收下一位');
  } catch (error) {
    setText('#saveStatus', error.message);
  } finally {
    button.disabled = false;
  }
}

async function nextUnreviewed() {
  if (!state.profiles.length) return;
  const currentIndex = Math.max(0, state.profiles.findIndex(item => item.sales_profile_id === state.current?.sales_profile_id));
  const ordered = [...state.profiles.slice(currentIndex + 1), ...state.profiles.slice(0, currentIndex + 1)];
  const next = ordered.find(item => !item.review_count && item.sales_profile_id !== state.current?.sales_profile_id);
  if (next) await selectProfile(next.sales_profile_id);
  else setText('#saveStatus', '当前列表已全部验收');
}

function boot() {
  document.querySelectorAll('[data-workspace]').forEach(button => {
    button.addEventListener('click', () => switchWorkspace(button.dataset.workspace));
  });
  $('#reviewForm').addEventListener('submit', saveReview);
  $('#methodReviewForm').addEventListener('submit', saveConversionReview);
  $('#promotionFilter').addEventListener('change', () => loadProfiles(false));
  $('#stratumFilter').addEventListener('change', () => loadProfiles(false));
  $('#statusFilter').addEventListener('change', () => loadProfiles(false));
  $('#nextUnreviewed').addEventListener('click', nextUnreviewed);
  $('#nextMethodSample').addEventListener('click', nextConversionSample);
  ['#methodStatusFilter', '#methodStateFilter', '#methodSignalFilter'].forEach(selector => {
    $(selector).addEventListener('change', () => loadConversionSamples(false));
  });
  document.querySelectorAll('[name="method_verdict"]').forEach(input => {
    input.addEventListener('change', () => {
      $('#methodCorrectionFields').classList.toggle('hidden', input.value !== 'corrected');
    });
  });
  $('#methodReviewNote').addEventListener('input', () => {
    setText('#methodReviewNoteCount', String($('#methodReviewNote').value.length));
  });
  $('#editOpening').addEventListener('click', () => showOpeningEditor());
  $('#chatPanel').addEventListener('toggle', () => {
    if ($('#chatPanel').open && !state.messagesLoaded && !state.messagesLoading) loadMessages();
  });
  $('#loadEarlierMessages').addEventListener('click', () => {
    if (state.messagesNextCursor) loadMessages({ before: state.messagesNextCursor });
  });
  $('#retryMessages').addEventListener('click', () => loadMessages());
  document.querySelectorAll('[name="priority_assessment"]').forEach(input => {
    input.addEventListener('change', () => syncPriorityReasonFields({ clear: true }));
  });
  $('#priorityNote').addEventListener('input', () => {
    setText('#priorityNoteCount', String($('#priorityNote').value.length));
  });
  $('#revisionNotes').addEventListener('input', () => {
    setText('#revisionNotesCount', String($('#revisionNotes').value.length));
  });
  $('#toggleOrders').addEventListener('click', () => {
    state.ordersExpanded = !state.ordersExpanded;
    renderOrders(state.current?.order_history || [], state.current?.business || {});
  });
  document.querySelectorAll('[name="verdict"]').forEach(input => {
    input.addEventListener('change', () => {
      if (['edited', 'rejected'].includes(input.value)) showOpeningEditor();
      else {
        $('#suggestedOpening').value = '';
        $('#openingEditor').classList.add('hidden');
      }
    });
  });

  refreshAll().catch(error => setText('#runMeta', `数据读取失败：${error.message}`));
}

document.addEventListener('DOMContentLoaded', boot);
