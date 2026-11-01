'use strict';
/* ORBIT console: STREAM (v0.2 parity) + MISSIONS (plan mode) + FLEET + SYSTEM
   (M3 runtime parity, honest 404 until PR #7 merges). CSP-safe: no eval, no CDN. */
const $ = id => document.getElementById(id);
const descriptions = {ORBIT:'Selected brain and user-approved real workers'};
let setupState = null;
let setupStage = 'brain';
let activeSession = null;
let activeProfile = 'ORBIT';
let busy = false;
let requiresApproval = false;
let activeTab = 'stream';
let activeMission = null;        // selected mission id
let missionsCache = [];
let fleetCache = [];
let clockTimer = null;

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
function shorten(text, n) {
  const s = String(text ?? '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function hhmmss(epochSeconds) {
  if (!epochSeconds) return '--:--:--';
  const d = new Date(epochSeconds * 1000);
  return d.toTimeString().slice(0, 8);
}
function ago(seconds) {
  if (seconds == null) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}
function banner(message, scope = 'banner') {
  const el = $(scope);
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || '';
}
function fmtMs(value) { return value == null ? 'n/a' : `${Math.round(value)} ms`; }

/* ================= TABS ================= */
function selectTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === name));
  document.querySelectorAll('.tab-page').forEach(page => page.classList.toggle('active', page.id === `tab-${name}`));
  if (name === 'missions') refreshMissions(true).catch(err => banner(err.message, 'missionBanner'));
  if (name === 'fleet') refreshFleet(true).catch(err => banner(err.message, 'fleetBanner'));
  if (name === 'system') refreshSystem(true).catch(err => banner(err.message, 'systemBanner'));
}

