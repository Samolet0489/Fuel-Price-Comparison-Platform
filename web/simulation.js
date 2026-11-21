const $ = id => document.getElementById(id);
const names = {at: 'Austria', de: 'Germany', unassigned: 'Not assigned'};
let initialized = false;

function fillForm(scenario) {
  $('objective').value = scenario.objective;
  $('enabled').checked = scenario.enabled;
  for (const region of ['at', 'de']) {
    const env = scenario.locations[region];
    $(region + '-price').value = env.price_eur_kwh;
    $(region + '-carbon').value = env.carbon_g_kwh;
    $(region + '-available').checked = env.available;
  }
}

function row(body, values) {
  const tr = document.createElement('tr');
  for (const value of values) {
    const td = document.createElement('td');
    td.textContent = value;
    tr.appendChild(td);
  }
  body.appendChild(tr);
}

async function refresh() {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Controller request failed');
    const state = await response.json();
    if (!initialized && state.scenario) {
      fillForm(state.scenario);
      initialized = true;
    }
    $('target').textContent = names[state.target] || 'Waiting for placement';
    $('reason').textContent = state.reason + (state.wait_seconds ? ` (${state.wait_seconds}s)` : '');
    const active = (state.pods || []).filter(p => p.ready);
    $('observed').textContent = active.length ? [...new Set(active.map(p => names[p.region] || p.region))].join(' + ') : 'No ready worker';
    $('health').textContent = state.healthy ? `Controller healthy · updated ${new Date(state.observed_at * 1000).toLocaleTimeString()} · objective: ${state.scenario.objective}` : 'Controller unavailable — showing last observations';
    $('health').className = state.healthy ? 'muted good' : 'muted error';
    $('pods').replaceChildren();
    for (const pod of state.pods || []) row($('pods'), [pod.pod, pod.node, names[pod.region] || pod.region, pod.terminating ? 'Terminating' : pod.ready ? 'Ready' : 'Starting / pending']);
    $('events').replaceChildren();
    for (const event of state.events || []) row($('events'), [new Date(event.time * 1000).toLocaleTimeString(), names[event.target], event.reason]);
    if (!state.events?.length) row($('events'), ['—', '—', 'No placement changes since this controller started']);
  } catch (error) {
    $('health').textContent = 'Cannot reach controller. Observations may be stale.';
    $('health').className = 'muted error';
  }
}

$('scenario').addEventListener('submit', async event => {
  event.preventDefault();
  $('apply').disabled = true;
  $('feedback').textContent = 'Saving…';
  $('feedback').className = '';
  const scenario = {enabled: $('enabled').checked, objective: $('objective').value, locations: {}};
  for (const region of ['at', 'de']) scenario.locations[region] = {
    price_eur_kwh: Number($(region + '-price').value),
    carbon_g_kwh: Number($(region + '-carbon').value),
    available: $(region + '-available').checked
  };
  try {
    const response = await fetch('/api/scenario', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(scenario)});
    if (!response.ok) throw new Error('Could not save conditions. Check values and controller health.');
    $('feedback').textContent = 'Saved. Watching Kubernetes…';
    await refresh();
  } catch (error) {
    $('feedback').textContent = error.message;
    $('feedback').className = 'error';
  } finally { $('apply').disabled = false; }
});

for (const button of document.querySelectorAll('[data-preset]')) button.addEventListener('click', () => {
  const [region, objective] = button.dataset.preset.split('-');
  fillForm({enabled: true, objective, locations: {
    at: {price_eur_kwh: region === 'at' ? 0.10 : 0.35, carbon_g_kwh: region === 'at' ? 60 : 500, available: true},
    de: {price_eur_kwh: region === 'de' ? 0.10 : 0.35, carbon_g_kwh: region === 'de' ? 60 : 500, available: true}
  }});
  $('feedback').textContent = 'Preset selected. Apply conditions to run it.';
});

refresh();
setInterval(refresh, 2000);
