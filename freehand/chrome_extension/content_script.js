/**
 * FreeHand Browser Bridge — Content Script
 *
 * Injected into every page. Handles:
 * - AXTree / DOM extraction (forwarded to background → FreeHand)
 * - Form state snapshots for checkpoint/restore
 * - Validation event listening (mutation observers on form submit)
 */

"use strict";

// ── Listen for commands from background service worker ───────────────────────

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message.type) {
    case "getAXTree":
      sendResponse({ axtree: getAxtree() });
      break;

    case "getFormState":
      sendResponse({ formState: getFormState() });
      break;

    case "setFormState":
      setFormState(message.state);
      sendResponse({ ok: true });
      break;

    case "getValidationSignals":
      sendResponse({ signals: collectValidationSignals() });
      break;

    default:
      sendResponse({ error: "Unknown message type: " + message.type });
  }
  return false; // synchronous response
});

// ── AXTree extraction ───────────────────────────────────────────────────────

/**
 * Build a plain-object AXTree from the current page.
 * Mirrors Playwright's aria_snapshot() output shape so the Python side
 * gets a consistent format regardless of how the page was accessed.
 */
function getAxtree() {
  const lines = [];
  function walk(node, depth = 0) {
    const indent = "  ".repeat(depth);
    if (node.tagName) {
      const role = getARIARole(node);
      const name = (node.getAttribute("aria-label") || node.textContent?.trim().slice(0, 80) || "").replace(/\s+/g, " ");
      const id = node.id ? `#${node.id}` : "";
      lines.push(`${indent}- [ref=e${node.tabIndex >= 0 ? node.tabIndex : 0}] ${role}: ${name}`);
      for (const child of node.children) {
        walk(child, depth + 1);
      }
    }
  }
  for (const el of document.body.children) {
    walk(el);
  }
  return lines.join("\n");
}

function getARIARole(el) {
  return el.getAttribute("role") || el.tagName.toLowerCase();
}

// ── Form state ─────────────────────────────────────────────────────────────

function getFormState() {
  const fields = [];
  const seen = new Set();

  document.querySelectorAll("input, select, textarea").forEach((el) => {
    if (!el.name || seen.has(el.name + el.type)) return;
    seen.add(el.name + el.type);

    const field = {
      name: el.name,
      type: el.type || el.tagName.toLowerCase(),
      tagName: el.tagName.toLowerCase(),
      value: el.value,
      checked: el.checked,
      selectedOptions: [],
    };

    if (el.tagName === "SELECT") {
      field.selectedOptions = Array.from(el.selectedOptions).map((o) => ({
        value: o.value,
        text: o.text,
      }));
    }

    fields.push(field);
  });

  return {
    url: location.href,
    title: document.title,
    fields,
    localStorage: Object.fromEntries(
      Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)])
    ),
  };
}

function setFormState(state) {
  if (!state || !state.fields) return;

  for (const field of state.fields) {
    const selector = `[name="${field.name}"]`;
    const el = document.querySelector(selector);
    if (!el) continue;

    if (field.type === "checkbox") {
      el.checked = field.checked;
    } else if (field.type === "radio") {
      const radio = document.querySelector(
        `[name="${field.name}"][value="${field.value}"]`
      );
      if (radio) radio.checked = true;
    } else {
      el.value = field.value;
      // Dispatch input event so Vue/React/reactives see the change
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
}

// ── Validation signal collection ───────────────────────────────────────────

function collectValidationSignals() {
  const signals = {
    ariaAlerts: [],
    errorMessages: [],
    fieldErrors: [],
    passFailKeywords: [],
    url: location.href,
    title: document.title,
  };

  // ARIA live regions
  document
    .querySelectorAll('[role="alert"], [role="status"], [aria-live]')
    .forEach((el) => {
      const text = el.innerText?.trim();
      if (text) signals.ariaAlerts.push(text);
    });

  // Error summary containers
  document
    .querySelectorAll(
      ".error-summary, .validation-summary, .alert-danger, .alert-error, " +
        ".form-errors, .errors, .validation-errors, .has-errors, .invalid-feedback"
    )
    .forEach((el) => {
      const text = el.innerText?.trim();
      if (text) signals.errorMessages.push(text);
    });

  // Field-level errors
  document
    .querySelectorAll(".is-invalid, .has-error, [aria-invalid='true'], .invalid")
    .forEach((el) => {
      const label =
        el.getAttribute("aria-label") ||
        document.querySelector(`label[for="${el.id}"]`)?.innerText ||
        el.name ||
        el.id ||
        el.tagName;
      signals.fieldErrors.push({
        field: label.slice(0, 100),
        tag: el.tagName.toLowerCase(),
        message: el.innerText?.trim().slice(0, 200),
      });
    });

  // Keyword scan
  const bodyText = document.body.innerText;
  const kwRe =
    /(?:correct|passed?|success|failed|wrong|incorrect|error|no\s+good|no\s+errors?|all\s+good)[^.!?\n]{0,80}/gi;
  const matches = bodyText.match(kwRe) || [];
  signals.passFailKeywords = [...new Set(matches.map((m) => m.trim()))].slice(0, 20);

  return signals;
}

// ── Mutation observer: detect form submission results ──────────────────────

let _validationObserver = null;

function startObservingValidation() {
  if (_validationObserver) return;
  _validationObserver = new MutationObserver(() => {
    const signals = collectValidationSignals();
    if (signals.ariaAlerts.length || signals.errorMessages.length || signals.fieldErrors.length) {
      browser.runtime.sendMessage({
        type: "validationDetected",
        signals,
      });
    }
  });
  _validationObserver.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "aria-invalid", "aria-live"],
  });
}

// Auto-start on page load
startObservingValidation();