/* ================= STREAM (v0.2 logic, preserved) ================= */
function chooseProfile(profile) {
  activeProfile = profile;
  $('activeProfile').textContent = profile;
  // A conversational profile is never a real worker, and no planet is faked.
  if (!setupState?.brain) $('profileName').textContent = 'НЕ Е ИЗБРАН';
  $('profileDescription').textContent = String(setupState?.worker_ids?.length || 0) + ' разрешени агенти';
}
async function loadSessions() {
  const sessions = await api('/api/sessions');
  const target = $('sessions'); target.replaceChildren();
  for (const s of sessions) {
    const btn = element('button', 'session-entry' + (s.id === activeSession ? ' selected' : ''));
    btn.append(element('span','session-icon','◈'), element('span','',s.title));
    btn.addEventListener('click', () => loadSession(s.id).catch(err => banner(err.message)));
    target.append(btn);
  }
}
async function createSession(profile = activeProfile) {
  if (busy) return;
  if (!setupState?.targets_confirmed) { await openSetup(); return; }
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
    wrap.append(mark, element('div','intro-kicker','WELCOME TO THE ORBIT'));
    const h2 = element('h2','');
    h2.append(document.createTextNode('Един контролен център.'));
    h2.append(document.createElement('br'));
    h2.append(element('em','','Неограничени възможности.'));
    wrap.append(h2, element('p','','Моделът мисли. Само регистрирани и изрично разрешени workers могат да получават задачи.'));
    target.append(wrap);
  }
  for (const msg of detail.messages || []) {
    if (msg.role === 'system' || (msg.role === 'assistant' && !msg.content)) continue;
    if (msg.role === 'tool') {
      const box = element('details','tool-activity');
      box.append(element('summary','','⌁ ИЗХОД ОТ ИНСТРУМЕНТ'));
      box.append(element('pre','', msg.content || ''));
      target.append(box); continue;
    }
    const wrap = element('article','chat-message '+msg.role);
    wrap.append(element('div','chat-caption', msg.role === 'user' ? 'YOU' : activeProfile));
    wrap.append(element('div','chat-bubble', msg.content || ''));
    target.append(wrap);
  }
  const approvals = $('approvals'); approvals.replaceChildren();
  for (const action of detail.approvals || []) {
    const box = element('div','approval');
    box.append(element('h3','','⚠ Изисква разрешение: '+action.name));
    box.append(element('p','','Файл: '+String(action.args.path || '')));
    box.append(element('pre','', String(action.args.content || '')));
    const buttons = element('div','approval-actions');
    for (const approved of [true, false]) {
      const btn = element('button', approved ? '' : 'deny', approved ? 'ОДОБРИ' : 'ОТКАЖИ');
      btn.addEventListener('click', () => resolveApproval(action.id, approved));
      buttons.append(btn);
    }
    box.append(buttons); approvals.append(box);
  }
  const audits = $('audit'); audits.replaceChildren();
  for (const action of detail.audit || []) {
    const row = element('div','audit-row', action.name);
    const state = element('span', action.decision.toLowerCase(), action.decision.toUpperCase());
    row.append(state); audits.append(row);
  }
  if (!detail.audit?.length) audits.append(element('div','muted','Няма изпълнени инструменти.'));
  target.scrollTop = target.scrollHeight;
}
async function resolveApproval(id, approved) {
  if (busy) return;
  setBusy(true); banner('');
  try {
    const result = await api(`/api/approvals/${encodeURIComponent(id)}`, {method:'POST', body:JSON.stringify({approve:approved})});
    render(result);
  } catch (e) {
    banner(e.message);
    if (activeSession) await loadSession(activeSession);
  } finally { setBusy(false); }
}
function setBusy(value) {
  busy = value;
  $('send').disabled = value || requiresApproval;
  $('send').textContent = value ? 'РАБОТИ...' : 'ИЗПРАТИ ↗';
}
async function sendMessage(event) {
  event.preventDefault();
  const content = $('message').value.trim();
  if (!content || busy || requiresApproval) return;
  if (!setupState?.targets_confirmed) { await openSetup(); return; }
  setBusy(true); banner('');
  try {
    if (!activeSession) {
      const session = await api('/api/sessions', {method:'POST', body:JSON.stringify({profile:activeProfile, title:'Нова мисия · '+activeProfile})});
      activeSession = session.id;
    }
    $('message').value = '';
    const result = await api(`/api/sessions/${encodeURIComponent(activeSession)}/chat`, {method:'POST', body:JSON.stringify({content})});
    render(result);
    $('missionTitle').textContent = result.session.title;
    window.localStorage.setItem('orbit_session', activeSession);
    await loadSessions();
  } catch (e) {
    banner(e.message + ' · Провери ORBIT_BASE_URL и ORBIT_MODEL.');
    if (activeSession) await loadSession(activeSession);
  } finally { setBusy(false); $('message').focus(); }
}

