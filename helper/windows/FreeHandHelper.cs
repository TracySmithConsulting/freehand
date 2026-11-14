// FreeHand Helper for Windows (stage 1: Look).
//
// Lets FreeHand, wherever it runs (Docker on this PC, or a server), see
// this computer through Windows accessibility (what screen readers use) and
// screenshots, after asking the person here. See
// docs/design/computer-helper.md.
//
// Built with the C# compiler that comes with Windows (build.cmd), so it
// needs nothing installed: .NET Framework 4.8 is part of Windows 10 and 11.
// C# 5 only: no $"" strings, no ?. operator.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("FreeHand Helper")]
[assembly: System.Reflection.AssemblyProduct("FreeHand")]
[assembly: System.Reflection.AssemblyVersion("0.1.0.0")]

namespace FreeHand.Helper
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--selftest")
                return SelfTest(args[1]);
            bool first;
            using (var mutex = new Mutex(true, "FreeHandHelperSingleInstance", out first))
            {
                if (!first)
                {
                    // Already running (perhaps hidden by the clock): bring
                    // its window back instead of starting a second one.
                    Native.PostMessage(Native.HWND_BROADCAST, Native.ShowMessage, IntPtr.Zero, IntPtr.Zero);
                    return 0;
                }
                Native.SetProcessDPIAware();
                ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm());
            }
            return 0;
        }

        // FreeHandHelper.exe --selftest out.json: look at this computer
        // without connecting or asking, and write what was seen. For
        // checking a build; the person runs it themselves, so it's allowed.
        static int SelfTest(string outFile)
        {
            Native.SetProcessDPIAware();
            var result = new Dictionary<string, object>();
            try
            {
                result["windows"] = Look.Windows();
                var w = Look.Window("");
                result["look"] = w;
                var shot = Look.Screenshot("");
                result["screenshot"] = new Dictionary<string, object> { { "window", shot["window"] }, { "width", shot["width"] }, { "height", shot["height"] },
                    { "bytes", shot["image"].ToString().Length } };
                result["ok"] = true;
            }
            catch (Exception ex) { result["ok"] = false; result["error"] = ex.ToString(); }
            File.WriteAllText(outFile, new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 }.Serialize(result));
            return result.ContainsKey("error") ? 1 : 0;
        }
    }

    static class Native
    {
        public const uint MB_OK = 0x0, MB_YESNO = 0x4, MB_ICONQUESTION = 0x20, MB_ICONINFORMATION = 0x40,
            MB_TOPMOST = 0x40000, MB_SETFOREGROUND = 0x10000, MB_DEFBUTTON2 = 0x100;
        public const int IDYES = 6;
        public const int WM_HOTKEY = 0x0312;
        public const uint MOD_ALT = 0x1, MOD_CONTROL = 0x2, MOD_NOREPEAT = 0x4000;
        public const uint VK_ESCAPE = 0x1B;
        public const uint GW_HWNDNEXT = 2;

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);
        [DllImport("user32.dll")] public static extern bool RegisterHotKey(IntPtr hWnd, int id, uint mods, uint vk);
        [DllImport("user32.dll")] public static extern bool UnregisterHotKey(IntPtr hWnd, int id);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint cmd);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern uint RegisterWindowMessage(string name);
        [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr w, IntPtr l);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        public static readonly IntPtr HWND_BROADCAST = new IntPtr(0xffff);
        public static readonly uint ShowMessage = RegisterWindowMessage("FreeHandHelperShowWindow");
    }

    // Settings in %APPDATA%\FreeHand Helper\settings.json. The token is
    // encrypted for this Windows user (DPAPI), so copying the file to
    // another account or PC doesn't copy the access.
    class Settings
    {
        public string Server = "http://localhost:8000";
        public string DeviceId = "";
        public string Name = "";
        public string Token = "";

        static string Folder { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "FreeHand Helper"); } }
        static string FilePath { get { return Path.Combine(Folder, "settings.json"); } }

        public bool Paired { get { return Token.Length > 0; } }

        public static Settings Load()
        {
            var s = new Settings();
            try
            {
                var d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(FilePath));
                s.Server = Str(d, "server", s.Server);
                s.DeviceId = Str(d, "device_id", "");
                s.Name = Str(d, "name", "");
                var sealedToken = Str(d, "token", "");
                if (sealedToken.Length > 0)
                    s.Token = Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(sealedToken), null, DataProtectionScope.CurrentUser));
            }
            catch (Exception) { }
            return s;
        }

        public void Save()
        {
            Directory.CreateDirectory(Folder);
            var sealedToken = Token.Length > 0
                ? Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(Token), null, DataProtectionScope.CurrentUser))
                : "";
            var d = new Dictionary<string, object> { { "server", Server }, { "device_id", DeviceId }, { "name", Name }, { "token", sealedToken } };
            File.WriteAllText(FilePath, new JavaScriptSerializer().Serialize(d));
        }

        static string Str(Dictionary<string, object> d, string key, string fallback)
        {
            object v;
            return d.TryGetValue(key, out v) && v != null ? v.ToString() : fallback;
        }
    }

    class MainForm : Form
    {
        readonly Settings settings = Settings.Load();
        readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };
        readonly SemaphoreSlim sendLock = new SemaphoreSlim(1, 1);

        Label statusLabel;
        TextBox serverBox, codeBox, nameBox;
        Button pairButton, stopButton, resumeButton, unpairButton;
        ListBox logList;
        GroupBox pairGroup;
        NotifyIcon trayIcon;

        volatile bool stopped;      // the person pressed Stop
        volatile bool lookAllowed;  // allowed to look for this session
        volatile bool quitting;
        bool hotkeyOk;
        CancellationTokenSource connection;

        public MainForm()
        {
            Text = "FreeHand Helper";
            Width = 640; Height = 560;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10f);

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, Padding = new Padding(12), AutoScroll = true };
            Controls.Add(layout);

            statusLabel = new Label { AutoSize = true, MaximumSize = new Size(590, 0), AccessibleName = "Status", Margin = new Padding(0, 0, 0, 10) };
            layout.Controls.Add(statusLabel);

            stopButton = new Button { Text = "Stop FreeHand (Ctrl+Alt+Escape)", AutoSize = true };
            stopButton.Click += delegate { StopHelping("You pressed Stop."); };
            resumeButton = new Button { Text = "Let FreeHand help again", AutoSize = true, Enabled = false };
            resumeButton.Click += delegate { Resume(); };
            var buttons = new FlowLayoutPanel { AutoSize = true };
            buttons.Controls.Add(stopButton);
            buttons.Controls.Add(resumeButton);
            layout.Controls.Add(buttons);

            pairGroup = new GroupBox { Text = "Pair with FreeHand", AutoSize = true, Dock = DockStyle.Top, Padding = new Padding(10) };
            var pairLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, AutoSize = true };
            serverBox = AddField(pairLayout, "FreeHand's address:", settings.Server);
            nameBox = AddField(pairLayout, "Name for this computer:", settings.Name.Length > 0 ? settings.Name : Environment.MachineName);
            codeBox = AddField(pairLayout, "Pairing code from FreeHand:", "");
            pairButton = new Button { Text = "Pair", AutoSize = true };
            pairButton.Click += delegate { Pair(); };
            pairLayout.Controls.Add(new Label());
            pairLayout.Controls.Add(pairButton);
            pairGroup.Controls.Add(pairLayout);
            layout.Controls.Add(pairGroup);
            AcceptButton = pairButton;

            var logLabel = new Label { Text = "What FreeHand asked for:", AutoSize = true, Margin = new Padding(0, 10, 0, 0) };
            layout.Controls.Add(logLabel);
            logList = new ListBox { Width = 590, Height = 160, AccessibleName = "What FreeHand asked for", HorizontalScrollbar = true };
            layout.Controls.Add(logList);

            unpairButton = new Button { Text = "Unpair this computer", AutoSize = true };
            unpairButton.Click += delegate { Unpair(); };
            layout.Controls.Add(unpairButton);

            var menu = new ContextMenuStrip();
            menu.Items.Add("Stop FreeHand", null, delegate { StopHelping("You pressed Stop."); });
            menu.Items.Add("Open the FreeHand Helper", null, delegate { ShowMe(); });
            menu.Items.Add("Quit", null, delegate { quitting = true; Close(); });
            trayIcon = new NotifyIcon { Icon = SystemIcons.Application, Text = "FreeHand Helper", Visible = true, ContextMenuStrip = menu };
            trayIcon.DoubleClick += delegate { ShowMe(); };

            Load += delegate { Startup(); };
            FormClosing += OnClosing;
        }

        static TextBox AddField(TableLayoutPanel panel, string label, string value)
        {
            var l = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left };
            var box = new TextBox { Text = value, Width = 320, AccessibleName = label.TrimEnd(':') };
            panel.Controls.Add(l);
            panel.Controls.Add(box);
            return box;
        }

        void Startup()
        {
            hotkeyOk = Native.RegisterHotKey(Handle, 1, Native.MOD_CONTROL | Native.MOD_ALT | Native.MOD_NOREPEAT, Native.VK_ESCAPE);
            if (!hotkeyOk) Log("Ctrl+Alt+Escape is in use by another program, so use the Stop button instead.");
            UpdateView();
            if (settings.Paired) Connect(); else codeBox.Focus();
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == Native.WM_HOTKEY) StopHelping("You pressed Ctrl+Alt+Escape.");
            if (m.Msg == (int)Native.ShowMessage) ShowMe();
            base.WndProc(ref m);
        }

        void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (!quitting && e.CloseReason == CloseReason.UserClosing)
            {
                // Closing the window keeps the Helper running by the clock.
                e.Cancel = true;
                Hide();
                trayIcon.ShowBalloonTip(4000, "FreeHand Helper", "Still running. Right-click its icon by the clock to stop or quit.", ToolTipIcon.Info);
                return;
            }
            if (hotkeyOk) Native.UnregisterHotKey(Handle, 1);
            if (connection != null) connection.Cancel();
            trayIcon.Visible = false;
        }

        void ShowMe() { Show(); WindowState = FormWindowState.Normal; Activate(); Native.SetForegroundWindow(Handle); }

        void Ui(Action a)
        {
            if (IsDisposed) return;
            if (InvokeRequired) { try { BeginInvoke(a); } catch (Exception) { } } else a();
        }

        void Log(string text)
        {
            Ui(delegate
            {
                logList.Items.Insert(0, DateTime.Now.ToString("HH:mm") + "  " + text);
                while (logList.Items.Count > 200) logList.Items.RemoveAt(logList.Items.Count - 1);
            });
        }

        string connectionState = "";

        void SetConnectionState(string text) { connectionState = text; Ui(UpdateView); }

        void UpdateView()
        {
            pairGroup.Visible = !settings.Paired;
            unpairButton.Visible = settings.Paired;
            stopButton.Enabled = settings.Paired && !stopped;
            resumeButton.Enabled = settings.Paired && stopped;
            string text;
            if (!settings.Paired)
                text = "Not paired yet. In FreeHand, open Settings, Help on my computer, and press Add a computer. Type the code it shows below, then press Pair.";
            else if (stopped)
                text = "Stopped. FreeHand can't see or do anything on this computer. Press \"Let FreeHand help again\" when you want it back.";
            else if (lookAllowed)
                text = "FreeHand is helping: it can see your screen for this session. Press Ctrl+Alt+Escape or Stop at any time.";
            else
                text = "Ready. FreeHand will ask you before it looks at anything. " + connectionState;
            statusLabel.Text = text;
            Text = lookAllowed && !stopped ? "FreeHand Helper - helping" : "FreeHand Helper";
            trayIcon.Text = lookAllowed && !stopped ? "FreeHand Helper: helping" : "FreeHand Helper";
        }

        // ── Pairing ──────────────────────────────────────────────────

        void Pair()
        {
            var server = serverBox.Text.Trim().TrimEnd('/');
            if (!server.StartsWith("http://") && !server.StartsWith("https://")) server = "http://" + server;
            var body = json.Serialize(new Dictionary<string, object> { { "code", codeBox.Text.Trim() }, { "name", nameBox.Text.Trim() } });
            pairButton.Enabled = false;
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(server + "/api/helper/pair");
                req.Method = "POST";
                req.ContentType = "application/json";
                req.Timeout = 20000;
                var bytes = Encoding.UTF8.GetBytes(body);
                using (var s = req.GetRequestStream()) s.Write(bytes, 0, bytes.Length);
                string text;
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var r = new StreamReader(resp.GetResponseStream())) text = r.ReadToEnd();
                var d = json.Deserialize<Dictionary<string, object>>(text);
                settings.Server = server;
                settings.DeviceId = d["device_id"].ToString();
                settings.Name = d["name"].ToString();
                settings.Token = d["token"].ToString();
                settings.Save();
                codeBox.Text = "";
                Log("Paired with FreeHand as \"" + settings.Name + "\".");
                UpdateView();
                stopButton.Focus();
                Connect();
            }
            catch (WebException ex)
            {
                var message = "Couldn't reach FreeHand at " + server + ". Check the address, and that FreeHand is running.";
                var resp = ex.Response as HttpWebResponse;
                if (resp != null)
                {
                    try
                    {
                        using (var r = new StreamReader(resp.GetResponseStream()))
                        {
                            var d = json.Deserialize<Dictionary<string, object>>(r.ReadToEnd());
                            if (d.ContainsKey("detail")) message = d["detail"].ToString();
                        }
                    }
                    catch (Exception) { }
                }
                Native.MessageBoxW(Handle, message, "Pairing didn't work", Native.MB_OK | Native.MB_ICONINFORMATION);
                codeBox.Focus();
            }
            finally { pairButton.Enabled = true; }
        }

        void Unpair()
        {
            if (Native.MessageBoxW(Handle, "Unpair this computer? FreeHand won't be able to reach it until you pair it again.",
                    "FreeHand Helper", Native.MB_YESNO | Native.MB_ICONQUESTION | Native.MB_DEFBUTTON2) != Native.IDYES) return;
            if (connection != null) connection.Cancel();
            settings.Token = ""; settings.DeviceId = "";
            settings.Save();
            lookAllowed = false;
            Log("Unpaired. (Remove it in FreeHand too, under Help on my computer.)");
            UpdateView();
            codeBox.Focus();
        }

        // ── Stop and resume ──────────────────────────────────────────

        void StopHelping(string why)
        {
            if (!settings.Paired) return;
            stopped = true;
            lookAllowed = false;
            if (connection != null) connection.Cancel();
            Log("Stopped. " + why);
            Ui(UpdateView);
            trayIcon.ShowBalloonTip(3000, "FreeHand Helper", "Stopped. FreeHand can't see or do anything on this computer.", ToolTipIcon.Info);
            Ui(delegate { if (Visible) resumeButton.Focus(); });
        }

        void Resume()
        {
            stopped = false;
            Log("FreeHand can ask to help again.");
            UpdateView();
            stopButton.Focus();
            Connect();
        }

        // ── The connection to FreeHand ───────────────────────────────

        void Connect()
        {
            if (connection != null) connection.Cancel();
            connection = new CancellationTokenSource();
            var token = connection.Token;
            Task.Run(delegate { return ConnectionLoop(token); });
        }

        async Task ConnectionLoop(CancellationToken stop)
        {
            var url = settings.Server.Replace("https://", "wss://").Replace("http://", "ws://") + "/api/helper/connect";
            while (!stop.IsCancellationRequested && settings.Paired && !stopped)
            {
                using (var ws = new ClientWebSocket())
                {
                    ws.Options.SetRequestHeader("Authorization", "Bearer " + settings.Token);
                    try
                    {
                        await ws.ConnectAsync(new Uri(url), stop);
                        SetConnectionState("Connected to FreeHand.");
                        await Receive(ws, stop);
                    }
                    catch (Exception) { }
                    if (ws.CloseStatus.HasValue && (int)ws.CloseStatus.Value == 4401)
                    {
                        // Removed in FreeHand: go back to pairing.
                        settings.Token = ""; settings.DeviceId = "";
                        settings.Save();
                        lookAllowed = false;
                        Log("FreeHand no longer knows this computer (it was removed there). Pair it again.");
                        Ui(delegate { UpdateView(); ShowMe(); codeBox.Focus(); });
                        return;
                    }
                }
                if (stop.IsCancellationRequested) break;
                SetConnectionState("Can't reach FreeHand right now; trying again.");
                try { await Task.Delay(5000, stop); } catch (Exception) { break; }
            }
        }

        async Task Receive(ClientWebSocket ws, CancellationToken stop)
        {
            var buffer = new byte[64 * 1024];
            while (ws.State == WebSocketState.Open && !stop.IsCancellationRequested)
            {
                var ms = new MemoryStream();
                WebSocketReceiveResult r;
                do
                {
                    r = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), stop);
                    if (r.MessageType == WebSocketMessageType.Close) return;
                    ms.Write(buffer, 0, r.Count);
                } while (!r.EndOfMessage);
                Dictionary<string, object> msg;
                try { msg = json.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(ms.ToArray())); }
                catch (Exception) { continue; }
                if (Get(msg, "type") == "request")
                {
                    var m = msg;
                    var t = Task.Run(delegate { return HandleRequest(ws, m, stop); });
                }
            }
        }

        async Task HandleRequest(ClientWebSocket ws, Dictionary<string, object> msg, CancellationToken stop)
        {
            var reply = new Dictionary<string, object> { { "type", "result" }, { "id", Get(msg, "id") } };
            try
            {
                var action = Get(msg, "action");
                var args = msg.ContainsKey("args") ? msg["args"] as Dictionary<string, object> : null;
                var window = args != null ? Get(args, "window") : "";
                var why = Get(msg, "why");
                if (stopped) throw new HelperRefusal("The person stopped the FreeHand Helper.");
                if (!AskToLook(why)) throw new HelperRefusal("The person said no to FreeHand looking at their screen.");
                object data;
                if (action == "windows") { data = Look.Windows(); Log("Listed the open windows. " + why); }
                else if (action == "look") { var d = Look.Window(window); data = d; Log("Read \"" + d["window"] + "\". " + why); }
                else if (action == "screenshot") { var d = Look.Screenshot(window); data = d; Log("Took a picture of \"" + d["window"] + "\". " + why); }
                else throw new HelperRefusal("This Helper can only look (stage 1). It can't do \"" + action + "\" yet.");
                reply["ok"] = true;
                reply["data"] = data;
            }
            catch (HelperRefusal ex) { reply["ok"] = false; reply["error"] = ex.Message; }
            catch (Exception ex) { reply["ok"] = false; reply["error"] = "The Helper couldn't do that: " + ex.Message; }
            var bytes = Encoding.UTF8.GetBytes(json.Serialize(reply));
            await sendLock.WaitAsync();
            try { await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, stop); }
            catch (Exception) { }
            finally { sendLock.Release(); }
        }

        readonly object askLock = new object();

        // The first look in a session asks; the answer lasts until Stop or
        // the Helper is closed.
        bool AskToLook(string why)
        {
            lock (askLock)
            {
                if (lookAllowed) return true;
                var text = "FreeHand wants to see what's on your screen.\n\n" +
                    (why.Length > 0 ? "Why: " + why + "\n\n" : "") +
                    "Allow it to look for the rest of this session? It can't click or type, only look. " +
                    "You can stop it any time with Ctrl+Alt+Escape.";
                var yes = Native.MessageBoxW(IntPtr.Zero, text, "FreeHand Helper: allow FreeHand to look?",
                    Native.MB_YESNO | Native.MB_ICONQUESTION | Native.MB_TOPMOST | Native.MB_SETFOREGROUND) == Native.IDYES;
                if (yes && !stopped)
                {
                    lookAllowed = true;
                    Log("You allowed FreeHand to look for this session.");
                    Ui(UpdateView);
                }
                else Log("You said no to FreeHand looking.");
                return lookAllowed;
            }
        }

        static string Get(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) && v != null ? v.ToString() : "";
        }
    }

    class HelperRefusal : Exception { public HelperRefusal(string m) : base(m) { } }

    // ── Looking: windows, the accessibility tree, screenshots ────────────
    static class Look
    {
        const int MaxNodes = 600, MaxChars = 7000, MaxDepth = 30;

        public static List<object> Windows()
        {
            var fg = Native.GetForegroundWindow();
            var list = new List<object>();
            foreach (AutomationElement w in AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition))
            {
                try
                {
                    var name = w.Current.Name;
                    if (string.IsNullOrEmpty(name)) continue;
                    var item = new Dictionary<string, object> { { "title", name }, { "app", ProcessName(w.Current.ProcessId) } };
                    if (new IntPtr(w.Current.NativeWindowHandle) == fg) item["in_front"] = true;
                    list.Add(item);
                }
                catch (Exception) { }
            }
            return list;
        }

        static string ProcessName(int pid)
        {
            try { return Process.GetProcessById(pid).ProcessName; } catch (Exception) { return ""; }
        }

        // The window whose title contains `title`, or the one in front
        // (skipping the Helper's own window).
        public static AutomationElement Find(string title)
        {
            int me = Process.GetCurrentProcess().Id;
            if (string.IsNullOrEmpty(title))
            {
                var h = Native.GetForegroundWindow();
                while (h != IntPtr.Zero)
                {
                    uint pid;
                    Native.GetWindowThreadProcessId(h, out pid);
                    if (pid != me && Native.IsWindowVisible(h) && Native.GetWindowTextLength(h) > 0)
                        return AutomationElement.FromHandle(h);
                    h = Native.GetWindow(h, Native.GW_HWNDNEXT);
                }
                throw new HelperRefusal("There's no window open to look at.");
            }
            foreach (AutomationElement w in AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition))
            {
                try
                {
                    if (w.Current.ProcessId != me && (w.Current.Name ?? "").IndexOf(title, StringComparison.OrdinalIgnoreCase) >= 0)
                        return w;
                }
                catch (Exception) { }
            }
            throw new HelperRefusal("No open window has \"" + title + "\" in its title.");
        }

        public static Dictionary<string, object> Window(string title)
        {
            var w = Find(title);
            var sb = new StringBuilder();
            int count = 0;
            Walk(w, 0, sb, ref count);
            var text = sb.ToString();
            bool cut = text.Length > MaxChars || count >= MaxNodes;
            if (text.Length > MaxChars) text = text.Substring(0, MaxChars);
            var d = new Dictionary<string, object> { { "window", w.Current.Name }, { "app", ProcessName(w.Current.ProcessId) }, { "tree", text } };
            if (cut) d["note"] = "Only part of the window is shown: it's large. Ask about a smaller part, or take a screenshot.";
            return d;
        }

        static void Walk(AutomationElement el, int depth, StringBuilder sb, ref int count)
        {
            if (count >= MaxNodes || depth > MaxDepth || sb.Length > MaxChars) return;
            AutomationElement.AutomationElementInformation c;
            try { c = el.Current; } catch (Exception) { return; }
            var type = c.ControlType.ProgrammaticName.Replace("ControlType.", "").ToLowerInvariant();
            var name = c.Name ?? "";
            bool container = name.Length == 0 && (type == "pane" || type == "group" || type == "custom" || type == "document" && depth > 0);
            int next = depth;
            if (!container)
            {
                count++;
                sb.Append(new string(' ', depth * 2)).Append(type);
                sb.Append(name.Length > 0 ? " \"" + Clip(name, 120) + "\"" : " (no name)");
                Describe(el, c, sb);
                sb.Append('\n');
                next = depth + 1;
            }
            var walker = TreeWalker.ControlViewWalker;
            AutomationElement child;
            try { child = walker.GetFirstChild(el); } catch (Exception) { return; }
            while (child != null && count < MaxNodes && sb.Length <= MaxChars)
            {
                Walk(child, next, sb, ref count);
                try { child = walker.GetNextSibling(child); } catch (Exception) { break; }
            }
        }

        static void Describe(AutomationElement el, AutomationElement.AutomationElementInformation c, StringBuilder sb)
        {
            if (c.IsPassword) { sb.Append(" [password, hidden]"); return; }
            object p;
            if (el.TryGetCurrentPattern(ValuePattern.Pattern, out p))
            {
                var v = ((ValuePattern)p).Current.Value;
                if (!string.IsNullOrEmpty(v)) sb.Append(" value=\"").Append(Clip(v, 200)).Append('"');
            }
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out p))
                sb.Append(" [").Append(((TogglePattern)p).Current.ToggleState.ToString().ToLowerInvariant()).Append(']');
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out p))
            {
                var s = ((ExpandCollapsePattern)p).Current.ExpandCollapseState;
                if (s != ExpandCollapseState.LeafNode) sb.Append(" [").Append(s.ToString().ToLowerInvariant()).Append(']');
            }
            if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out p) && ((SelectionItemPattern)p).Current.IsSelected)
                sb.Append(" [selected]");
            if (!c.IsEnabled) sb.Append(" [unavailable]");
            if (c.HasKeyboardFocus) sb.Append(" [focus]");
            if (!string.IsNullOrEmpty(c.AccessKey)) sb.Append(" key=").Append(c.AccessKey);
            if (!string.IsNullOrEmpty(c.AcceleratorKey)) sb.Append(" shortcut=").Append(c.AcceleratorKey);
        }

        static string Clip(string s, int n)
        {
            s = s.Replace('\n', ' ').Replace('\r', ' ');
            return s.Length > n ? s.Substring(0, n) + "..." : s;
        }

        public static Dictionary<string, object> Screenshot(string title)
        {
            var w = Find(title);
            var r = w.Current.BoundingRectangle;
            if (r.IsEmpty || r.Width < 2 || r.Height < 2) throw new HelperRefusal("That window is minimised, so there's nothing to see.");
            var screen = SystemInformation.VirtualScreen;
            var rect = Rectangle.Intersect(new Rectangle((int)r.X, (int)r.Y, (int)r.Width, (int)r.Height), screen);
            using (var shot = new Bitmap(rect.Width, rect.Height))
            {
                using (var g = Graphics.FromImage(shot))
                {
                    g.CopyFromScreen(rect.Location, Point.Empty, rect.Size);
                    // Never send what's in a password field.
                    var pw = w.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.IsPasswordProperty, true));
                    foreach (AutomationElement f in pw)
                    {
                        try
                        {
                            var b = f.Current.BoundingRectangle;
                            g.FillRectangle(Brushes.Black, (float)(b.X - rect.X), (float)(b.Y - rect.Y), (float)b.Width, (float)b.Height);
                        }
                        catch (Exception) { }
                    }
                }
                var scale = Math.Min(1.0, 1280.0 / rect.Width);
                int width = Math.Max(1, (int)(rect.Width * scale)), height = Math.Max(1, (int)(rect.Height * scale));
                using (var small = new Bitmap(width, height))
                {
                    using (var g = Graphics.FromImage(small))
                    {
                        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                        g.DrawImage(shot, 0, 0, width, height);
                    }
                    var ms = new MemoryStream();
                    var jpeg = Array.Find(ImageCodecInfo.GetImageEncoders(), e => e.MimeType == "image/jpeg");
                    var ps = new EncoderParameters(1);
                    ps.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 70L);
                    small.Save(ms, jpeg, ps);
                    return new Dictionary<string, object> {
                        { "window", w.Current.Name }, { "width", width }, { "height", height },
                        { "image", "data:image/jpeg;base64," + Convert.ToBase64String(ms.ToArray()) } };
                }
            }
        }
    }
}
