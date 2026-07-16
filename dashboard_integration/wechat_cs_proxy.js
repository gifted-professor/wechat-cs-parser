'use strict';

const fs = require('fs');
const http = require('http');
const https = require('https');
const crypto = require('crypto');

const MAX_REQUEST_BYTES = Number(process.env.WECHAT_CS_PROXY_MAX_BYTES || 1024 * 1024);
const MAX_RESPONSE_BYTES = Number(process.env.WECHAT_CS_PROXY_MAX_RESPONSE_BYTES || 4 * 1024 * 1024);
const TIMEOUT_MS = Number(process.env.WECHAT_CS_PROXY_TIMEOUT_MS || 45000);
const SENSITIVE_KEY_RE = /phone|mobile|wxid|wechat|raw|hmac/i;
const SENSITIVE_VALUE_RE = /(?:\b1[3-9]\d{9}\b|wxid_[a-z0-9_-]+|(?:phone|hmac)_[a-z0-9_-]{8,}|(?:微信号|wechat(?:\s*id)?|\bwx\b)\s*[:：]?\s*[a-z0-9_-]{4,})/i;
const ANONYMOUS_CUSTOMER_RE = /^customer_[0-9a-f]{16,64}$/;
const PRIVATE_DISPLAY_FIELDS = ['display_name', 'owner', 'account_label', 'contact_hint'];
let privateMapCache = { path: '', mtimeMs: -1, value: null };

function scrubSensitiveKeys(value) {
  if (Array.isArray(value)) return value.map(scrubSensitiveKeys);
  if (!value || typeof value !== 'object') return value;
  const safe = Object.create(null);
  for (const [key, item] of Object.entries(value)) {
    if (SENSITIVE_KEY_RE.test(key)) continue;
    safe[key] = scrubSensitiveKeys(item);
  }
  return safe;
}

function safePrivateFields(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const safe = Object.create(null);
  for (const field of PRIVATE_DISPLAY_FIELDS) {
    if (typeof value[field] !== 'string') continue;
    const text = value[field].trim().slice(0, 240);
    const digits = text.replace(/\D/g, '');
    const hasFormattedPhone = /(?:86)?1[3-9]\d{9}/.test(digits);
    if (text && !hasFormattedPhone && !SENSITIVE_VALUE_RE.test(text)) safe[field] = text;
  }
  return safe;
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ''), 'utf8');
  const b = Buffer.from(String(right || ''), 'utf8');
  return a.length === b.length && a.length > 0 && crypto.timingSafeEqual(a, b);
}

function isAllowedActionRoute(method, suffix) {
  if (method === 'GET' && (suffix === '/health' || suffix === '/action-queue')) return true;
  const detail = /^\/action-queue\/[A-Za-z0-9_.:-]{1,160}$/.test(suffix);
  if (method === 'GET' && detail) return true;
  return method === 'POST'
    && /^\/action-queue\/[A-Za-z0-9_.:-]{1,160}\/(?:draft|feedback)$/.test(suffix);
}

function loadPrivateMap() {
  const configuredPath = String(process.env.WECHAT_CS_PRIVATE_MAP_PATH || '').trim();
  if (!configuredPath) return null;
  try {
    const stat = fs.statSync(configuredPath);
    if (!stat.isFile()) return null;
    if (privateMapCache.path === configuredPath && privateMapCache.mtimeMs === stat.mtimeMs) {
      return privateMapCache.value;
    }
    const parsed = JSON.parse(fs.readFileSync(configuredPath, 'utf8'));
    const source = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed.customers && typeof parsed.customers === 'object' ? parsed.customers : parsed)
      : null;
    const safeMap = Object.create(null);
    if (source) {
      for (const [customerKey, privateValue] of Object.entries(source)) {
        if (!ANONYMOUS_CUSTOMER_RE.test(customerKey)) continue;
        safeMap[customerKey] = safePrivateFields(privateValue);
      }
    }
    privateMapCache = { path: configuredPath, mtimeMs: stat.mtimeMs, value: safeMap };
    return safeMap;
  } catch (error) {
    privateMapCache = { path: configuredPath, mtimeMs: -1, value: null };
    return null;
  }
}