/* ================= MISSIONS (plan mode supervisor) ================= */
const PIPELINE = ['GOAL','DISCOVER','MATCH','SCAFFOLD','ASSIGN','RUN','VERIFY','GAP','REPLAN','COMPLETE'];
function renderPipeline(stage) {
  const strip = $('pipelineStrip');
  strip.replaceChildren();
  strip.hidden = !stage;
  if (!stage) return;
  const alertStages = {'GAP':'alert','REPLAN':'alert','BLOCKED':'alert','CANCELLED':'alert'};
  let seen = stage === 'BLOCKED' || stage === 'CANCELLED';
  for (let i = 0; i < PIPELINE.length; i++) {
    const node = PIPELINE[i];
    const el = element('span', 'stage-node', node);
    if (node === stage) el.classList.add('now');
    else if (!seen && PIPELINE.indexOf(stage) > i) el.classList.add('done');
    if (node === stage || node === 'REPLAN') seen = true;
    if (alertStages[stage] && (node === stage || node === 'GAP' || node === 'REPLAN')) el.classList.add('alert');
    strip.append(el);
    if (i < PIPELINE.length - 1) strip.append(element('span','stage-node sep','▸'));
  }
}
async function refreshMissions(selectFirst = false) {
  const data = await api('/api/missions');
  missionsCache = data.missions || [];
  if (selectFirst && !activeMission && missionsCache.length) activeMission = missionsCache[0].id;
  if (activeMission && !missionsCache.some(m => m.id === activeMission)) {
    activeMission = missionsCache.length ? missionsCache[0].id : null;
  }
  renderMissionList();
  const total = missionsCache.length;
  const active = missionsCache.filter(m => !['COMPLETE','BLOCKED','CANCELLED'].includes(m.stage)).length;
  $('missionsActive').textContent = String(active);
  if (activeMission) await renderMissionDetail(activeMission);
  else renderPipeline(null);
}
function renderMissionList() {
  const target = $('missionList'); target.replaceChildren();
  if (!missionsCache.length) {
    target.append(element('div','muted','Няма мисии. Стартирай ▶ LAUNCH DEMO MISSION или създай custom.'));
    return;
  }
  for (const m of missionsCache) {
    const card = element('button', 'mission-card' + (m.id === activeMission ? ' selected' : ''));
    const head = element('strong','', shorten(m.goal, 92));
    const meta = element('div','task-meta');
    meta.append(element('span','chip st-'+(m.stage||''), m.stage + ' · it ' + m.iteration + '/' + m.max_iterations));
    const counts = Object.entries(m.task_counts || {}).map(([k,v]) => k.toLowerCase()+' '+v).join(' · ');
    meta.append(element('span','', counts || 'no tasks'));
    card.append(head, meta);
    card.addEventListener('click', () => { activeMission = m.id; renderMissionList(); renderMissionDetail(m.id).catch(err => banner(err.message, 'missionBanner')); });
    target.append(card);
  }
}
function renderMissionTasks(tasks) {
  const target = $('missionTasks'); target.replaceChildren();
  const counts = {};
  for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1;
  $('missionTaskCounts').textContent = tasks.length + ' tasks';
  if (!tasks.length) { target.append(element('div','muted','—')); return; }
  for (const t of tasks) {
    const card = element('div','task-card');
    const top = element('div','task-top');
    top.append(element('strong','', t.title));
    top.append(element('span','chip st-'+t.status, t.status));
    card.append(top);
    const meta = element('div','task-meta');
    meta.append(element('span','', 'attempt '+t.attempt));
    if (t.spec?.path) meta.append(element('span','', 'path: '+t.spec.path));
    if (t.worker_id) meta.append(element('span','', 'worker …'+String(t.worker_id).slice(-6)));
    card.append(meta);
    if (t.result_summary) card.append(element('div','task-summary', t.result_summary));
    target.append(card);
  }
}
function renderMissionCriteria(criteria) {
  const target = $('missionCriteria'); target.replaceChildren();
  if (!criteria.length) { target.append(element('div','muted','—')); return; }
  for (const c of criteria) {
    const row = element('div','criteria-item');
    const mark = element('span','cri-mark '+(c.satisfied ? 'ok' : 'fail'), c.satisfied ? '✓' : '✗');
    const body = element('div','cri-body');
    body.append(element('strong','', c.text || c.kind));
    body.append(element('code','', '['+c.kind+'] '+c.path));
    body.append(element('em','', c.evidence || ''));
    row.append(mark, body); target.append(row);
  }
}
function renderMissionEvents(events) {
  const target = $('missionEvents'); target.replaceChildren();
  for (const e of events) {
    const row = element('div','event-row '+e.kind);
    row.append(element('span','ev-stage', e.stage));
    row.append(element('span','ev-time', hhmmss(e.created_at)));
    row.append(element('span','', e.message));
    target.append(row);
  }
  if (!events.length) target.append(element('div','muted','Няма събития.'));
}
async function renderMissionDetail(id) {
  const m = await api(`/api/missions/${encodeURIComponent(id)}`);
  renderPipeline(m.stage);
  renderMissionTasks(m.tasks);
  renderMissionCriteria(m.criteria);
  renderMissionEvents(m.events);
}
async function launchDemoMission() {
  banner('', 'missionBanner');
  try {
    const view = await api('/api/lab/demo/mission', {method:'POST', body:'{}'});
    activeMission = view.id;
    await refreshMissions();
    selectTab('missions');
    banner('Демо мисия стартирана: ' + shorten(view.goal, 80), 'missionBanner');
  } catch (e) { banner(e.message, 'missionBanner'); }
}
async function launchDemoWorker() {
  banner('', 'missionBanner'); banner('', 'fleetBanner');
  try {
    const info = await api('/api/lab/demo/worker', {method:'POST', body:'{}'});
    banner('Mock worker стартиран (PID ' + info.demo_pid + '). Регистрацията минава през pairing API — виж FLEET таба.', 'fleetBanner');
    if (activeTab === 'fleet') refreshFleet(true).catch(() => {});
  } catch (e) { banner(e.message, 'fleetBanner'); }
}
async function cancelActiveMission() {
  if (!activeMission) return;
  try {
    await api(`/api/missions/${encodeURIComponent(activeMission)}/cancel`, {method:'POST', body:JSON.stringify({note:'cancelled from UI'})});
  } catch (e) { /* 404-safe? cancel accepts empty body */ }
  await refreshMissions();
}
async function createCustomMission(event) {
  event.preventDefault();
  banner('', 'missionBanner');
  const goal = $('missionGoal').value.trim();
  let criteria;
  try { criteria = JSON.parse($('missionCriteria').value); }
  catch { banner('Criteria не е валиден JSON.', 'missionBanner'); return; }
  if (!goal || !Array.isArray(criteria) || !criteria.length) {
    banner('Цел + поне един criterion са задължителни.', 'missionBanner'); return;
  }
  try {
    const view = await api('/api/missions', {method:'POST', body:JSON.stringify({goal, criteria})});
    activeMission = view.id;
    $('missionGoal').value = ''; $('missionCriteria').value = '';
    await refreshMissions();
    banner('Мисия създадена: ' + shorten(view.goal, 70), 'missionBanner');
  } catch (e) { banner(e.message, 'missionBanner'); }
}

