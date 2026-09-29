# FreeHand Browser Bridge — Chrome Extension

Exposes Chrome DevTools Protocol (CDP) to the FreeHand AI agent via a local bridge, enabling browser control with your existing session — cookies, logins, extensions, all preserved.

## Architecture

```
┌─────────────────────┐     HTTP POST      ┌──────────────────┐
│  FreeHand (Python)  │ ─── localhost ──── │  Native Messaging │  (freehand-nph.py)
│  browser.py tool    │    :9223/JSON-RPC  │  Host (NMH)       │
└─────────────────────┘                    └────────┬─────────┘
                                                     │ chrome.storage.local
                                                     │ polling
                                          ┌──────────▼──────────┐
                                          │  Chrome Extension    │
                                          │  (service worker     │
                                          │   + content script)  │
                                          └──────────┬──────────┘
                                                     │ chrome.debugger API
                                          ┌──────────▼──────────┐
                                          │  Your Chrome Browser │
                                          │  (real session)      │
                                          └──────────────────────┘
```

Chrome service workers can't bind TCP sockets. The **native messaging host** (NMH) is a small companion process that:
1. Listens on `localhost:9223` for JSON-RPC from FreeHand
2. Writes commands to `chrome.storage.local`
3. The extension's service worker reads them, executes via `chrome.debugger`
4. Results go back the same way

## Installation

### 1. Install the Extension

```bash
# Clone the repo (if you haven't already)
git clone https://github.com/TracySmithConsulting/freehand
cd freehand

# In Chrome:
#   1. Open chrome://extensions/
#   2. Enable "Developer mode" (top right)
#   3. Click "Load unpacked"
#   4. Select: freehand/chrome_extension/
```

### 2. Register the Native Messaging Host (one-time)

Create the native messaging manifest file:

**Windows:**
```json
// %LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\freehand.json
{
  "name": "freehand",
  "description": "FreeHand Browser Bridge",
  "path": "C:\\path\\to\\freehand\\freehand-nph.py",
  "type": "stdio"
}
```

**macOS:**
```json
// ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/freehand.json
{
  "name": "freehand",
  "description": "FreeHand Browser Bridge",
  "path": "/full/path/to/freehand/freehand-nph.py",
  "type": "stdio"
}
```

**Linux:**
```json
// ~/.config/google-chrome/native-messaging-hosts/freehand.json
{
  "name": "freehand",
  "description": "FreeHand Browser Bridge",
  "path": "/full/path/to/freehand/freehand-nph.py",
  "type": "stdio"
}
```

### 3. Start the NMH (or let Chrome start it automatically)

Chrome starts the NMH automatically when the extension first calls it. You can also run it manually for debugging:

```bash
python freehand-nph.py --port 9223
```

### 4. Verify

Click the FreeHand icon in Chrome's toolbar. The popup should show:
- **Status:** Connected
- **Active tabs:** (your open tabs)
- **Attached tabs:** 0+ (tabs the extension has connected to)

If it shows "FreeHand not running", ensure:
- FreeHand's server is running: `python server.py`
- The NMH is running (or let Chrome start it on first use)

## Usage

Once installed, FreeHand's browser tool automatically detects the extension and uses it:

```python
# FreeHand automatically upgrades from sandboxed Chromium
# to your real Chrome when the extension is detected
result = await browser.get_axtree("https://gotranscript.com/transcription-jobs/quiz?language=english%20%28uk%29")
# result["cdp_strategy"] == "chrome_extension"  ✓
```

All 14 browser tools (get_axtree, click, fill, navigate, get_validation_summary,
start_request_capture, get_captured_requests, save_checkpoint, restore_checkpoint,
...) work with the extension — no code changes needed.

## How It Works

### Content Script (`content_script.js`)
Injected into every page. Handles:
- AXTree extraction (Playwright-compatible format)
- Form state snapshots (for checkpoint/restore)
- Validation signal collection (error messages, ARIA alerts)
- Mutation observer to detect form submission results

### Background Service Worker (`background.js`)
Runs in the extension context. Handles:
- Tab tracking (onCreated, onActivated, onRemoved)
- `chrome.debugger.attach()` — connects to tabs
- Polls `chrome.storage.local` for commands from the NMH
- CDP command execution and response routing

### Native Messaging Host (`freehand-nph.py`)
Companion Python process. Two modes:
- **`--stdio`** (production): Chrome native messaging protocol
- **`--port 9223`** (debug): HTTP server that FreeHand calls directly

## Uninstalling

1. Remove the extension: `chrome://extensions/` → FreeHand Browser Bridge → Remove
2. Delete the native messaging host manifest:
   - Windows: `%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\freehand.json`
   - macOS: `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/freehand.json`
   - Linux: `~/.config/google-chrome/native-messaging-hosts/freehand.json`

## Security

- The extension only accesses tabs you explicitly allow via the toolbar popup
- CDP access requires the `<all_urls>` host permission — this is unavoidable for browser automation
- The NMH only accepts connections from `localhost` — no remote access
- Cookies and credentials are never transmitted anywhere — all execution is local

## Troubleshooting

### "FreeHand not running" in popup
- Ensure FreeHand's server is started: `cd /path/to/freehand && python server.py`
- Check that `localhost:9223` is not blocked by a firewall

### Extension not auto-attaching to tabs
- Open the extension popup and click **"Attach to Current Tab"**
- Or navigate to `chrome://inspect/#extensions` to see extension debug output

### NMH not starting automatically
- Run it manually: `python freehand-nph.py --port 9223`
- Check Chrome's extension logs: `chrome://extensions/` → FreeHand → "Errors"

### `cdp_strategy` still shows "sandboxed" after installing extension
- The extension was detected but the NMH bridge isn't responding
- Run `python freehand-nph.py --port 9223` manually in a terminal
- Check that the native messaging host manifest is in the correct location (see Step 2 above)
