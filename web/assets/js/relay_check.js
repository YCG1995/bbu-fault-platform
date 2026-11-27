// 浏览器代理自检页：不登录、不调用平台 API，只从当前浏览器访问模型网关。
// 报告永远不包含 Key 或模型响应正文。
const $ = (id) => document.getElementById(id);

const state = {
  running: false,
  checks: [],
  conclusion: { tone: '', title: '', detail: '' },
  target: { host: '', path: '', protocol: '' },
  modelCount: 0,
};

function clearRows() {
  $('check-status').innerHTML = '';
  state.checks = [];
  state.conclusion = { tone: '', title: '', detail: '' };
  state.modelCount = 0;
  $('check-export').disabled = true;
}

function addRow(label, tone, detail, key = '') {
  state.checks.push({ key: key || label, label, tone, detail });
  const row = document.createElement('div');
  row.className = `check-row ${tone || ''}`.trim();
  const dot = document.createElement('span');
  dot.className = 'dot';
  const text = document.createElement('div');
  text.className = 'txt';
  const strong = document.createElement('strong');
  strong.textContent = label;
  const span = document.createElement('span');
  span.textContent = detail || '';
  text.appendChild(strong);
  text.appendChild(span);
  row.appendChild(dot);
  row.appendChild(text);
  $('check-status').appendChild(row);
}

function setSummary(tone, title, detail) {
  const el = $('check-summary');
  el.className = `check-summary ${tone || ''}`.trim();
  el.innerHTML = '';
  const strong = document.createElement('strong');
  strong.textContent = title;
  el.appendChild(strong);
  if (detail) {
    const div = document.createElement('div');
    div.style.marginTop = '5px';
    div.textContent = detail;
    el.appendChild(div);
  }
  state.conclusion = { tone, title, detail };
}

function isPrivateHost(host) {
  const h = String(host || '').toLowerCase();
  if (h === 'localhost' || h.endsWith('.localhost')) return true;
  if (h.startsWith('10.') || h.startsWith('192.168.') || h.startsWith('127.') ||
      h.startsWith('169.254.')) return true;
  const m = h.match(/^172\.(\d{1,2})\./);
  if (m && Number(m[1]) >= 16 && Number(m[1]) <= 31) return true;
  if (h.startsWith('100.64.') || h.startsWith('100.65.') || h.startsWith('100.66.') ||
      h.startsWith('100.67.') || h.startsWith('100.68.') || h.startsWith('100.69.') ||
      h.startsWith('100.7')) return true;
  if (h.startsWith('fc') || h.startsWith('fd') || h.startsWith('fe80:')) return true;
  return false;
}

function forbiddenHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[]|\]$/g, '');
  if (!host) return true;
  if (host === 'localhost' || host === 'localhost.localdomain' || host.endsWith('.localhost')) return true;
  if (host === '0.0.0.0' || host === '::' || host === '::1') return true;
  if (host.startsWith('127.') || host.startsWith('169.254.')) return true;
  const m = host.match(/^([0-9a-f]{1,4}):/);
  if (m) {
    const value = parseInt(m[1], 16);
    if (value >= 0xfe80 && value <= 0xfebf) return true;
  }
  if (host.startsWith('::ffff:127.')) return true;
  return false;
}

function parseTarget(raw) {
  let url;
  try { url = new URL(String(raw || '').trim()); } catch { throw new Error('Base URL 格式非法'); }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('只允许 http(s) 地址');
  if (url.username || url.password) throw new Error('地址不能包含用户名或密码');
  if (forbiddenHost(url.hostname)) throw new Error('平台会拒绝 localhost、回环或链路本地地址');
  return url;
}

function endpointSet(url, provider) {
  let base = url.toString().replace(/\/+$/, '');
  const isOpenAI = provider !== 'anthropic';
  if (isOpenAI) {
    let root = base;
    if (/\/chat\/completions$/.test(root)) root = root.replace(/\/chat\/completions$/, '');
    if (/\/models$/.test(root)) root = root.replace(/\/models$/, '');
    return { root, models: `${root}/models`, inference: `${root}/chat/completions`, protocol: 'openai' };
  }
  let root = base;
  if (/\/v1\/messages$/.test(root)) root = root.replace(/\/v1\/messages$/, '/v1');
  else if (/\/messages$/.test(root)) root = root.replace(/\/messages$/, '');
  if (/\/models$/.test(root)) root = root.replace(/\/models$/, '');
  if (/\/v1$/.test(root)) {
    return { root, models: `${root}/models`, inference: `${root}/messages`, protocol: 'anthropic' };
  }
  return { root, models: `${root}/v1/models`, inference: `${root}/v1/messages`, protocol: 'anthropic' };
}