/* ================= FLEET ================= */
async function refreshFleet(selectFirst = false) {
  const [fleet, pairings] = await Promise.all([
    api('/api/workers'),
    api('/api/lab/pairings').catch(() => ({pairings: []})),
  ]);
  fleetCache = fleet.workers || [];
  $('workerCount').textContent = String(fleetCache.length);
  renderFleetWorkers();
  renderPairingLog(pairings.pairings || []);
  $('fleetLive').textContent = String(fleetCache.filter(w => w.status === 'LIVE').length);
  $('fleetTotal').textContent = String(fleetCache.length);
  if (selectFirst) void 0;
}
function renderFleetWorkers() {
  const target = $('workerList'); target.replaceChildren();
  if (!fleetCache.length) {
    target.append(element('div','muted','Няма работници. FLEET → GENERATE PAIRING, после `orbit-mock-worker --pairing <code>`.'));
    return;
  }
  for (const w of fleetCache) {
    const card = element('div','worker-card');
    const top = element('div','task-top');
    top.append(element('strong','', w.name || w.id || 'worker'));
    top.append(element('span','chip st-'+w.status, w.status));
    card.append(top);
    const meta = element('div','task-meta');
    meta.append(element('span','', (w.kind || '?') + ' · v' + (w.version || '?')));
    meta.append(element('span','', (w.host || '127.0.0.1')));
    const age = element('span','heartbeat-age','❤ ' + ago(w.heartbeat_age));
    meta.append(age);
    card.append(meta);
    if (w.capabilities?.length) {
      const caps = element('div','caps');
      for (const c of w.capabilities.slice(0, 6)) caps.append(element('span','cap', c));
      card.append(caps);
    }
    if (w.assigned_task) {
      card.append(element('div','task-summary','▸ ' + w.assigned_task.title + ' · ' + w.assigned_task.status));
    }
    target.append(card);
  }
}
let pairingCountdown = null;
async function createPairing() {
  banner('', 'fleetBanner');
  try {
    const pairing = await api('/api/lab/pairing', {method:'POST', body:'{}'});
    $('pairingPanel').hidden = false;
    $('pairingCode').textContent = pairing.pairing_code;
    const deadline = pairing.expires_at;
    clearInterval(pairingCountdown);
    const tick = () => {
      const left = deadline - Date.now() / 1000;
      $('pairingExpiry').textContent = left > 0 ? `изтича след ${Math.ceil(left)}s` : 'ИЗТЕКЛО';
      if (left <= 0) clearInterval(pairingCountdown);
    };
    tick();
    pairingCountdown = setInterval(tick, 500);
    await refreshFleet(true).catch(() => {});
  } catch (e) { banner(e.message, 'fleetBanner'); }
}
function renderPairingLog(pairings) {
  const target = $('pairingLog'); target.replaceChildren();
  if (!pairings.length) { target.append(element('div','muted','Няма pairings.')); return; }
  for (const p of pairings) {
    const row = element('div','pairing-row');
    row.append(element('span','', '№' + String(p.pairing_id).slice(0, 8)));
    row.append(element('span','', hhmmss(p.created_at)));
    let chip = 'OPEN';
    if (p.used) chip = 'USED';
    else if (p.burned) chip = 'BURNED';
    else if (p.expired) chip = 'EXPIRED';
    const label = chip === 'EXPIRED' ? 'EXPIRED · unused' : chip;
    row.append(element('span','chip st-'+chip, label));
    if (p.failed_attempts) row.append(element('span','', 'failed ' + p.failed_attempts));
    target.append(row);
  }
}

