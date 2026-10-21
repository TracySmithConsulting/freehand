/**
 * Replay driver for the real FreeHand index.html inline script.
 *
 * Runs the ACTUAL shipped JavaScript in a Node VM sandbox with:
 *  - a real linkedom document (real DOM behaviour, real innerHTML parsing,
 *    real querySelectorAll) from a tiny HTML skeleton mirroring index.html's
 *    static section IDs;
 *  - a fetch() stub wired to answers the pytest fixture recorded from the
 *    real FastAPI app — including `/api/integrations` returned in the
 *    CURRENT grouped shape (`[{service, connections: [...]}]`), with one
 *    connection whose `expires_at` is in the past;
 *  - localStorage/window/confirm/alert stubs.
 *
 * The driver then runs loadConnections() synchronously (via prom tick) and
 * answers one question the pytest suite asserts: does the Connections panel
 * render a Reconnect button for the expired connection, with the right
 * service/label? Any future refactor that regresses the grouped-payload
 * handling makes this driver throw or print missing=true.
 */(async () => {
  try {
    await main();
  } catch (e) {
    process.stderr.write('DRIVER-FATAL: ' + String(e && e.stack || e));
    process.exit(1);
  }
})();

async function main() {
  const { parseHTML } = require('linkedom');
  const fs = require('fs');

  const htmlPath = process.argv[2];
  const answersPath = process.argv[3];

  const htmlSource = fs.readFileSync(htmlPath, 'utf-8');

  const answers = JSON.parse(fs.readFileSync(answersPath, 'utf-8'));
  global.__answers = answers; // print() reads resultPath from here

  // Minimal skeleton carrying every element id the inline script touches at
  // load time. Mirrors index.html's static structure for those nodes.
  const skeleton = `<!DOCTYPE html>
<html><head></head><body>
  <header><h1>FreeHand</h1><div id="tier-display" aria-live="polite"></div></header>
  <nav aria-label="Main navigation"><ul></ul></nav>
  <main>
    <section id="command">
      <form id="command-form"><input id="command-input"><p class="error" id="command-error" role="alert"></p></form>
      <div id="command-response" aria-live="polite" class="hint"></div>
      <div id="agent-response" aria-live="polite"></div>
    </section>
    <section id="scribble">
      <form id="scribble-form"><textarea id="scribble-content"></textarea><p class="error" id="scribble-error" role="alert"></p></form>
      <button type="button" id="sweep-btn">Sweep Now</button>
    </section>
    <section id="tasks">
      <div id="task-runner-status" aria-live="polite"></div>
      <div id="task-list" aria-live="polite"></div>
      <form id="task-form">
        <input id="task-title"><p class="error" id="task-title-error" role="alert"></p>
        <input id="task-cron"><p class="error" id="task-cron-error" role="alert"></p>
      </form>
    </section>
    <section id="memory">
      <form id="memory-form"><input id="memory-input"><p class="error" id="memory-error" role="alert"></p><button type="submit">Search</button><button type="button" id="memory-sync-btn">Re-sync Vault Files</button></form>
      <button type="button" id="memory-sync-btn">Re-sync Vault Files</button>
      <div id="memory-results" aria-live="polite"></div>
      <div id="memory-files" aria-live="polite"></div>
      <div id="memory-viewer" aria-live="polite"></div>
    </section>
    <section id="connections">
      <div id="connections-list" aria-live="polite"></div>
    </section>
    <section id="agents">
      <div id="agents-status" aria-live="polite"></div>
      <div id="agents-imports" aria-live="polite"></div>
    </section>
    <section id="settings">
      <form id="tier-form">
        <select id="tier-select"><option>autonomous</option><option selected>semi_autonomous</option><option>god_mode</option></select>
        <button type="submit">Save Tier</button>
      </form>
      <input id="omniroute">
      <input id="api-key">
      <form id="settings-form"><input id="omniroute"><input id="api-key"><button type="submit">Save Settings</button></form>
      <form id="llm-form">
        <select id="llm-provider"><option selected>openai</option></select>
        <input id="llm-model" value="gpt-4o-mini">
        <input id="llm-api-key">
        <input id="llm-base-url">
        <button type="button" id="llm-save-btn">Save LLM Settings</button>
      </form>
      <button type="button" id="oauth-save-btn">Save OAuth Credentials</button>
      <input id="google-client-id"><input id="google-client-secret">
      <input id="microsoft-client-id"><input id="microsoft-client-secret"><input id="microsoft-tenant">
      <input id="zoom-client-id"><input id="zoom-client-secret">
      <input id="facebook-app-id"><input id="facebook-app-secret">
      <input id="instagram-app-id"><input id="instagram-app-secret">
      <input type="button" value="Remember" id="remember-key-btn">
      <input type="button" value="Forget" id="forget-key-btn">
    </section>
    <section id="gateways">
      <div id="gateway-status" aria-live="polite"></div>
      <form id="gateway-form">
        <input id="tg-token"><input id="tg-whitelist">
        <input id="slack-signing"><input id="slack-token">
        <input id="wa-phone"><p id="wa-code-hint"></p>
        <button id="wa-pair-btn" type="button">Pair</button>
        <button type="button" id="gateway-save-btn">Save Gateway Settings</button>
      </form>
    </section>
    <div id="status-region" role="status" aria-live="polite"></div>
    <div id="approvals-list" aria-live="polite"></div>
  </main>
</body></html>`;

  const { window: parserWindow, document } = parseHTML(skeleton);

  // This linkedom build exposes parseHTML's own `window` (no `Window`
  // constructor) — use it directly as the sandbox global.
  const sandboxWindow = parserWindow;
  sandboxWindow.document = document;

  // Sequential response queues: responses["X LOADS"] = [first, second, …]
  // pops per GET; the last value repeats. Covers load → action → reload
  // flows where the same endpoint must answer differently per call.
  const _queues = {};
  for (const k of Object.keys(answers.responses)) {
    if (k.endsWith(' LOADS')) _queues[k.slice(0, -6)] = answers.responses[k].slice();
  }
  const _fetchLog = [];
  sandboxWindow.fetch = async (url, options) => {
    const base = String(url).split('?')[0];
    const method = (options && options.method) || 'GET';
    const key = base + ' ' + method;
    _fetchLog.push(key);
    let answer;
    if (method === 'GET' && _queues[base] && _queues[base].length) {
      answer = _queues[base].length > 1 ? _queues[base].shift() : _queues[base][0];
    } else {
      answer = answers.responses[key] !== undefined ? answers.responses[key] : answers.responses[base];
    }
    if (answer === undefined) {
      return { ok: false, status: 404, json: async () => ({ detail: 'no replay answer for ' + key }) };
    }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(answer)) };
  };
  sandboxWindow.__fetchLog = _fetchLog;
  Object.assign(sandboxWindow, {
    localStorage: {
      _d: {},
      getItem(k) { return this._d[k] || null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
    },
    confirm: () => true,
    alert: () => {},
    prompt: () => null,
    open: () => ({}),
    location: { protocol: 'http:', hostname: '127.0.0.1', port: '8000', origin: 'http://127.0.0.1:8000' },
    EventSource: function () { return { addEventListener() {}, close() {} }; },
    scrollTo() {}, scrollIntoView() {},
  });
  sandboxWindow.window = sandboxWindow;
  sandboxWindow.globalThis = sandboxWindow;
  // Keep NODE's real timers/globalThis: don't let linkedom's stubbed
  // setTimeout/clearTimeout/etc. be hoisted into the sandbox, or every
  // `await new Promise(r => setTimeout(...))` in this driver AND in the
  // inline script would depend on a timer queue linkedom never pumps.
  for (const g of ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask', 'performance', 'structuredClone', 'Date', 'URLSearchParams']) {
    try { sandboxWindow[g] = globalThis[g]; } catch (e) {}
  }


  const vm = require('vm');
  const ctx = vm.createContext(sandboxWindow);

  // Also hoist the DOM globals the script uses bare.
  for (const g of ['document', 'fetch', 'localStorage', 'console', 'Event', 'CustomEvent', 'URLSearchParams', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval']) {
    try { ctx[g] = sandboxWindow[g] !== undefined ? sandboxWindow[g] : undefined; } catch (e) {}
  }
  ctx.console = console;

  // Extract the real inline script.
  const m = htmlSource.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('No inline <script> block found in index.html');
  const inlineJs = m[1];

  let loadError = null;
  try {
    vm.runInContext(inlineJs, ctx, { filename: 'index.html:inline' });
  } catch (e) {
    loadError = e;
  }



  // Let microtasks fire (all apiFetch chains), then answer.
  await new Promise((resolve) => setTimeout(resolve, 250));

  if (loadError) {
    print(JSON.stringify({ threw: String(loadError && loadError.stack || loadError) }));
    return; // print() exits
  }

  const doc = sandboxWindow.document;

  // Run optional scenario steps: [{set: [id, value]}, {click: selectorOrId}, {waitForStatus: prefix}, {eval: "js"}]
  // Each awaited tick after a click lets the click's fetch chain resolve.
  let stepError = null;
  try {
    for (const step of (answers.steps || [])) {
      if (step.set !== undefined) {
        const el = doc.getElementById(step.set[0]);
        if (!el) throw new Error('scenario set: #' + step.set[0] + ' not found');
        el.value = step.set[1];
      } else if (step.click !== undefined) {
        // Bare "#id" (no spaces) resolves by id; anything else is a selector.
        const sel = step.click;
        const targets = (/^#[\w-]+$/.test(sel)
          ? [doc.getElementById(sel.slice(1))]
          : [...doc.querySelectorAll(sel)]);
        const el = targets[0];
        if (!el) throw new Error('scenario click: ' + sel + ' not found');
        // Two cases:
        //  - <button type="submit"> inside a form → the shipped handlers
        //    hang off the FORM's submit event, and linkedom does not
        //    implement implicit submission from HTMLElement.click(), so
        //    dispatch the submit event explicitly.
        //  - anything else (type=button, or a button outside a form) →
        //    plain .click(), which fires its addEventListener handlers.
        let handled = false;
        if (el.tagName === 'BUTTON') {
          const type = (el.getAttribute('type') || 'submit').toLowerCase();
          const form = el.closest ? el.closest('form') : null;
          if (type === 'submit' && form) {
            form.dispatchEvent(new sandboxWindow.Event('submit', { bubbles: true, cancelable: true }));
            handled = true;
          }
        }
        if (!handled) {
          if (typeof el.onclick === 'function') {
            el.onclick({ preventDefault() {} });
          } else if (el.getAttribute && el.getAttribute('onclick')) {
            // Runtime-rendered markup (approvals cards) wires handlers via
            // onclick ATTRIBUTES; linkedom's .click() emits an event but
            // nothing listens for attribute handlers, so eval them here.
            vm.runInContext(el.getAttribute('onclick'), ctx, { filename: 'onclick-attr' });
          } else if (typeof el.click === 'function') {
            el.click();
          } else {
            throw new Error('scenario click: ' + step.click + ' has no click handler');
          }
        }
      } else if (step.waitForStatus !== undefined) {
        const prefix = step.waitForStatus;
        for (let i = 0; i < 40; i++) {
          const s = doc.getElementById('status-region').textContent;
          if (s.startsWith(prefix)) break;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        const s = doc.getElementById('status-region').textContent;
        if (prefix.startsWith && !s.includes(prefix) && !s.startsWith(prefix)) {
          throw new Error('waitForStatus "' + prefix + '" timed out (status: ' + s.slice(0, 80) + ')');
        }
      } else if (step.waitMs !== undefined) {
        await new Promise((resolve) => setTimeout(resolve, step.waitMs));
      }
      await new Promise((resolve) => setTimeout(resolve, 30));
    }
  } catch (e) {
    stepError = String(e && e.message || e);
  }
  await new Promise((resolve) => setTimeout(resolve, 150));

  // Verdict: either a fixed fixed map from answers.verdict (a JS expression
  // evaluated in the sandbox, returning an object) or the default
  // Reconnect-connections check (back-compat).
  let verdict = null;
  try {
    const expr = (answers && answers.verdict) || null;
    if (expr) {
      verdict = vm.runInContext('JSON.stringify((' + expr + '))', ctx, { filename: 'verdict' });
      verdict = JSON.parse(verdict);
    } else {
      const expired = doc.querySelectorAll('.connection-card.expired');
      const reconnects = [...doc.querySelectorAll('button')].filter((b) => b.textContent === 'Reconnect');
      const labels = [...doc.querySelectorAll('.connection-label')].map((e) => e.textContent);
      let firstReconnectOnclick = '';
      for (const b of doc.querySelectorAll('button')) {
        if (b.textContent === 'Reconnect') {
          firstReconnectOnclick = b.getAttribute('onclick') || b.getAttribute('aria-label') || '';
          break;
        }
      }
      verdict = {
        expiredCount: expired.length,
        reconnectCount: reconnects.length,
        labels,
        firstReconnect: firstReconnectOnclick,
      };
    }
  } catch (e) {
    print(JSON.stringify({ threw: 'verdict eval failed: ' + String(e && e.message || e) }));
    return;
  }
  verdict.stepError = stepError;
  if (answers.debug) {
    verdict.debugFetchLog = _fetchLog;
    verdict.debugTierDisplay = (doc.getElementById('tier-display') || {}).innerHTML || 'NO #tier-display';
    verdict.debugApprovalsList = (doc.getElementById('approvals-list') || {}).innerHTML || 'NO #approvals-list';
  }
  print(JSON.stringify(Object.assign({ threw: null }, verdict)));

}

function print(s) {
  process.stdout.write('RESULT:' + s + '\n');
  require('fs').writeFileSync(global.__answers.resultPath, s, 'utf-8');
  process.exit(0); // only exit AFTER the verdict is flushed
}