function hydrateActionPayload(payload, privateMap = loadPrivateMap()) {
  function visit(value) {
    if (Array.isArray(value)) return value.map(visit);
    if (!value || typeof value !== 'object') return value;
    const safe = Object.create(null);
    for (const [key, item] of Object.entries(value)) {
      if (SENSITIVE_KEY_RE.test(key)) continue;
      if (PRIVATE_DISPLAY_FIELDS.includes(key)) continue;
      safe[key] = visit(item);
    }
    if (typeof safe.customer_key === 'string' && ANONYMOUS_CUSTOMER_RE.test(safe.customer_key)) {
      const mapped = privateMap && typeof privateMap === 'object'
        ? safePrivateFields(privateMap[safe.customer_key])
        : {};
      Object.assign(safe, mapped);
      if (!safe.display_name) safe.display_name = '匿名客户';
    }
    return safe;
  }
  return visit(scrubSensitiveKeys(payload));
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_REQUEST_BYTES) {
        const error = new Error('request body too large');
        error.statusCode = 413;
        reject(error);
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function requestUpstream(target, method, headers, body) {
  const client = target.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const upstream = client.request(
      target,
      { method, headers, timeout: TIMEOUT_MS },
      response => {
        const chunks = [];
        let size = 0;
        response.on('error', reject);
        response.on('data', chunk => {
          size += chunk.length;
          if (size > MAX_RESPONSE_BYTES) {
            response.destroy(new Error('upstream response too large'));
            return;
          }
          chunks.push(chunk);
        });
        response.on('end', () => resolve({
          status: response.statusCode || 502,
          headers: response.headers,
          body: Buffer.concat(chunks),
        }));
      },
    );
    upstream.on('timeout', () => upstream.destroy(new Error('upstream timeout')));
    upstream.on('error', reject);
    if (body.length) upstream.write(body);
    upstream.end();
  });
}

async function handleWechatCsProxy(req, res, requestUrl, urlPath) {
  const prefix = '/api/wechat-cs';
  if (urlPath !== prefix && !urlPath.startsWith(`${prefix}/`)) return false;

  if (!['GET', 'POST'].includes(req.method || 'GET')) {
    sendJson(res, 405, { ok: false, error: 'method_not_allowed' });
    return true;
  }

  const baseUrl = String(process.env.WECHAT_CS_BASE_URL || '').trim();
  const token = String(process.env.WECHAT_CS_TOKEN || '').trim();
  const dashboardToken = String(process.env.WECHAT_CS_DASHBOARD_TOKEN || '').trim();
  if (!baseUrl || !token || dashboardToken.length < 32) {
    sendJson(res, 503, { ok: false, error: 'wechat_cs_unconfigured' });
    return true;
  }
  if (!safeEqual(req.headers['x-wechat-cs-dashboard-token'], dashboardToken)) {
    sendJson(res, 401, { ok: false, error: 'wechat_cs_dashboard_unauthorized' });
    return true;
  }

  try {
    const suffix = urlPath.slice(prefix.length) || '/health';
    if (!isAllowedActionRoute(req.method || 'GET', suffix)) {
      sendJson(res, 404, { ok: false, error: 'wechat_cs_route_not_allowed' });
      return true;
    }
    const base = new URL(baseUrl);
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
      sendJson(res, 503, { ok: false, error: 'wechat_cs_invalid_base_url' });
      return true;
    }
    const target = new URL(`/v1${suffix}${requestUrl.search || ''}`, base);
    const body = req.method === 'POST' ? await readBody(req) : Buffer.alloc(0);
    const headers = {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'Content-Type': String(req.headers['content-type'] || 'application/json'),
      'Content-Length': body.length,
    };
    const upstream = await requestUpstream(target, req.method || 'GET', headers, body);
    let responseBody = upstream.body;
    const isActionQueue = suffix === '/action-queue' || suffix.startsWith('/action-queue/');
    if (isActionQueue) {
      try {
        const payload = JSON.parse(upstream.body.toString('utf8'));
        responseBody = Buffer.from(JSON.stringify(hydrateActionPayload(payload)), 'utf8');
      } catch (error) {
        sendJson(res, 502, { ok: false, error: 'wechat_cs_invalid_response' });
        return true;
      }
    }
    res.writeHead(upstream.status, {
      'Content-Type': String(upstream.headers['content-type'] || 'application/json; charset=utf-8'),
      'Cache-Control': 'no-store',
      'Content-Length': responseBody.length,
    });
    res.end(responseBody);
  } catch (error) {
    sendJson(res, error.statusCode || 502, { ok: false, error: 'wechat_cs_unavailable' });
  }
  return true;
}

module.exports = {
  handleWechatCsProxy,
  hydrateActionPayload,
  isAllowedActionRoute,
  loadPrivateMap,
  scrubSensitiveKeys,
};