/* ================= SYSTEM ================= */
async function refreshSystem() {
  const health = await api('/api/health');
  renderHealth(health);
  runtimePanel(await api('/api/runtime').catch(() => null));
}
function renderHealth(health) {
  const target = $('systemHealth'); target.replaceChildren();
  const rows = [
    ['ENGINE STATUS', health.status === 'ok' ? 'OPERATIONAL' : String(health.status), health.status === 'ok' ? 'green' : 'red'],
    ['VERSION', health.version || '--', 'violet'],
    ['MODEL', health.model || '--', ''],
    ['BASE URL', health.base_url || '--', ''],
    ['WORKSPACE', health.workspace || '--', ''],
    ['WRITE POLICY', String(health.write_policy || 'human_approval').toUpperCase(), 'amber'],
    ['PROVIDER', String(health.provider_status || 'not_checked'), health.provider_status === 'ok' ? 'green' : ''],
    ['MOTOR', 'orbit.planmode.supervisor', 'violet'],
  ];
  for (const [label, value, tone] of rows) {
    const metric = element('div','metric');
    metric.append(element('span','', label), element('strong', tone, String(value)));
    target.append(metric);
  }
  $('modelShort').textContent = setupState?.brain?.model || 'ИЗБЕРИ МОЗЪК';
  $('model').textContent = setupState?.brain?.model || '--';
  $('workspace').textContent = health.workspace || '--';
}
function runtimePanel(data) {
  const target = $('runtimePanel'); target.replaceChildren();
  if (!data) {
    const offline = element('div','runtime-offline');
    offline.textContent = '/api/runtime не е налична в тази версия (PR #7 M3 не е обединен) — панелът честно показва 404 вместо фалшиви метрики. Това не е грешка.';
    target.append(offline);
    return;
  }
  const providers = Array.isArray(data.providers) ? data.providers : [];
  target.append(element('div','task-meta',
    'PROBED MODEL ENDPOINTS · ' + providers.length + ' · NOT THE ACTIVE CHAT ROUTE'));
  if (!providers.length) {
    target.append(element('div','runtime-offline','Няма конфигуриран inference provider.'));
    return;
  }
  for (const provider of providers) {
    const card = element('div','mission-col');
    card.append(element('div','aside-label',
      (provider.provider || 'unknown') + ' · ' + (provider.kind || '?')));
    const tone = provider.status === 'READY' ? 'green' :
      provider.status === 'ERROR' || provider.status === 'OFFLINE' ? 'red' : 'amber';
    const facts = [
      ['PROBE STATUS', provider.status || 'NOT_CHECKED'],
      ['MODEL CONFIG', provider.model || 'unavailable'],
      ['LATENCY', typeof provider.latency_ms === 'number' ? provider.latency_ms.toFixed(1) + ' ms' : 'unavailable'],
      ['DISCOVERED MODELS', (provider.available_models || []).join(', ') || 'unavailable'],
      ['ERROR', provider.error || 'none']
    ];
    for (const [name, value] of facts) {
      const row = element('div','metric');
      row.append(element('span','',name), element('strong',name === 'PROBE STATUS' ? tone : '', String(value)));
      card.append(row);
    }
    target.append(card);
  }
  target.append(element('div','task-meta',
    'READY означава, че API отговаря на списъка с модели — не че моделът е зареден или чатът го използва.'));
}
async function probeRuntime() {
  banner('', 'systemBanner');
  try {
    const result = await api('/api/runtime/probe', {method:'POST', body:'{}'});
    runtimePanel(result);
    banner('Model endpoint probe completed (not an active chat switch).', 'systemBanner');
  } catch (e) {
    banner(e.status === undefined && /HTTP 404/.test(String(e.message)) ?
      '/api/runtime/probe отсъства — PR #7 (M3) не е обединен.' : 'Probe: ' + e.message, 'systemBanner');
  }
}
async function benchRuntime() {
  banner('', 'systemBanner');
  try {
    const result = await api('/api/runtime/benchmark', {method:'POST', body:'{}'});
    const rate = typeof result.tokens_per_s === 'number' ? result.tokens_per_s + ' tokens/s' : 'throughput unavailable';
    banner('BENCHMARK · ' + (result.provider || '?') + ' · ' + rate, 'systemBanner');
  } catch (e) {
    banner(/HTTP 404/.test(String(e.message)) ?
      '/api/runtime/benchmark отсъства — PR #7 (M3) не е обединен.' : 'Benchmark: ' + e.message, 'systemBanner');
  }
}

