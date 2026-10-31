'use strict';
const $ = id => document.getElementById(id);
let activeSession = null;
let activeProfile = 'ALEX';
let busy = false;
let requiresApproval = false;
const descriptions = {ALEX:'Engineering · code · systems', NOVA:'Architecture · planning · research', HERMES:'Local models · efficient execution'};

async function api(path, opts = {}) {
  const headers = {'Content-Type':'application/json'};
  const response = await fetch(path, {...opts, headers});
  const content = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(content.detail || `HTTP ${response.status}`);
  return content;
}
function element(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function banner(message) {
  $('banner').hidden = !message;
  $('banner').textContent = message || '';
}
function chooseProfile(profile) {
  activeProfile = profile;
  $('activeProfile').textContent = profile;
  $('profileName').textContent = profile + ' ●';
  $('profileDescription').textContent = descriptions[profile];
  document.querySelectorAll('.planet').forEach(p => p.classList.toggle('active', p.dataset.profile === profile));
}
async function loadSessions() {
  const sessions = await api('/api/sessions');
  const target = $('sessions'); target.replaceChildren();
  for (const s of sessions) {
    const btn = element('button', 'session-entry' + (s.id === activeSession ? ' selected' : ''));
    btn.append(element('span','session-icon','◈'),element('span','',s.title));
    btn.addEventListener('click', () => loadSession(s.id).catch(err => banner(err.message)));
    target.append(btn);
  }
}
async function createSession(profile = activeProfile) {
  if (busy) return;
  chooseProfile(profile);
  const session = await api('/api/sessions', {method:'POST',body:JSON.stringify({profile, title:'Нова мисия · '+profile})});
  return loadSession(session.id);
}
async function loadSession(id) {
  const detail = await api(`/api/sessions/${encodeURIComponent(id)}`);
  activeSession = id;
  chooseProfile(detail.session.profile);
  $('missionTitle').textContent = detail.session.title;
  window.localStorage.setItem('orbit_session', id);
  render(detail);
  await loadSessions();
}
function render(detail) {
  requiresApproval = Boolean(detail.approvals?.length);
  setBusy(busy);
  const target = $('messages'); target.replaceChildren();
  if (!detail.messages?.length) {
    const wrap = element('div','intro');
    const mark = element('div','intro-mark','◎');
    wrap.append(mark,element('div','intro-kicker','WELCOME TO THE ORBIT'));
    const h2=element('h2','');h2.append(document.createTextNode('Един контролен център.'));h2.append(document.createElement('br'));h2.append(element('em','','Неограничени възможности.'));
    wrap.append(h2,element('p','','ALEX, NOVA и HERMES са готови. Започни с първата си мисия.'));
    target.append(wrap);
  }
  for (const msg of detail.messages || []) {
    if (msg.role === 'system' || (msg.role === 'assistant' && !msg.content)) continue;
    if (msg.role === 'tool') {
      const box=element('details','tool-activity');
      box.append(element('summary','', '⌁ ИЗХОД ОТ ИНСТРУМЕНТ'));
      box.append(element('pre','', msg.content || ''));
      target.append(box); continue;
    }
    const wrap=element('article','chat-message '+msg.role);
    wrap.append(element('div','chat-caption',msg.role === 'user' ? 'YOU' : activeProfile));
    wrap.append(element('div','chat-bubble',msg.content || ''));
    target.append(wrap);
  }
  const approvals = $('approvals'); approvals.replaceChildren();
  for (const action of detail.approvals || []) {
    const box=element('div','approval');
    box.append(element('h3','',`⚠ Изисква разрешение: ${action.name}`));
    box.append(element('p','',`Файл: ${String(action.args.path || '')}`));
    const content=String(action.args.content || '');
    box.append(element('pre','',content));
    const buttons=element('div','approval-actions');
    for (const approved of [true,false]) {
      const btn=element('button',approved?'':'deny',approved?'ОДОБРИ':'ОТКАЖИ');
      btn.addEventListener('click',()=>resolveApproval(action.id,approved));
      buttons.append(btn);
    }
    box.append(buttons); approvals.append(box);
  }
  const audits=$('audit');audits.replaceChildren();
  for (const action of detail.audit || []) {
    const row=element('div','audit-row',action.name);
    row.append(element('span','',action.decision.toUpperCase()));
    audits.append(row);
  }
  if (!detail.audit?.length) audits.append(element('div','muted','Няма изпълнени инструменти.'));
  target.scrollTop=target.scrollHeight;
}
async function resolveApproval(id, approved) {
  if (busy) return;
  setBusy(true); banner('');
  try {
    const result = await api(`/api/approvals/${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify({approve:approved})});
    render(result);
  } catch (e) {
    banner(e.message);
    if (activeSession) await loadSession(activeSession);
  } finally {setBusy(false);}
}
function setBusy(value) {
  busy=value;$('send').disabled=value || requiresApproval;
  $('send').textContent=value?'РАБОТИ...':'ИЗПРАТИ ↗';
}
async function sendMessage(event) {
  event.preventDefault();
  const content=$('message').value.trim();
  if (!content || busy || requiresApproval) return;
  setBusy(true);banner('');
  try {
    if (!activeSession) {
      const session=await api('/api/sessions',{method:'POST',body:JSON.stringify({profile:activeProfile,title:'Нова мисия · '+activeProfile})});
      activeSession=session.id;
    }
    $('message').value='';
    const result=await api(`/api/sessions/${encodeURIComponent(activeSession)}/chat`,{method:'POST',body:JSON.stringify({content})});
    render(result);
    $('missionTitle').textContent=result.session.title;
    window.localStorage.setItem('orbit_session',activeSession);
    await loadSessions();
  } catch(e) {
    banner(e.message + ' · Провери ORBIT_BASE_URL и ORBIT_MODEL.');
    if (activeSession) await loadSession(activeSession);
  } finally {setBusy(false);$('message').focus();}
}
async function boot() {
  $('composer').addEventListener('submit',sendMessage);
  $('newSession').addEventListener('click',()=>createSession().catch(err=>banner(err.message)));
  $('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('composer').requestSubmit();}});
  document.querySelectorAll('.planet').forEach(planet=>planet.addEventListener('click',()=>createSession(planet.dataset.profile).catch(err=>banner(err.message))));
  try {
    const health=await api('/api/health');
    $('model').textContent=health.model;
    $('modelShort').textContent=health.model;
    $('workspace').textContent=health.workspace;
    $('provider').textContent='Not probed';
    $('version').textContent='v'+health.version;
    const remembered=window.localStorage.getItem('orbit_session');
    if (remembered) {
      try {await loadSession(remembered);return;}catch {window.localStorage.removeItem('orbit_session');}
    }
    await loadSessions();
  } catch(e){banner(`API недостъпно: ${e.message}`);}
}
boot();
