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
      <form id="memory-form"><input id="memory-input"><p class="error" id="memory-error" role="alert"></p></form>
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
      <div id="tier-display" aria-live="polite"></div>
      <form id="tier-form">
        <select id="tier-select"><option>autonomous</option><option selected>semi_autonomous</option><option>god_mode</option></select>
      </form>
      <input id="omniroute">
      <input id="api-key">
      <form id="settings-form"><button type="submit">Save Settings</button></form>
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

  sandboxWindow.fetch = async (url, options) => {
    const base = String(url).split('?')[0];
    const method = (options && options.method) || 'GET';
    const key = base + ' ' + method;
    const answer = answers.responses[key] !== undefined
      ? answers.responses[key]
      : answers.responses[base];
    if (answer === undefined) {
      return { ok: false, status: 404, json: async () => ({ detail: 'no replay answer for ' + base }) };
    }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(answer)) };
  };
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
  } else {
    const doc = sandboxWindow.document;
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
    print(JSON.stringify({
      threw: null,
      expiredCount: expired.length,
      reconnectCount: reconnects.length,
      labels,
      firstReconnect: firstReconnectOnclick,
    }));
  }

}

function print(s) {
  process.stdout.write('RESULT:' + s + '\n');
  require('fs').writeFileSync(global.__answers.resultPath, s, 'utf-8');
  process.exit(0); // only exit AFTER the verdict is flushed
}