/* ================= FLEET SUMMARY (right rail) ================= */
async function refreshSummary() {
  const fleet = await api('/api/workers').catch(() => ({workers: []}));
  const missions = await api('/api/missions').catch(() => ({missions: []}));
  const workers = fleet.workers || [];
  const ms = missions.missions || [];
  const liveNow = workers.filter(w => w.status === 'LIVE').length;
  $('fleetLive').textContent = String(workers.filter(w => w.status === 'LIVE' || w.status === 'IDLE').length);
  $('fleetTotal').textContent = String(workers.length);
  $('missionsActive').textContent = String(ms.filter(m => !['COMPLETE','BLOCKED','CANCELLED'].includes(m.stage)).length);
  void liveNow;
}

/* ================= CLOCK DRIVER (2s poll per active tab) ================= */
function startClock() {
  clearInterval(clockTimer);
  clockTimer = setInterval(() => {
    refreshSummary().catch(() => {});
    if (activeTab === 'missions') {
      refreshMissions().catch(() => {});      // 404-safe: keep last known UI on error
    } else if (activeTab === 'fleet') {
      refreshFleet().catch(() => {});
    } else if (activeTab === 'system') {
      refreshSystem().catch(() => {});
    }
  }, 2000);
}


/* === FIRST RUN: select actual model brain, then an explicit worker allowlist === */
function showSetupError(message) {
  $('setupError').hidden = !message;
  $('setupError').textContent = message || '';
}
async function openSetup(preferred = null) {
  $('setupScreen').hidden = false;
  setupState = await api('/api/setup/status');
  renderSelectedBrain();
  if (preferred === 'brain' || !setupState.brain) return showBrainStep();
  if (preferred === 'workers' || !setupState.targets_confirmed) return showWorkerStep();
  $('setupScreen').hidden = true;
}
function renderSelectedBrain() {
  const brain = setupState?.brain;
  $('profileName').textContent = brain?.model || 'НЕ Е ИЗБРАН';
  $('profileDescription').textContent = String(setupState?.worker_ids?.length || 0) + ' разрешени агенти';
  $('modelShort').textContent = brain?.model || 'ИЗБЕРИ МОЗЪК';
  $('model').textContent = brain?.model || '--';
  $('provider').textContent = brain?.base_url || 'Няма избран мозък';
  const planets = $('selectedWorkerOrbits');
  planets.replaceChildren();
  for (const id of setupState?.worker_ids || []) {
    const dot = element('span', 'selected-dot');
    dot.title = id;
    planets.append(dot);
  }
}
async function showBrainStep() {
  setupStage = 'brain';
  $('setupHeading').textContent = '01 / Избери мозък';
  $('setupSubtitle').textContent = 'Избери модел, който ще мисли и планира. Показваме действително отговарящи model API-та, без да зареждаме GPU автоматично.';
  $('setupStageOne').classList.add('current');
  $('setupStageTwo').classList.remove('current');
  $('setupBrainList').hidden = false;
  $('setupTargetList').hidden = true;
  $('setupContinue').hidden = true;
  $('setupBack').hidden = true;
  showSetupError('');
  const target = $('setupBrainList');
  target.replaceChildren(element('p', 'setup-empty', 'Откривам локалните model API-та…'));
  try {
    const result = await api('/api/setup/brains');
    target.replaceChildren();
    if (!result.brains?.length) target.append(element('p','setup-empty',
      'Няма намерен модел. Стартирай model API и натисни ОБНОВИ.'));
    for (const brain of result.brains || []) {
      const btn = element('button', 'setup-option');
      btn.type = 'button';
      btn.disabled = !brain.online;
      btn.append(element('strong','',brain.model),element('code','',brain.base_url),
                 element('small','',brain.online ? 'API ONLINE · ИЗБЕРИ' : 'OFFLINE / НЕПОТВЪРДЕН'));
      if (brain.online) btn.addEventListener('click', async () => {
        try {
          setupState = await api('/api/setup/brain',
            {method:'POST',body:JSON.stringify({id:brain.id})});
          renderSelectedBrain();
          await showWorkerStep();
        } catch (e) { showSetupError(e.message); }
      });
      target.append(btn);
    }
  } catch (e) { showSetupError('Грешка при проверка на моделите: ' + e.message); }
}
async function showWorkerStep() {
  if (!setupState?.brain) return showBrainStep();
  setupStage = 'workers';
  $('setupHeading').textContent = '02 / Кого да командва?';
  $('setupSubtitle').textContent = 'Избери само реално свързаните агенти. Ако няма нито един, продължи с мозъка; можеш да ги свържеш по-късно през FLEET.';
  $('setupStageOne').classList.remove('current');
  $('setupStageTwo').classList.add('current');
  $('setupBrainList').hidden = true;
  $('setupTargetList').hidden = false;
  $('setupContinue').hidden = false;
  $('setupBack').hidden = false;
  showSetupError('');
  const target = $('setupTargetList');
  target.replaceChildren(element('p','setup-empty','Проверявам реалните worker регистрации…'));
  try {
    const result = await api('/api/setup/workers');
    target.replaceChildren();
    if (!result.workers?.length) target.append(element('p','setup-empty',
      '0 СВЪРЗАНИ АГЕНТА · ORBIT започва празен. Нито ALEX, нито NOVA, нито HERMES са фиктивно регистрирани.'));
    for (const worker of result.workers || []) {
      const row = element('label','setup-option setup-worker');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'setup-worker-checkbox';
      checkbox.value = worker.id;
      checkbox.checked = setupState.worker_ids?.includes(worker.id) || false;
      const desc = element('span','');
      desc.append(element('strong','',worker.name+' · '+worker.host));
      desc.append(element('small','',worker.kind+' · '+worker.status+' · '+(worker.capabilities||[]).join(', ')));
      row.append(checkbox,desc);
      target.append(row);
    }
    $('setupContinue').textContent = result.workers?.length ?
      'ЗАПАЗИ РАЗРЕШЕНИТЕ АГЕНТИ' : 'ПРОДЪЛЖИ БЕЗ АГЕНТИ';
  } catch (e) { showSetupError('Грешка при проверка на агентите: ' + e.message); }
}
async function saveTargetSelection() {
  try {
    const ids = [...document.querySelectorAll('.setup-worker-checkbox:checked')].map(x=>x.value);
    setupState = await api('/api/setup/workers',
      {method:'POST',body:JSON.stringify({worker_ids:ids})});
    renderSelectedBrain();
    $('setupScreen').hidden = true;
    await refreshSummary().catch(()=>{});
    await refreshSystem().catch(()=>{});
  } catch (e) { showSetupError(e.message); }
}

