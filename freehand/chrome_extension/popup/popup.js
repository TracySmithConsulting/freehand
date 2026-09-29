/**
 * FreeHand Browser Bridge — Popup Script
 */

const dot = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const serverStatus = document.getElementById("server-status");
const tabCount = document.getElementById("tab-count");
const attachedCount = document.getElementById("attached-count");

const dotInactive = document.querySelector(".dot");

async function checkHealth() {
  try {
    const resp = await fetch("http://localhost:9223/health", { signal: AbortSignal.timeout(2000) });
    if (resp.ok) {
      const data = await resp.json();
      dot.classList.add("active");
      dot.classList.remove("inactive");
      statusText.textContent = "Connected";
      serverStatus.textContent = `localhost:9223 (v${data.version || "?"})`;
    } else {
      setInactive(`Server error: ${resp.status}`);
    }
  } catch (_) {
    setInactive("FreeHand not running");
  }

  // Get tab counts from chrome.runtime
  try {
    const [tabs, storageData] = await Promise.all([
      chrome.tabs.query({}),
      new Promise((r) => chrome.storage.local.get(["activeTabId", "attachedTabs"], r)),
    ]);
    tabCount.textContent = tabs.length;
    const attached = storageData.attachedTabs;
    attachedCount.textContent = typeof attached === "number" ? attached : "?";
  } catch (_) {}
}

function setInactive(msg) {
  dot.classList.remove("active");
  dot.classList.add("inactive");
  statusText.textContent = msg;
  serverStatus.textContent = "Not connected";
}

document.getElementById("btn-attach").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id !== undefined) {
    try {
      await chrome.debugger.attach({ tabId: tab.id }, "0.1.0");
      statusText.textContent = `Attached to: ${tab.title?.slice(0, 30) || "tab"}`;
    } catch (e) {
      statusText.textContent = "Attach failed: " + (chrome.runtime.lastError?.message || e.message);
    }
  }
});

document.getElementById("btn-refresh").addEventListener("click", checkHealth);

document.getElementById("btn-help").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://github.com/TracySmithConsulting/freehand#chrome-extension" });
});

checkHealth();
