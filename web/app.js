(() => {
  'use strict';

  const fallbackDeliveries = [
    { at: '10:42:18', id: 'trc_8f21a4', event: 'delivery.accepted', detail: 'evt_1007 · webhook / billing', status: 'accepted' },
    { at: '10:42:19', id: 'trc_8f21a4', event: 'delivery.dispatched', detail: 'attempt 1 · 202 Accepted', status: 'accepted' },
    { at: '10:17:03', id: 'trc_6c93d1', event: 'delivery.retry_scheduled', detail: 'retry in 30s · timeout', status: 'warning' },
    { at: '09:58:51', id: 'trc_18ae77', event: 'delivery.accepted', detail: 'evt_1004 · webhook / ledger', status: 'accepted' }
  ];

  const endpoints = {
    health: {
      method: 'GET', path: '/health', category: 'SONDE SYSTÈME', title: 'Vérifier la disponibilité', auth: 'sans authentification',
      payload: '', notes: ['Donne un signal léger pour un load balancer.', 'Ne retourne ni secret ni détail interne.', 'Un statut non-200 est immédiatement exploitable.'],
      headers: 'Accept: application/json', local: { status: 'ok', service: 'courier-api', version: '1.0', time: new Date().toISOString() }
    },
    'demo-key': {
      method: 'POST', path: '/v1/keys/demo', category: 'ACCÈS DE DÉMONSTRATION', title: 'Créer une clé de démo', auth: 'sans clé préalable',
      payload: JSON.stringify({ label: 'playground', ttl_minutes: 30 }, null, 2), notes: ['La clé est volontairement courte et limitée.', 'Le format d’authentification reste standard : Bearer.', 'Le secret ne doit apparaître qu’une seule fois.'],
      headers: 'Content-Type: application/json', local: { key: 'demo_crr_4a9e7d…', expires_in_seconds: 1800, scope: ['deliveries:read', 'deliveries:write'] }
    },
    'delivery-create': {
      method: 'POST', path: '/v1/deliveries', category: 'ÉMISSION D’UN ÉVÉNEMENT', title: 'Déposer une livraison', auth: 'Bearer demo_…',
      payload: JSON.stringify({ event: 'invoice.paid', destination: 'https://example.test/webhooks/billing', payload: { invoice_id: 'inv_1042', amount_cents: 4200 }, idempotency_key: 'inv_1042-paid' }, null, 2),
      notes: ['La clé d’idempotence protège contre les doubles envois.', 'Le payload est validé avant mise en file.', 'La réponse 202 confirme la prise en charge, pas la réception finale.'],
      headers: 'Authorization: Bearer demo_…\nIdempotency-Key: inv_1042-paid\nContent-Type: application/json',
      local: { delivery_id: 'dly_7b26ea', trace_id: 'trc_7b26ea', status: 'accepted', next: 'queued for dispatch' }
    },
    'delivery-list': {
      method: 'GET', path: '/v1/deliveries', category: 'LECTURE ET AUDIT', title: 'Consulter les livraisons', auth: 'Bearer demo_…',
      payload: '', notes: ['La liste est limitée et triée pour rester rapide.', 'Les détails de destination peuvent être masqués.', 'Chaque ligne garde un trace id pour le support.'],
      headers: 'Authorization: Bearer demo_…\nAccept: application/json', local: { items: fallbackDeliveries.map(({ at, ...item }) => item), next_cursor: null }
    }
  };

  const ui = {
    state: document.getElementById('service-state'),
    baseUrl: document.getElementById('base-url'),
    category: document.getElementById('request-category'),
    title: document.getElementById('request-title'),
    auth: document.getElementById('request-auth'),
    method: document.getElementById('request-method'),
    path: document.getElementById('request-path'),
    payload: document.getElementById('payload'),
    notes: document.getElementById('request-notes'),
    headers: document.getElementById('header-example'),
    send: document.getElementById('send-request'),
    responseStatus: document.getElementById('response-status'),
    responseDot: document.getElementById('response-dot'),
    response: document.getElementById('response-output'),
    ledger: document.getElementById('trace-ledger'),
    caption: document.getElementById('trace-caption'),
    demoKey: document.getElementById('demo-key'),
    keyMode: document.getElementById('key-mode'),
    toast: document.getElementById('toast')
  };

  let selected = 'health';
  let key = '';
  let online = false;
  let toastTimer;

  const originLabel = window.location.origin === 'null' ? 'instance locale' : window.location.origin.replace(/^https?:\/\//, '');
  ui.baseUrl.textContent = originLabel;

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  function notify(message) {
    ui.toast.textContent = message;
    ui.toast.classList.add('visible');
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => ui.toast.classList.remove('visible'), 2800);
  }

  function setServiceState(state, text) {
    ui.state.classList.remove('online', 'offline');
    if (state) ui.state.classList.add(state);
    ui.state.querySelector('span').textContent = text;
  }

  function setResponse(value, label, tone = '') {
    ui.responseStatus.textContent = label;
    ui.responseDot.className = `response-dot ${tone}`.trim();
    ui.response.textContent = pretty(value);
  }

  function renderEndpoint(name) {
    selected = name;
    const config = endpoints[name];
    document.querySelectorAll('.endpoint').forEach((button) => button.classList.toggle('active', button.dataset.endpoint === name));
    document.querySelectorAll('.api-strip a[data-route]').forEach((link) => link.classList.toggle('active-route', link.dataset.route === name));
    ui.category.textContent = config.category;
    ui.title.textContent = config.title;
    ui.auth.textContent = config.auth;
    ui.method.textContent = config.method;
    ui.method.className = `method ${config.method.toLowerCase()}`;
    ui.path.textContent = config.path;
    ui.payload.value = config.payload;
    ui.payload.disabled = config.method === 'GET';
    ui.payload.placeholder = config.method === 'GET' ? 'Cette requête n’a pas de corps.' : '';
    ui.notes.innerHTML = config.notes.map((note) => `<li>${note}</li>`).join('');
    ui.headers.textContent = config.headers;
    setResponse({ tip: `Endpoint prêt : ${config.method} ${config.path}` }, 'Prêt à envoyer');
  }

  function normalizeDeliveries(payload) {
    const raw = payload?.deliveries || payload?.items || payload?.events || payload?.activity || [];
    if (!Array.isArray(raw) || raw.length === 0) return fallbackDeliveries;
    return raw.map((item, index) => ({
      at: item.at || item.time || item.created_at || item.timestamp || `trace ${index + 1}`,
      id: item.trace_id || item.traceId || item.id || `trc_${String(index + 1).padStart(6, '0')}`,
      event: item.event || item.type || item.name || 'delivery.recorded',
      detail: item.detail || item.destination || item.message || item.status || 'activité enregistrée',
      status: String(item.status || item.state || 'accepted').toLowerCase()
    }));
  }

  function renderLedger(deliveries, source = 'Scénario local') {
    if (!deliveries.length) {
      ui.ledger.innerHTML = '<p class="trace-empty">Aucune livraison à afficher pour le moment.</p>';
      ui.caption.textContent = source;
      return;
    }
    ui.ledger.innerHTML = deliveries.slice(0, 6).map((item) => {
      const status = /fail|error|reject/.test(item.status) ? 'error' : /retry|warn|pending/.test(item.status) ? 'warning' : '';
      const label = status === 'error' ? 'échec' : status === 'warning' ? 'à suivre' : item.status === 'accepted' ? 'accepté' : item.status;
      return `<article class="trace-row"><time>${escapeHtml(item.at)}</time><span class="trace-id">${escapeHtml(item.id)}</span><strong class="trace-event">${escapeHtml(item.event)}</strong><span class="trace-detail">${escapeHtml(item.detail)}</span><span class="trace-tag ${status}">${escapeHtml(label)}</span></article>`;
    }).join('');
    ui.caption.textContent = source;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[char]));
  }

  async function fetchJson(path, options = {}) {
    const response = await fetch(path, {
      headers: { Accept: 'application/json', ...(options.headers || {}) },
      ...options
    });
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!response.ok) {
      const error = new Error(data?.message || data?.detail || `HTTP ${response.status}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return { data, status: response.status };
  }

  async function callWithFallback(paths, options, playgroundPayload) {
    let lastError;
    for (const path of paths) {
      try { return await fetchJson(path, options); }
      catch (error) {
        lastError = error;
        if (error.status !== 404) throw error;
      }
    }
    if (!playgroundPayload) throw lastError;
    return fetchJson('/api/playground', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(playgroundPayload)
    });
  }

  async function sendRequest() {
    const config = endpoints[selected];
    ui.send.disabled = true;
    ui.send.textContent = 'Envoi…';
    let body;
    if (config.method !== 'GET' && ui.payload.value.trim()) {
      try { body = JSON.parse(ui.payload.value); }
      catch {
        setResponse({ error: 'Le corps doit être du JSON valide.' }, 'JSON à corriger', 'error');
        ui.send.disabled = false;
        ui.send.innerHTML = 'Envoyer <span aria-hidden="true">↗</span>';
        return;
      }
    }
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (key && config.auth !== 'sans authentification' && config.auth !== 'sans clé préalable') headers.Authorization = `Bearer ${key}`;
    if (selected === 'delivery-create' && body?.idempotency_key) headers['Idempotency-Key'] = body.idempotency_key;
    const options = { method: config.method, headers, ...(body ? { body: JSON.stringify(body) } : {}) };
    const playgroundPayload = { method: config.method, path: config.path, headers, body };

    try {
      const compatiblePaths = selected === 'demo-key'
        ? ['/api/demo', '/v1/demo', '/v1/keys/demo']
        : [config.path];
      const result = await callWithFallback(compatiblePaths, options, playgroundPayload);
      online = true;
      setServiceState('online', 'service accessible');
      setResponse(result.data, `${result.status} · réponse du service`, 'success');
      if (selected === 'demo-key') acceptDemoKey(result.data);
      if (selected === 'delivery-create' || selected === 'delivery-list') await refreshOverview();
    } catch (error) {
      online = false;
      setServiceState('offline', 'mode démo local');
      setResponse(config.local, 'Mode démo local · backend indisponible', '');
      if (selected === 'demo-key') acceptDemoKey(config.local);
      if (selected === 'delivery-create') {
        renderLedger([{ at: new Date().toLocaleTimeString('fr-FR'), id: 'trc_local', event: body?.event || 'delivery.accepted', detail: 'exemple local · la demande serait mise en file', status: 'accepted' }, ...fallbackDeliveries], 'Scénario local · backend non joint');
      }
    } finally {
      ui.send.disabled = false;
      ui.send.innerHTML = 'Envoyer <span aria-hidden="true">↗</span>';
    }
  }

  function acceptDemoKey(payload) {
    key = payload?.key || payload?.api_key || payload?.token || `demo_crr_${Math.random().toString(36).slice(2, 12)}`;
    const visible = key.length > 20 ? `${key.slice(0, 15)}••••${key.slice(-4)}` : key;
    ui.demoKey.textContent = `Bearer ${visible}`;
    ui.keyMode.textContent = 'active · 30 min';
    notify('Clé de démonstration prête pour le studio.');
  }

  async function createDemoKey() {
    const old = selected;
    renderEndpoint('demo-key');
    document.getElementById('studio').scrollIntoView({ behavior: 'smooth', block: 'start' });
    await new Promise((resolve) => window.setTimeout(resolve, 320));
    await sendRequest();
    if (old !== 'demo-key') window.setTimeout(() => renderEndpoint(old), 900);
  }

  async function runQuickDemo() {
    const button = document.getElementById('run-quick-demo');
    button.disabled = true;
    button.textContent = 'Parcours en cours…';
    document.getElementById('studio').scrollIntoView({ behavior: 'smooth', block: 'start' });
    try {
      renderEndpoint('demo-key');
      await sendRequest();
      renderEndpoint('delivery-create');
      await sendRequest();
      renderEndpoint('delivery-list');
      await sendRequest();
      notify(online ? 'Parcours complet exécuté sur le service.' : 'Parcours local affiché. Le backend est indisponible.');
    } finally {
      button.disabled = false;
      button.textContent = 'Rejouer le parcours complet ↗';
    }
  }

  async function refreshOverview() {
    try {
      const { data } = await fetchJson('/api/overview');
      online = true;
      setServiceState('online', 'service accessible');
      renderLedger(normalizeDeliveries(data), 'Données de l’API Courier · actualisées maintenant');
    } catch {
      try {
        const { data } = await fetchJson('/v1/deliveries', { headers: key ? { Authorization: `Bearer ${key}` } : {} });
        online = true;
        setServiceState('online', 'service accessible');
        renderLedger(normalizeDeliveries(data), 'Données de l’API Courier · endpoint /v1/deliveries');
      } catch {
        online = false;
        setServiceState('offline', 'mode démo local');
        renderLedger(fallbackDeliveries, 'Scénario local · connectez le backend pour voir les vraies traces');
      }
    }
  }

  async function copyText(value, message) {
    try {
      await navigator.clipboard.writeText(value);
      notify(message);
    } catch {
      notify('Copie non disponible dans ce navigateur.');
    }
  }

  document.querySelectorAll('.endpoint').forEach((button) => button.addEventListener('click', () => renderEndpoint(button.dataset.endpoint)));
  document.querySelectorAll('.api-strip a[data-route]').forEach((link) => link.addEventListener('click', () => renderEndpoint(link.dataset.route)));
  ui.send.addEventListener('click', sendRequest);
  document.getElementById('create-demo-key').addEventListener('click', createDemoKey);
  document.getElementById('run-quick-demo').addEventListener('click', runQuickDemo);
  document.getElementById('rotate-key').addEventListener('click', createDemoKey);
  document.getElementById('copy-key').addEventListener('click', () => copyText(key ? `Bearer ${key}` : ui.demoKey.textContent, 'Clé copiée.'));
  document.getElementById('copy-response').addEventListener('click', () => copyText(ui.response.textContent, 'Réponse copiée.'));
  document.getElementById('refresh-overview').addEventListener('click', refreshOverview);

  renderEndpoint('health');
  renderLedger(fallbackDeliveries, 'En attente de la première vérification du service');
  refreshOverview();
})();
