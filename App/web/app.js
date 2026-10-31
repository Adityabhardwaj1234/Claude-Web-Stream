const API = '';
let userId = localStorage.getItem('userId') || '';
let apiKey = localStorage.getItem('apiKey') || '';
let currentConv = null;

const $ = (id) => document.getElementById(id);

function authHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (apiKey) h['Authorization'] = 'Bearer ' + apiKey;
  return h;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function addMessage(role, text = '') {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  $('messages').appendChild(wrap);
  $('messages').scrollTop = $('messages').scrollHeight;
  return bubble;
}

$('login-btn').onclick = async () => {
  const body = { username: $('username').value, password: $('password').value };
  try {
    const r = await api('/auth/login', { method: 'POST', body: JSON.stringify(body) });
    userId = r.id;
    localStorage.setItem('userId', userId);
    if (r.api_key) { apiKey = r.api_key; localStorage.setItem('apiKey', apiKey); $('api-key').value = apiKey; }
    $('user-id').textContent = 'user: ' + userId;
    await refreshKeys();
    await loadModels();
    await refreshConvs();
    $('status-pill').textContent = 'online';
  } catch (e) { alert(e.message); }
};

$('save-key-btn').onclick = () => {
  apiKey = $('api-key').value.trim();
  localStorage.setItem('apiKey', apiKey);
  refreshKeys().catch(() => {});
};

async function refreshKeys() {
  if (!apiKey) return;
  try {
    const ks = await api('/keys');
    const ul = $('keys-list'); ul.innerHTML = '';
    ks.forEach(k => {
      const li = document.createElement('li');
      li.textContent = k.prefix + ' (' + k.name + ')';
      ul.appendChild(li);
    });
  } catch {}
}

async function loadModels() {
  try {
    const ms = await api('/claude/models');
    const sel = $('model-select'); sel.innerHTML = '';
    (ms.data || []).forEach(m => {
      const o = document.createElement('option'); o.value = m.id; o.textContent = m.id;
      if (m.active === false) o.disabled = true;
      sel.appendChild(o);
    });
  } catch {}
}

async function refreshConvs() {
  if (!apiKey) return;
  try {
    const cs = await api('/conversations');
    const ul = $('conv-list'); ul.innerHTML = '';
    cs.forEach(c => {
      const li = document.createElement('li');
      li.textContent = (c.title || c.id.slice(0,8)) + ' · ' + c.model;
      li.onclick = () => { loadConversation(c.id); };
      // delete button
      const del = document.createElement('button');
      del.textContent = '×';
      del.style.marginLeft = '6px';
      del.onclick = async (e) => { e.stopPropagation(); if (confirm('Delete?')) { await api('/conversations/' + c.id, { method: 'DELETE' }); refreshConvs(); if (currentConv === c.id) { currentConv = null; $('messages').innerHTML=''; } } };
      li.appendChild(del);
      ul.appendChild(li);
    });
  } catch {}
}

async function loadConversation(id) {
  currentConv = id;
  $('messages').innerHTML = '';
  $('thinking-panel').textContent = '';
  $('tools-panel').textContent = '';
  $('artifacts-list').innerHTML = '';
  try {
    const c = await api('/conversations/' + id);
    (c.messages || []).forEach(m => {
      const bubble = addMessage(m.role === 'user' ? 'user' : 'assistant', m.content || '');
      if (m.thinking) { /* could show in panel */ }
    });
    if (c.model) $('model-select').value = c.model;
    if (c.effort) $('effort-select').value = c.effort;
    try {
      const as = await api('/conversations/' + id + '/artifacts');
      const ul = $('artifacts-list'); ul.innerHTML = '';
      as.forEach(a => {
        const li = document.createElement('li');
        const link = document.createElement('a');
        link.href = '/artifacts/' + a.id + '/content';
        link.textContent = a.filename + ' (' + a.size + ' bytes)';
        link.target = '_blank';
        li.appendChild(link);
        ul.appendChild(li);
      });
    } catch {}
  } catch (e) { console.error(e); }
}

$('new-chat-btn').onclick = () => {
  currentConv = null;
  $('messages').innerHTML = '';
  $('thinking-panel').textContent = '';
  $('tools-panel').textContent = '';
  $('artifacts-list').innerHTML = '';
};

$('send-btn').onclick = async () => {
  const prompt = $('prompt').value.trim();
  if (!prompt) return;
  $('prompt').value = '';
  addMessage('user', prompt);
  const bubble = addMessage('assistant', '');
  $('thinking-panel').textContent = '';
  $('tools-panel').textContent = '';
  const model = $('model-select').value;
  const effort = $('effort-select').value;
  const body = { model, messages: [{ role: 'user', content: prompt }], stream: true, effort, conversation_id: currentConv };
  try {
    const res = await fetch(API + '/v1/chat/completions', { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
    if (!res.ok) { bubble.textContent = 'Error: ' + await res.text(); return; }
    if (res.headers.get('X-Conversation-Id')) currentConv = res.headers.get('X-Conversation-Id');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const p of parts) {
        for (const line of p.split('\n')) {
          if (line.startsWith('data: ')) {
            const d = line.slice(6);
            if (d === '[DONE]') continue;
            try {
              const j = JSON.parse(d);
              const delta = j.choices?.[0]?.delta || {};
              if (delta.reasoning) {
                $('thinking-panel').textContent += delta.reasoning;
                $('thinking-panel').scrollTop = $('thinking-panel').scrollHeight;
              }
              if (delta.content) bubble.textContent += delta.content;
              if (delta.tool_calls) $('tools-panel').textContent += JSON.stringify(delta.tool_calls) + '\n';
              if (j.choices?.[0]?.finish_reason === 'stop') {
                if (currentConv) {
                  try {
                    const as = await api('/conversations/' + currentConv + '/artifacts');
                    const ul = $('artifacts-list'); ul.innerHTML = '';
                    as.forEach(a => {
                      const li = document.createElement('li');
                      const link = document.createElement('a');
                      link.href = '/artifacts/' + a.id + '/content';
                      link.textContent = a.filename + ' (' + a.size + ' bytes)';
                      link.target = '_blank';
                      li.appendChild(link);
                      ul.appendChild(li);
                    });
                  } catch {}
                }
              }
              $('messages').scrollTop = $('messages').scrollHeight;
            } catch {}
          }
        }
      }
    }
    await refreshConvs();
  } catch (e) { bubble.textContent = 'Error: ' + e.message; }
};

async function refreshArtifacts() {
  // best-effort: list not available; keep last
}

async function refreshUsage() {
  if (!apiKey || !userId) return;
  try {
    const u = await api('/usage');
    $('usage-panel').textContent = u.map(x => x.day + ': ' + x.requests + ' req').join('\n');
  } catch {}
}

// init
if (apiKey) { $('api-key').value = apiKey; }
if (userId) { $('user-id').textContent = 'user: ' + userId; }
loadModels();
refreshConvs();
refreshUsage();
fetch(API + '/health').then(r=>r.json()).then(()=>{ $('status-pill').textContent='online'; $('status-pill').style.borderColor='#10a37f'; }).catch(()=>{});