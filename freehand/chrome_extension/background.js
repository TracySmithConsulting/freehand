/**
 * FreeHand Browser Bridge — Background Service Worker
 *
 * This service worker bridges FreeHand's Python browser tool (over HTTP)
 * to Chrome's built-in debugger API (chrome.debugger).
 *
 * How it works:
 * 1. Chrome loads this extension
 * 2. Service worker starts an HTTP server on localhost:9223
 * 3. FreeHand sends JSON-RPC commands to localhost:9223
 * 4. We forward them to chrome.debugger.* for the active tab
 * 5. CDP response travels back to FreeHand
 *
 * Why not remote-debugging-port?
 *   The remote debugging port requires a consent prompt on every connect.
 *   chrome.debugger is built into Chrome and doesn't prompt — it's the same
 *   API that Chrome DevTools uses internally. The trade-off: we can only
 *   debug tabs this extension is permitted to see (same origin, or all
 *   hosts if granted <all_urls> host permission).
 */

const PORT = 9223;
const VERSION = "0.1.0";

// ── chrome.debugger state ────────────────────────────────────────────────────

/** Currently attached tab IDs. Map from tabId → debugging session id. */
const attachedTabs = new Map();

/** Pending CDP request callbacks keyed by CDP message ID. */
const pendingCallbacks = new Map();

/** Next CDP message ID counter. */
let _msgId = 1;

/** The currently focused tab ID (updated on tab activation). */
let activeTabId = null;

/** Start an HTTP server within the service worker context.
 *
 * Chrome service workers don't have a traditional `net` module, but we can
 * use the `chrome.debugger` API to send commands and receive events, and
 * we communicate with the Python side via chrome.storage.local + long-lived
 * message ports OR a simple approach using chrome.runtime.sendNativeMessage.
 *
 * However, the cleanest approach for FreeHand is a WebSocket-like pattern:
 * We open a long-lived port to a native messaging host, or we poll.
 *
 * ACTUAL IMPLEMENTATION — since service workers can't bind TCP sockets,
 * we use chrome.runtime.sendNativeMessage to communicate with a companion
 * native messaging host (a small Python helper) that binds port 9223.
 *
 * The native messaging host is distributed as freehand-nph (native messaging
 * host). See freehand/chrome_extension/README.md for setup.
 *
 * Here we handle the in-extension side of the bridge: tab events, CDP
 * command routing, and responses to the native messaging host.
 */

// ── CDP Event forwarding ────────────────────────────────────────────────────

/** Forward CDP events from a tab back to FreeHand via native messaging. */
function forwardCDPEvent(tabId, method, params) {
  const payload = JSON.stringify({ type: "event", tabId, method, params });
  chrome.storage.local.set({ lastEvent: { tabId, method, params, at: Date.now() } });
  // The native host polls chrome.storage.local for events — see README.md
}

/** Handle incoming CDP events from chrome.debugger. */
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId !== undefined) {
    forwardCDPEvent(source.tabId, method, params);
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId !== undefined) {
    attachedTabs.delete(source.tabId);
    forwardCDPEvent(source.tabId, "FreeHand.detached", { reason });
  }
});

// ── chrome.storage listener for commands from native host ───────────────────

/** The native messaging host (Python) sends commands to us via chrome.storage.
 *
 * Flow:
 *  1. FreeHand (Python) → HTTP POST localhost:9223 → native host
 *  2. Native host → chrome.storage.local "freehand_cmd" key
 *  3. Service worker reads "freehand_cmd", executes via chrome.debugger
 *  4. Result stored in "freehand_result" → native host reads it
 *  5. Native host HTTP-responds to FreeHand
 *
 * This polling bridge is necessary because service workers can't bind sockets.
 */
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.freehand_cmd) return;

  const cmd = changes.freehand_cmd.newValue;
  if (!cmd) return;

  executeCDPCommand(cmd).then((result) => {
    chrome.storage.local.set({ freehand_result: result });
  });
});

/** Execute a single CDP command from FreeHand. */
async function executeCDPCommand(cmd) {
  const { id, method, params, tabId } = cmd;

  // Attach to tab if not already attached
  if (!attachedTabs.has(tabId)) {
    try {
      await new Promise((resolve, reject) => {
        chrome.debugger.attach({ tabId }, VERSION, () => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else {
            attachedTabs.set(tabId, VERSION);
            resolve();
          }
        });
      });
    } catch (err) {
      return { id, error: { code: -32000, message: err.message } };
    }
  }

  // Execute the CDP command
  return new Promise((resolve) => {
    const cb = (source, m, p) => {
      // Ignore unrelated events
    };

    // Route to specific tab
    const tabSessionId = attachedTabs.get(tabId);
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (result, err) => {
      if (chrome.runtime.lastError) {
        resolve({ id, error: { code: -32000, message: chrome.runtime.lastError.message } });
      } else {
        resolve({ id, result });
      }
    });
  });
}

// ── Tab tracking ─────────────────────────────────────────────────────────────

chrome.tabs.onActivated.addListener(async (activeInfo) => {
  activeTabId = activeInfo.tabId;
  // Auto-attach to the newly activated tab
  if (!attachedTabs.has(activeTabId)) {
    try {
      await new Promise((resolve) => {
        chrome.debugger.attach({ tabId: activeTabId }, VERSION, () => {
          if (!chrome.runtime.lastError) {
            attachedTabs.set(activeTabId, VERSION);
          }
          resolve();
        });
      });
    } catch (_) {}
  }
});

chrome.tabs.onCreated.addListener((tab) => {
  if (tab.id !== undefined) {
    // Auto-attach new tabs
    chrome.debugger.attach({ tabId: tab.id }, VERSION, () => {
      if (!chrome.runtime.lastError) {
        attachedTabs.set(tab.id, VERSION);
      }
    });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  attachedTabs.delete(tabId);
  if (activeTabId === tabId) activeTabId = null;
});

// ── Startup: attach to all existing tabs ─────────────────────────────────────

chrome.runtime.onStartup.addListener(async () => {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (tab.id !== undefined && !tab.pinned) {
      try {
        await new Promise((resolve) => {
          chrome.debugger.attach({ tabId: tab.id }, VERSION, () => {
            if (!chrome.runtime.lastError) {
              attachedTabs.set(tab.id, VERSION);
            }
            resolve();
          });
        });
      } catch (_) {}
    }
  }
});

// ── Native messaging host health check ──────────────────────────────────────
// The native messaging host (Python helper) sends a ping on startup.
// We acknowledge it by writing our status to storage.

chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  if (message.type === "ping") {
    sendResponse({ type: "pong", version: VERSION, activeTabId, attachedTabs: attachedTabs.size });
  }
  return false;
});

// ── Icon badge: show number of attached tabs ────────────────────────────────

function updateBadge() {
  const count = attachedTabs.size;
  chrome.action.setBadgeText({ text: count > 0 ? String(count) : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
}

// Update badge every time a tab is attached/detached
const originalSet = attachedTabs.set.bind(attachedTabs);
attachedTabs.set = (k, v) => { const r = originalSet(k, v); updateBadge(); return r; };
attachedTabs.delete = (k) => { const r = attachedTabs.delete(k); updateBadge(); return r; };
attachedTabs.clear = () => { attachedTabs.clear(); updateBadge(); };

updateBadge();

console.log(`[FreeHand Bridge v${VERSION}] Started. Listening for commands via chrome.storage.local.`);