/* ================= BOOT ================= */
async function boot() {
  try {
    const health = await api('/api/health');
    $('version').textContent = 'v' + (health.version || '0.1.0');
  } catch { /* banner already handles stream errors */ }

  // tab wiring
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => selectTab(btn.dataset.tab)));

  // STREAM (v0.2 wiring preserved)
  $('composer').addEventListener('submit', sendMessage);
  $('newSession').addEventListener('click', () => createSession().catch(err => banner(err.message)));
  // Brain and workers are selected explicitly, not via hardcoded agent planets.
  $('changeBrain').addEventListener('click', () => openSetup('brain').catch(e => showSetupError(e.message)));
  $('changeTargets').addEventListener('click', () => openSetup('workers').catch(e => showSetupError(e.message)));
  $('setupRefresh').addEventListener('click', () => (setupStage === 'brain' ? showBrainStep() : showWorkerStep()));
  $('setupBack').addEventListener('click', showBrainStep);
  $('setupContinue').addEventListener('click', saveTargetSelection);

  // MISSIONS
  $('launchDemoMission').addEventListener('click', () => launchDemoMission().catch(err => banner(err.message, 'missionBanner')));
  $('launchDemoWorker').addEventListener('click', () => launchDemoWorker().catch(err => banner(err.message, 'fleetBanner')));
  $('customMissionForm').addEventListener('submit', createCustomMission);

  // FLEET
  $('createPairing').addEventListener('click', () => createPairing().catch(err => banner(err.message, 'fleetBanner')));

  // SYSTEM
  $('probeRuntime').addEventListener('click', () => probeRuntime().catch(err => banner(err.message, 'systemBanner')));
  $('benchRuntime').addEventListener('click', () => benchRuntime().catch(err => banner(err.message, 'systemBanner')));

  // Show a neutral empty screen instead of auto-activating an old ALEX/NOVA profile.
  try { await loadSessions(); }
  catch (e) { banner('Backend не отговаря: ' + e.message); }
  await refreshSummary().catch(() => {});
  // Explicitly confirm brain then targets on EVERY desktop launch.
  // Remembered choices never silently authorize a previous agent fleet.
  await openSetup('brain').catch(e => showSetupError(e.message));
  startClock();
}
window.addEventListener('DOMContentLoaded', boot);
