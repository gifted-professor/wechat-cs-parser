'use strict';

const http = require('http');
const https = require('https');

const MAX_REQUEST_BYTES = Number(process.env.WECHAT_CS_PROXY_MAX_BYTES || 1024 * 1024);
const MAX_RESPONSE_BYTES = Number(process.env.WECHAT_CS_PROXY_MAX_RESPONSE_BYTES || 4 * 1024 * 1024);
const TIMEOUT_MS = Number(process.env.WECHAT_CS_PROXY_TIMEOUT_MS || 45000);

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

  if (!['GET', 'POST', 'PATCH'].includes(req.method || 'GET')) {
    sendJson(res, 405, { ok: false, error: 'method_not_allowed' });
    return true;
  }

  const baseUrl = String(process.env.WECHAT_CS_BASE_URL || '').trim();
  const token = String(process.env.WECHAT_CS_TOKEN || '').trim();
  if (!baseUrl || !token) {
    sendJson(res, 503, { ok: false, error: 'wechat_cs_unconfigured' });
    return true;
  }

  try {
    const suffix = urlPath.slice(prefix.length) || '/health';
    const target = new URL(`/v1${suffix}${requestUrl.search || ''}`, baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`);
    const body = ['POST', 'PATCH'].includes(req.method || '') ? await readBody(req) : Buffer.alloc(0);
    const headers = {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'Content-Type': String(req.headers['content-type'] || 'application/json'),
      'Content-Length': body.length,
    };
    const upstream = await requestUpstream(target, req.method || 'GET', headers, body);
    res.writeHead(upstream.status, {
      'Content-Type': String(upstream.headers['content-type'] || 'application/json; charset=utf-8'),
      'Cache-Control': 'no-store',
      'Content-Length': upstream.body.length,
    });
    res.end(upstream.body);
  } catch (error) {
    sendJson(res, error.statusCode || 502, { ok: false, error: 'wechat_cs_unavailable' });
  }
  return true;
}

module.exports = { handleWechatCsProxy };