function authStyles(provider, key) {
  const bearer = { name: 'Bearer', protocol: provider === 'anthropic' ? 'anthropic' : 'openai',
    headers: { Authorization: `Bearer ${key}` } };
  const apiKey = { name: 'api-key', protocol: 'openai', headers: { 'api-key': key } };
  const anthropic = { name: 'x-api-key + anthropic-version', protocol: 'anthropic',
    headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01' } };
  if (provider === 'openai') return [bearer, apiKey];
  if (provider === 'anthropic') return [anthropic, bearer];
  return [bearer, apiKey, anthropic];
}

async function timedFetch(url, options, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort('selftest-timeout'), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err?.name === 'AbortError') {
      const e = new Error('请求超时');
      e.code = 'timeout';
      throw e;
    }
    const e = new Error(err?.message || 'fetch 失败');
    e.code = 'network';
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function toneFromStatus(status) {
  if (status >= 200 && status < 300) return 'ok';
  if (status === 401 || status === 403) return 'warn';
  if (status === 429) return 'warn';
  if (status >= 500) return 'warn';
  return 'warn';
}

function describeError(err) {
  if (err?.code === 'timeout') return '请求超时';
  const text = String(err?.message || err || '未知错误');
  // fetch 的 TypeError 文案因浏览器而异，不在这里断言具体 CORS 原因。
  return text.length > 180 ? `${text.slice(0, 180)}…` : text;
}

async function parseJsonSafe(resp) {
  const text = await resp.text();
  try { return JSON.parse(text); } catch { return null; }
}

function pickModelReply(data, provider) {
  if (!data) return '';
  if (provider === 'anthropic') {
    const blocks = Array.isArray(data.content) ? data.content : [];
    return blocks.filter((b) => b && b.type === 'text').map((b) => b.text || '').join('').trim();
  }
  const choices = Array.isArray(data.choices) ? data.choices : [];
  const message = choices[0]?.message || {};
  return String(message.content || choices[0]?.text || '').trim();
}

async function runChecks() {
  if (state.running) return;
  clearRows();
  const baseRaw = $('check-base').value.trim();
  const model = $('check-model').value.trim();
  const providerInput = $('check-provider').value;
  const key = $('check-key').value;
  if (!baseRaw || !model) {
    setSummary('bad', '缺少参数', '请填写模型 Base URL 和模型名。');
    return;
  }
  if (!key) {
    setSummary('bad', '缺少 API Key', '自检需要一次最小推理请求；Key 只在本页内存使用，不写入报告。');
    return;
  }
  let target;
  try { target = parseTarget(baseRaw); }
  catch (err) {
    setSummary('bad', '地址不可用', err.message);
    addRow('地址检查', 'danger', err.message, 'url');
    return;
  }
  state.target = { host: target.hostname, path: target.pathname, protocol: target.protocol.replace(':', '') };
  const pageIsHttps = location.protocol === 'https:';
  const mixed = pageIsHttps && target.protocol === 'http:';
  state.running = true;
  $('check-start').disabled = true;
  $('check-export').disabled = true;

  try {
    addRow('页面环境', 'ok',
      `来源 ${location.origin}；${window.isSecureContext ? '安全上下文' : '非安全上下文'}；浏览器在线 ${navigator.onLine ? '是' : '否'}`,
      'env');
    if (mixed) {
      addRow('混合内容', 'danger', '平台是 HTTPS，模型地址是 HTTP。浏览器会在请求前直接阻断；请改用 HTTPS 网关，或把平台与模型部署在同一内网 HTTP 环境。', 'mixed');
      setSummary('bad', '混合内容阻断', '这是浏览器策略，不是网关 CORS。请先解决协议一致性。');
      return;
    }
    addRow('目标地址', 'ok', `${state.target.protocol.toUpperCase()} · ${state.target.host}${state.target.path || '/'}`, 'target');
    if (!navigator.onLine) {
      addRow('网络状态', 'danger', '浏览器报告离线；请检查本机网络和系统代理。', 'online');
      setSummary('bad', '浏览器离线', '系统网络不可用，先恢复网络。');
      return;
    }

    const provider = providerInput === 'auto'
      ? (providerInput === 'auto' ? 'auto' : providerInput)
      : providerInput;
    // 自动模式先按 OpenAI 探测；若返回 404 再尝试 Anthropic。
    const protocolOrder = provider === 'auto' ? ['openai', 'anthropic'] : [provider];
    let endpoints = null;
    let chosenStyle = null;
    let modelsResp = null;
    let modelsStatus = 0;
    let modelsData = null;
    let corsAllowed = false;
    let networkReachable = false;
    let lastNetworkError = null;

    // 1) no-cors 网络探针：只要能拿到 opaque response，就说明 TCP/TLS/代理链路通。
    for (const proto of protocolOrder) {
      endpoints = endpointSet(target, proto);
      try {
        await timedFetch(endpoints.models, { method: 'GET', mode: 'no-cors', cache: 'no-store', credentials: 'omit' }, 12000);
        networkReachable = true;
        break;
      } catch (err) {
        lastNetworkError = err;
      }
    }
    if (!networkReachable) {
      addRow('网络探针（no-cors）', 'danger',
        `无法完成无凭证探针：${describeError(lastNetworkError)}。优先检查系统代理/PAC、DNS、证书或防火墙。`, 'network');
      setSummary('bad', '网络/代理不可达', '连 no-cors 探针都失败；这不是 CORS 问题。请检查浏览器代理设置与网关连通性。');
      return;
    }
    addRow('网络探针（no-cors）', 'ok', '已收到不透明响应，说明浏览器到网关的网络链路可达。', 'network');

    // 2) 带认证头的 GET 触发 CORS 预检并尝试读取 /models。
    for (const style of authStyles(provider, key)) {
      if (provider === 'auto' && style.protocol !== endpoints.protocol) continue;
      try {
        const resp = await timedFetch(endpoints.models, {
          method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store',
          headers: { ...style.headers, Accept: 'application/json' },
        }, 15000);
        modelsStatus = resp.status;
        corsAllowed = true;
        modelsData = await parseJsonSafe(resp);
        if ([200, 201].includes(resp.status)) {
          chosenStyle = style;
          const items = Array.isArray(modelsData?.data) ? modelsData.data : modelsData?.models;
          state.modelCount = Array.isArray(items) ? items.length : 0;
          addRow('CORS 预检与读取', 'ok',
            `带 ${style.name} 认证头的 GET 已通过浏览器 CORS，/models 返回 HTTP ${resp.status}；发现 ${state.modelCount || '若干'} 个模型。`, 'cors');
          break;
        }
        if ([401, 403].includes(resp.status)) {
          addRow(`认证样式 ${style.name}`, 'warn', `网关可达，但 /models 返回 HTTP ${resp.status}，该认证样式未被接受。`, `auth-models-${style.name}`);
          continue;
        }
        if (resp.status === 404) {
          chosenStyle = style;
          addRow('CORS 预检与读取', 'warn', `认证头已通过 CORS 预检，但 /models 返回 404；部分网关不提供列表接口，继续测推理。`, 'cors');
          break;
        }
        if (resp.status === 429) {
          addRow('CORS 预检与读取', 'warn', 'CORS 已通过，但网关返回 429 限流；说明链路可用。', 'cors');
          chosenStyle = style;
          break;
        }
        addRow('CORS 预检与读取', toneFromStatus(resp.status), `带 ${style.name} 认证头的 /models 返回 HTTP ${resp.status}。`, 'cors');
        chosenStyle = style;
        break;
      } catch (err) {
        addRow(`认证样式 ${style.name}`, 'danger',
          `带认证头的请求被浏览器阻断：${describeError(err)}。若 no-cors 探针成功，优先判断为 CORS 预检/响应头缺失。`, `cors-${style.name}`);
      }
    }

    if (!corsAllowed) {
      const pna = isPrivateHost(state.target.host) && pageIsHttps &&
        !isPrivateHost(location.hostname);
      addRow('现场判断', 'warn',
        (pna ? '目标是私网地址且页面在公网/安全上下文：除 CORS 外还可能触发 Private Network Access 预检；网关需返回 Access-Control-Allow-Private-Network。'
             : '网络层可达但浏览器读不到响应；请让网关放行平台 Origin，并允许 Authorization / api-key / x-api-key 等预检头。')
        + ' 若平台服务端的 Python 可以直连该网关，无需浏览器代理，直接在模型配置改回“服务端执行”即可；启用回退时平台也会在浏览器失败后自动改走 Python。', 'cors-diagnosis');
      setSummary('warn', 'CORS 拒绝', '网络/代理可达，但浏览器拒绝了跨域请求或预检。需要网关配置 CORS（包括 OPTIONS 预检和所需认证头），这不是网络故障。若 Python 可直连，改用服务端执行即可绕过这一限制。');
      return;
    }

    // 3) 最小推理测试，逐个认证样式尝试；成功一次即可。
    let inferOk = false;
    let inferWarn = false;
    let lastStatus = 0;
    for (const style of authStyles(provider, key)) {
      if (provider === 'auto' && style.protocol !== endpoints.protocol) continue;
      const proto = style.protocol === 'anthropic' ? 'anthropic' : 'openai';
      const set = endpointSet(target, proto);
      const payload = proto === 'anthropic'
        ? { model, max_tokens: 8, temperature: 0, messages: [{ role: 'user', content: '只回复 OK' }] }
        : { model, max_tokens: 8, temperature: 0, stream: false,
            messages: [{ role: 'user', content: '只回复 OK' }] };
      try {
        const resp = await timedFetch(set.inference, {
          method: 'POST', mode: 'cors', credentials: 'omit', cache: 'no-store',
          headers: { ...style.headers, 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(payload),
        }, 30000);
        lastStatus = resp.status;
        const data = await parseJsonSafe(resp);
        if ([200, 201].includes(resp.status)) {
          const reply = pickModelReply(data, proto);
          inferOk = true;
          addRow('最小推理', 'ok',
            `HTTP ${resp.status}，认证样式 ${style.name}${reply ? `，模型回复「${reply.slice(0, 24)}」` : ''}。`, 'infer');
          break;
        }
        if ([401, 403].includes(resp.status)) {
          addRow(`推理认证 ${style.name}`, 'warn', `推理端点返回 HTTP ${resp.status}，该认证样式未被接受。`, `infer-auth-${style.name}`);
          continue;
        }
        if (resp.status === 429) {
          inferWarn = true;
          addRow('最小推理', 'warn', '推理端点返回 429：网络与 CORS 均正常，但当前额度/速率被限流。', 'infer');
          break;
        }
        if (resp.status === 404) {
          addRow('最小推理', 'warn', '推理端点返回 404：Base URL 可能缺少 /v1 或网关未开放该路径。', 'infer');
          inferWarn = true;
          break;
        }
        addRow('最小推理', 'warn', `推理端点返回 HTTP ${resp.status}；链路可达，但网关未返回成功。`, 'infer');
        inferWarn = true;
        break;
      } catch (err) {
        addRow(`推理请求 ${style.name}`, 'danger',
          `浏览器在推理请求阶段失败：${describeError(err)}。若 GET 已通过 CORS，重点检查 POST 预检是否允许 Content-Type 与认证头。`, `infer-${style.name}`);
      }
    }

    if (inferOk) {
      setSummary('good', '浏览器代理链路可用', '网络、CORS 与认证均通过；平台配置该网关为浏览器代理模型后，请保持使用页面打开。');
    } else if (inferWarn) {
      setSummary('warn', '网络与 CORS 可用，但推理未成功', `网关可达，但最小推理返回 HTTP ${lastStatus || '—'}。请检查模型名、额度或网关策略。`);
    } else if (!chosenStyle) {
      setSummary('warn', '认证未通过', '网络与 CORS 可达，但所有认证样式都被 401/403 拒绝。请核对 Key 和网关认证方式。');
    } else {
      setSummary('warn', '链路可用但推理失败', '网络与 CORS 正常；请检查模型名、Base URL 路径与网关日志。');
    }
  } finally {
    state.running = false;
    $('check-start').disabled = false;
    $('check-export').disabled = false;
  }
}

function exportReport() {
  const report = {
    schema: 'bbu-browser-relay-selftest/1',
    generated_at: new Date().toISOString(),
    page: {
      origin: location.origin,
      protocol: location.protocol.replace(':', ''),
      secure_context: !!window.isSecureContext,
      online: !!navigator.onLine,
    },
    target: {
      host: state.target.host,
      path: state.target.path,
      protocol: state.target.protocol,
      model_count: state.modelCount,
    },
    conclusion: state.conclusion,
    checks: state.checks,
    // 明确不包含 API Key、请求头、模型响应正文或完整 Base URL。
    redaction: { api_key: true, request_headers: true, response_body: true, base_url_query: true },
  };
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `浏览器代理自检-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$('relay-check-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runChecks();
});
$('check-export').addEventListener('click', exportReport);
