// FreeHand Helper for Windows (stage 1: Look; stage 2: Act; stage 3: Change).
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
[assembly: System.Reflection.AssemblyVersion("0.4.0.0")]

namespace FreeHand.Helper
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--selftest")
                return SelfTest(args[1]);
            if (args.Length == 3 && args[0] == "--selftest-open")
                return SelfTestOpen(args[1], args[2]);
            if (args.Length == 2 && args[0] == "--selftest-change")
                return SelfTestChange(args[1]);
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
        // FreeHandHelper.exe --selftest-open notepad out.json: open an app
        // and read it straight away, timing each step.
        static int SelfTestOpen(string app, string outFile)
        {
            Native.SetProcessDPIAware();
            var result = new Dictionary<string, object>();
            var clock = Stopwatch.StartNew();
            try
            {
                var plan = Act.Prepare("open", new Dictionary<string, object> { { "target", app } });
                result["opened"] = Act.Run(plan);
                result["open_ms"] = clock.ElapsedMilliseconds;
                var typing = new Dictionary<string, object> { { "do", "type" }, { "text", "x" } };
                try { Act.Prepare("act", typing); result["type_before_look"] = "allowed (wrong)"; }
                catch (HelperRefusal ex) { result["type_before_look"] = ex.Message; }
                clock.Restart();
                var look = Look.Window("");
                // Only works out what would happen; nothing is typed.
                var planned = Act.Prepare("act", typing);
                result["type_after_look"] = planned.Summary;
                result["sensitive"] = planned.Sensitive;
                result["look_ms"] = clock.ElapsedMilliseconds;
                result["window"] = look["window"];
                result["tree_start"] = look["tree"].ToString().Substring(0, Math.Min(400, look["tree"].ToString().Length));
                result["ok"] = true;
            }
            catch (Exception ex) { result["ok"] = false; result["error"] = ex.ToString(); result["error_ms"] = clock.ElapsedMilliseconds; }
            File.WriteAllText(outFile, new JavaScriptSerializer().Serialize(result));
            return result.ContainsKey("error") ? 1 : 0;
        }

        // FreeHandHelper.exe --selftest-change out.json: this computer's
        // details, a program search, and what an install and a fix would
        // say when asking. Nothing is installed or changed.
        static int SelfTestChange(string outFile)
        {
            var result = new Dictionary<string, object>();
            try
            {
                result["about"] = Change.About();
                result["available"] = Change.Programs("zoom", "available");
                result["installed"] = Change.Programs("notepad", "installed");
                var install = Change.Prepare("package", new Dictionary<string, object> { { "do", "install" }, { "id", "Zoom.Zoom" } });
                result["install_asks"] = install.Summary + " | " + install.Details;
                try
                {
                    var all = Change.Prepare("package", new Dictionary<string, object> { { "do", "update_all" } });
                    result["update_all_asks"] = all.Summary + " | " + all.Details;
                }
                catch (HelperRefusal ex) { result["update_all_asks"] = ex.Message; }
                var fix = Change.Prepare("fix", new Dictionary<string, object> { { "fix", "sound" } });
                result["fix_asks"] = fix.Summary + " | admin=" + fix.Admin;
                try { Change.Prepare("package", new Dictionary<string, object> { { "do", "install" }, { "id", "x; rm -rf" } }); result["bad_id"] = "allowed (wrong)"; }
                catch (HelperRefusal ex) { result["bad_id"] = ex.Message; }
                try { Change.Prepare("fix", new Dictionary<string, object> { { "fix", "format the disk" } }); result["bad_fix"] = "allowed (wrong)"; }
                catch (HelperRefusal ex) { result["bad_fix"] = ex.Message; }
                result["ok"] = true;
            }
            catch (Exception ex) { result["ok"] = false; result["error"] = ex.ToString(); }
            File.WriteAllText(outFile, new JavaScriptSerializer().Serialize(result));
            return result.ContainsKey("error") ? 1 : 0;
        }

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
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
        [DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
        public const int SW_RESTORE = 9;
        public const uint WM_CLOSE = 0x0010;

        [StructLayout(LayoutKind.Sequential)]
        public struct INPUT { public uint type; public InputUnion u; }
        [StructLayout(LayoutKind.Explicit)]
        public struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr extra; }
        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; }

        [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
        [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();

        // Bring a window to the front. Windows only lets the program in
        // front do that, so briefly share input with it. (Tapping Alt, the
        // usual trick, opens the menu bar in many apps, and then typing
        // would go into the menu.)
        public static void Front(IntPtr h)
        {
            if (h == IntPtr.Zero) return;
            if (IsIconic(h)) ShowWindow(h, SW_RESTORE);
            if (GetForegroundWindow() == h) return;
            uint pid;
            uint front = GetWindowThreadProcessId(GetForegroundWindow(), out pid);
            uint me = GetCurrentThreadId();
            bool joined = front != 0 && front != me && AttachThreadInput(me, front, true);
            SetForegroundWindow(h);
            if (joined) AttachThreadInput(me, front, false);
            if (GetForegroundWindow() != h)
            {
                // Last resort: F24, a key no keyboard has and apps ignore.
                keybd_event(0x87, 0, 0, UIntPtr.Zero);
                keybd_event(0x87, 0, 2, UIntPtr.Zero);
                SetForegroundWindow(h);
            }
        }
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
        // Apps where the person allowed FreeHand to act for 30 minutes.
        readonly Dictionary<string, DateTime> appGrants = new Dictionary<string, DateTime>();
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
            else if (lookAllowed || GrantsText().Length > 0)
                text = "FreeHand is helping: " + (lookAllowed ? "it can see your screen for this session. " : "") + GrantsText() +
                       "Press Ctrl+Alt+Escape or Stop at any time.";
            else
                text = "Ready. FreeHand will ask you before it looks at anything. " + connectionState;
            statusLabel.Text = text;
            Text = lookAllowed && !stopped ? "FreeHand Helper - helping" : "FreeHand Helper";
            trayIcon.Text = lookAllowed && !stopped ? "FreeHand Helper: helping" : "FreeHand Helper";
        }

        string GrantsText()
        {
            var parts = new List<string>();
            lock (appGrants)
                foreach (var g in appGrants)
                    if (g.Value > DateTime.Now) parts.Add(g.Key + " until " + g.Value.ToString("HH:mm"));
            return parts.Count > 0 ? "It can act without asking in " + string.Join(", ", parts.ToArray()) + ". " : "";
        }

        // ── Pairing ──────────────────────────────────────────────────

        void Pair()
        {
            var server = serverBox.Text.Trim().TrimEnd('/');
            if (!server.StartsWith("http://") && !server.StartsWith("https://")) server = "http://" + server;
            if (server.StartsWith("http://") && !IsLocal(server))
            {
                // Over the internet the pairing code and the computer's key
                // must be encrypted on the way.
                Native.MessageBoxW(Handle, "For a FreeHand on the internet, the address must start with https:// (for example https://yourname.example.com).",
                    "Pairing didn't work", Native.MB_OK | Native.MB_ICONINFORMATION);
                serverBox.Focus();
                return;
            }
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

        // This computer or the local network: plain http is fine there.
        static bool IsLocal(string url)
        {
            Uri u;
            if (!Uri.TryCreate(url, UriKind.Absolute, out u)) return false;
            var h = u.Host.ToLowerInvariant();
            if (h == "localhost" || h == "127.0.0.1" || h == "[::1]" || h == "::1" || h.EndsWith(".local") || h.EndsWith(".localhost")) return true;
            IPAddress ip;
            if (!IPAddress.TryParse(h, out ip)) return false;
            var b = ip.GetAddressBytes();
            return b.Length == 4 && (b[0] == 10 || (b[0] == 192 && b[1] == 168) || (b[0] == 172 && b[1] >= 16 && b[1] <= 31));
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
            lock (appGrants) appGrants.Clear();
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
                    // Proxies close quiet connections; keep this one alive.
                    ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(25);
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
                object data;
                if (action == "package" || action == "fix")
                {
                    // Change: always asked, every time.
                    var plan = Change.Prepare(action, args);
                    if (!AskToAct(plan, why)) throw new HelperRefusal("The person said no to: " + plan.Summary + ".");
                    Log("Started: " + plan.Summary + ". " + why);
                    var done = Change.Do(plan);
                    data = done;
                    Log((done.ContainsKey("ok") && (bool)done["ok"] ? "Finished: " : "Didn't finish: ") + plan.Summary + ".");
                }
                else if (action == "act" || action == "window" || action == "open")
                {
                    // Act: work out exactly what would happen, ask, then do it.
                    var plan = Act.Prepare(action, args);
                    if (!AskToAct(plan, why)) throw new HelperRefusal("The person said no to: " + plan.Summary + ".");
                    data = Act.Run(plan);
                    Log("Did: " + plan.Summary + ". " + why);
                }
                else
                {
                    if (!AskToLook(why)) throw new HelperRefusal("The person said no to FreeHand looking at their screen.");
                    if (action == "windows") { data = Look.Windows(); Log("Listed the open windows. " + why); }
                    else if (action == "about") { data = Change.About(); Log("Checked this computer's details. " + why); }
                    else if (action == "programs")
                    {
                        var where = Get(args, "where") == "available" ? "available" : "installed";
                        data = Change.Programs(Get(args, "query"), where);
                        Log((where == "available" ? "Searched for programs: " : "Listed installed programs: ") + Get(args, "query") + ". " + why);
                    }
                    else if (action == "look") { var d = Look.Window(window); data = d; Log("Read \"" + d["window"] + "\". " + why); }
                    else if (action == "screenshot") { var d = Look.Screenshot(window); data = d; Log("Took a picture of \"" + d["window"] + "\". " + why); }
                    else throw new HelperRefusal("This Helper can't do \"" + action + "\" yet.");
                }
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

        // Every action asks, unless the person allowed this app for 30
        // minutes. Opening an app always asks.
        bool AskToAct(Plan plan, string why)
        {
            lock (askLock)
            {
                if (stopped) return false;
                var key = plan.App.ToLowerInvariant();
                lock (appGrants)
                {
                    DateTime until;
                    // Secret files always ask, even in an allowed app.
                    if (plan.Level != "change" && !plan.Sensitive && key.Length > 0 && appGrants.TryGetValue(key, out until) && until > DateTime.Now) return true;
                }
                int choice = 0;
                Invoke((Action)delegate
                {
                    using (var dialog = new ActDialog(plan, why))
                    {
                        dialog.ShowDialog();
                        choice = dialog.Choice;
                    }
                });
                if (stopped) return false;
                if (choice == 2 && key.Length > 0)
                {
                    lock (appGrants) appGrants[key] = DateTime.Now.AddMinutes(30);
                    Log("You allowed FreeHand to act in " + plan.App + " for 30 minutes.");
                    Ui(UpdateView);
                }
                else if (choice == 0) Log("You said no to: " + plan.Summary + ".");
                return choice > 0;
            }
        }

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
        // Windows' accessibility calls time out while an app is starting
        // or busy (Notepad just after opening, for example). Try again a
        // few times before giving up.
        public static T Retry<T>(Func<T> work)
        {
            for (int attempt = 1; ; attempt++)
            {
                try { return work(); }
                catch (Exception ex)
                {
                    bool busy = ex is TimeoutException || ex is ElementNotAvailableException || ex is COMException;
                    if (!busy || attempt >= 4) throw;
                    Thread.Sleep(700 * attempt);
                }
            }
        }

        // A named window may take a moment to appear (just opened).
        public static AutomationElement Find(string title)
        {
            for (int attempt = 0; ; attempt++)
            {
                try { return Retry(delegate { return FindOnce(title); }); }
                catch (HelperRefusal)
                {
                    if (attempt >= 5 || string.IsNullOrEmpty(title)) throw;
                    Thread.Sleep(600);
                }
            }
        }

        static AutomationElement FindOnce(string title)
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
            Act.Seen.Clear();
            Act.SeenWindow = IntPtr.Zero;
            Retry(delegate
            {
                sb.Clear(); count = 0; Act.Seen.Clear();
                Walk(w, 0, sb, ref count, true);
                return 0;
            });
            Act.SeenWindow = new IntPtr(w.Current.NativeWindowHandle);
            var text = sb.ToString();
            bool cut = text.Length > MaxChars || count >= MaxNodes;
            if (text.Length > MaxChars) text = text.Substring(0, MaxChars);
            var d = new Dictionary<string, object> { { "window", w.Current.Name }, { "app", ProcessName(w.Current.ProcessId) }, { "tree", text } };
            if (cut) d["note"] = "Only part of the window is shown: it's large. Ask about a smaller part, or take a screenshot.";
            return d;
        }

        static void Walk(AutomationElement el, int depth, StringBuilder sb, ref int count, bool top = false)
        {
            if (count >= MaxNodes || depth > MaxDepth || sb.Length > MaxChars) return;
            AutomationElement.AutomationElementInformation c;
            try { c = el.Current; } catch (Exception) { if (top) throw; return; }
            var type = c.ControlType.ProgrammaticName.Replace("ControlType.", "").ToLowerInvariant();
            var name = c.Name ?? "";
            bool container = name.Length == 0 && (type == "pane" || type == "group" || type == "custom" || type == "document" && depth > 0);
            int next = depth;
            if (!container)
            {
                count++;
                Act.Seen.Add(el);
                sb.Append(new string(' ', depth * 2)).Append('#').Append(Act.Seen.Count).Append(' ').Append(type);
                sb.Append(name.Length > 0 ? " \"" + Clip(name, 120) + "\"" : " (no name)");
                Describe(el, c, sb);
                sb.Append('\n');
                next = depth + 1;
            }
            var walker = TreeWalker.ControlViewWalker;
            AutomationElement child;
            try { child = walker.GetFirstChild(el); } catch (Exception) { if (top) throw; return; }
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

    // What FreeHand wants to do, worked out before asking.
    class Plan
    {
        public string Action = "", Do = "", Text = "", Target = "", App = "", WindowTitle = "", Summary = "", Into = "";
        public bool Sensitive;
        // Stage 3: "change" (programs and fixes): always asked, never in advance.
        public string Level = "act", Details = "";
        public bool Admin;
        public AutomationElement Window, Element;
    }

    // The "Allow?" question for acting: a normal dialog, so screen readers
    // read it. Focus starts on the message; Escape means Don't allow.
    class ActDialog : Form
    {
        public int Choice; // 0 no, 1 once, 2 this app for 30 minutes

        public ActDialog(Plan plan, string why)
        {
            Text = "FreeHand Helper: allow FreeHand to do this?";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MinimizeBox = false; MaximizeBox = false; ShowInTaskbar = true; TopMost = true;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10f);
            Width = 560; Height = 330;

            bool change = plan.Level == "change";
            if (change) Text = "FreeHand Helper: allow FreeHand to change your computer?";
            var text = (change
                    ? "This changes your computer. FreeHand always asks for changes like this.\r\n\r\n"
                    : plan.Sensitive
                    ? "CAREFUL: this looks like a file or window with passwords or secret keys. Only allow this if you're sure.\r\n\r\n"
                    : "") +
                "FreeHand wants to: " + plan.Summary + "." +
                (plan.WindowTitle.Length > 0 ? "\r\nIn: " + plan.WindowTitle + (plan.App.Length > 0 ? " (" + plan.App + ")" : "") : "") +
                (why.Length > 0 ? "\r\nWhy: " + why : "") +
                (plan.Details.Length > 0 ? "\r\n\r\n" + plan.Details : "") +
                (plan.Admin ? "\r\n\r\nWindows will then ask \"Do you want to allow this app to make changes?\". That question is from Windows itself: press Yes if you want this fix." : "") +
                (plan.App.Length > 0 && plan.Action != "open" && !plan.Sensitive && !change
                    ? "\r\n\r\nIf you allow " + plan.App + " for 30 minutes, FreeHand can click and type in it without asking until then. " +
                      "Something on screen could try to trick it, so only choose that for apps you trust."
                    : "") +
                "\r\n\r\nCtrl+Alt+Escape stops FreeHand at any time.";
            var message = new TextBox { Multiline = true, ReadOnly = true, Text = text, Dock = DockStyle.Fill, TabIndex = 0,
                                        ScrollBars = ScrollBars.Vertical, AccessibleName = "What FreeHand wants to do", BorderStyle = BorderStyle.None };
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, Padding = new Padding(6) };
            var once = new Button { Text = change ? "Allow" : "Allow once", AutoSize = true, TabIndex = 1 };
            once.Click += delegate { Choice = 1; Close(); };
            buttons.Controls.Add(once);
            if (plan.App.Length > 0 && plan.Action != "open" && !plan.Sensitive && !change)
            {
                var app = new Button { Text = "Allow in " + plan.App + " for 30 minutes", AutoSize = true, TabIndex = 2 };
                app.Click += delegate { Choice = 2; Close(); };
                buttons.Controls.Add(app);
            }
            var no = new Button { Text = "Don't allow", AutoSize = true, TabIndex = 3 };
            no.Click += delegate { Choice = 0; Close(); };
            buttons.Controls.Add(no);
            CancelButton = no;
            var panel = new Panel { Dock = DockStyle.Fill, Padding = new Padding(10) };
            panel.Controls.Add(message);
            Controls.Add(panel);
            Controls.Add(buttons);

            // Nobody answering counts as no, before FreeHand gives up waiting.
            var timer = new System.Windows.Forms.Timer { Interval = 100000 };
            timer.Tick += delegate { timer.Stop(); Choice = 0; Close(); };
            Shown += delegate
            {
                timer.Start();
                Native.Front(Handle);
                Activate();
                message.Focus();
                message.SelectionStart = 0; message.SelectionLength = 0;
            };
        }
    }

    static class Act
    {
        // The elements from the last computer_look, so FreeHand can aim at
        // "#12" even when it has no name.
        public static readonly List<AutomationElement> Seen = new List<AutomationElement>();
        // The window that last look was of: typing needs a fresh look at it.
        public static IntPtr SeenWindow = IntPtr.Zero;

        // Apps FreeHand may never act in: Windows' own security and sign-in
        // screens. And never FreeHand's own console (so it can't approve
        // itself) or the Helper (it isn't in the window list at all).
        static readonly string[] BlockedApps = { "consent", "credentialuibroker", "lockapp", "logonui", "securityhealthui" };
        static readonly string[] BlockedOpen = { "cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "regedit", "reg", "rundll32",
            "msiexec", "bash", "wsl", "diskpart", "format", "bcdedit", "schtasks", "sc", "net", "netsh", "taskkill", "certutil", "bitsadmin" };

        static string Arg(Dictionary<string, object> a, string key)
        {
            object v;
            return a != null && a.TryGetValue(key, out v) && v != null ? v.ToString().Trim() : "";
        }

        static string ProcessOf(AutomationElement el)
        {
            try { return Process.GetProcessById(el.Current.ProcessId).ProcessName; } catch (Exception) { return ""; }
        }

        static void CheckAllowed(AutomationElement window, string app)
        {
            if (Array.IndexOf(BlockedApps, app.ToLowerInvariant()) >= 0)
                throw new HelperRefusal("FreeHand can't act in Windows' security or sign-in screens. Please do that part yourself.");
            var title = window.Current.Name ?? "";
            if (title == "FreeHand" || title.StartsWith("FreeHand - ") || title.StartsWith("FreeHand — "))
                throw new HelperRefusal("FreeHand can't act in its own console, so it can never approve its own requests.");
        }

        public static Plan Prepare(string action, Dictionary<string, object> args)
        {
            var plan = new Plan { Action = action, Do = Arg(args, "do").ToLowerInvariant(), Text = Arg(args, "text"), Target = Arg(args, "element") };
            if (action == "open")
            {
                var target = Arg(args, "target");
                bool url = target.StartsWith("https://") || target.StartsWith("http://");
                if (!url)
                {
                    foreach (var ch in target)
                        if (!(char.IsLetterOrDigit(ch) || ch == ' ' || ch == '.' || ch == '-' || ch == '_'))
                            throw new HelperRefusal("FreeHand can open a website or an app by name (like notepad or chrome), nothing else.");
                    var name = target.ToLowerInvariant();
                    if (name.EndsWith(".exe")) name = name.Substring(0, name.Length - 4);
                    if (target.Length == 0 || Array.IndexOf(BlockedOpen, name) >= 0)
                        throw new HelperRefusal("FreeHand can't open \"" + target + "\": command tools come in a later, stricter stage.");
                }
                plan.Target = target;
                plan.Summary = url ? "open the website " + target : "open " + target;
                return plan;
            }

            plan.Window = Look.Find(Arg(args, "window"));
            plan.App = ProcessOf(plan.Window);
            plan.WindowTitle = plan.Window.Current.Name ?? "";
            CheckAllowed(plan.Window, plan.App);

            if (action == "window")
            {
                var verbs = new Dictionary<string, string> { { "switch", "switch to" }, { "close", "close" }, { "minimise", "minimise" },
                    { "minimize", "minimise" }, { "maximise", "maximise" }, { "maximize", "maximise" }, { "restore", "restore" } };
                if (!verbs.ContainsKey(plan.Do)) throw new HelperRefusal("For a window, FreeHand can switch, close, minimise, maximise or restore.");
                plan.Summary = verbs[plan.Do] + " the window \"" + plan.WindowTitle + "\"";
                return plan;
            }

            // act: click, type, keys or focus
            if (plan.Target.Length > 0) plan.Element = FindElement(plan.Window, plan.Target);
            var what = plan.Element != null ? Describe(plan.Element) : "";
            if (plan.Do == "click" || plan.Do == "focus")
            {
                if (plan.Element == null) throw new HelperRefusal("Say which element to " + plan.Do + ": its name, or its #number from computer_look.");
                plan.Summary = (plan.Do == "click" ? "click " : "move to ") + what;
            }
            else if (plan.Do == "type" || plan.Do == "keys")
            {
                if (plan.Text.Length == 0) throw new HelperRefusal(plan.Do == "type" ? "Say what to type." : "Say which keys to press.");
                if (plan.Do == "keys") Keys.Parse(plan.Text); // check before asking
                // Typing goes wherever the cursor is, so FreeHand must have
                // looked at this window since its last action (an app can
                // open something unexpected, like a file from last time).
                if (SeenWindow == IntPtr.Zero || SeenWindow != new IntPtr(plan.Window.Current.NativeWindowHandle))
                    throw new HelperRefusal("Look at this window first (computer_look), so you know what's open and where the cursor is. Then try again.");
                if (plan.Element == null)
                {
                    // Find out exactly where the text would go: bring the
                    // window to the front, and see what has the focus there.
                    Native.Front(new IntPtr(plan.Window.Current.NativeWindowHandle));
                    Thread.Sleep(300);
                    var f = Focused();
                    if (f == null || f.Current.ProcessId != plan.Window.Current.ProcessId)
                        throw new HelperRefusal("Couldn't tell where the cursor is in that window. Say which element to type into.");
                    plan.Element = f;
                    what = Describe(f);
                }
                if (plan.Element.Current.IsPassword)
                    throw new HelperRefusal("That's a password field. FreeHand never types passwords: please type it yourself.");
                plan.Into = what + " in \"" + plan.WindowTitle + "\"";
                plan.Summary = plan.Do == "type"
                    ? "type \"" + Clip(plan.Text, 80) + "\" into " + plan.Into
                    : "press " + plan.Text + " in " + plan.Into;
            }
            else throw new HelperRefusal("FreeHand can click, type, press keys or move to an element.");
            plan.Sensitive = LooksSecret(plan.WindowTitle) || (plan.Element != null && LooksSecret(plan.Element.Current.Name ?? ""));
            return plan;
        }

        // Files and windows that usually hold passwords or secret keys.
        static readonly string[] SecretWords = { ".env", "password", "passwd", "secret", "credential", "token", "api key", "apikey",
            "api_key", ".pem", ".key", "id_rsa", "id_ed25519", "wallet", "keystore", "vault", "2fa", "recovery code", "backup code" };

        public static bool LooksSecret(string s)
        {
            s = (s ?? "").ToLowerInvariant();
            foreach (var w in SecretWords) if (s.Contains(w)) return true;
            return false;
        }

        static AutomationElement Focused()
        {
            try { return AutomationElement.FocusedElement; } catch (Exception) { return null; }
        }

        static string Describe(AutomationElement el)
        {
            var c = el.Current;
            var type = c.ControlType.ProgrammaticName.Replace("ControlType.", "").ToLowerInvariant();
            return string.IsNullOrEmpty(c.Name) ? "the unnamed " + type : "the " + type + " \"" + Clip(c.Name, 80) + "\"";
        }

        static string Clip(string s, int n) { return s.Length > n ? s.Substring(0, n) + "..." : s; }

        static AutomationElement FindElement(AutomationElement window, string target)
        {
            int me = Process.GetCurrentProcess().Id;
            if (target.StartsWith("#"))
            {
                int n;
                if (!int.TryParse(target.Substring(1), out n) || n < 1 || n > Seen.Count)
                    throw new HelperRefusal("There's no element " + target + ". Use computer_look again to get fresh numbers.");
                var el = Seen[n - 1];
                try
                {
                    if (el.Current.ProcessId != window.Current.ProcessId || el.Current.ProcessId == me)
                        throw new HelperRefusal("Element " + target + " isn't in that window. Use computer_look on it first.");
                }
                catch (ElementNotAvailableException) { throw new HelperRefusal("Element " + target + " has gone. Use computer_look again."); }
                return el;
            }
            var exact = window.FindAll(TreeScope.Descendants,
                new PropertyCondition(AutomationElement.NameProperty, target, PropertyConditionFlags.IgnoreCase));
            var found = new List<AutomationElement>();
            foreach (AutomationElement e in exact)
                try { if (e.Current.IsEnabled && !e.Current.IsOffscreen) found.Add(e); } catch (Exception) { }
            if (found.Count == 0)
                foreach (AutomationElement e in exact) found.Add(e);
            if (found.Count == 1) return found[0];
            if (found.Count > 1)
                throw new HelperRefusal("There are " + found.Count + " things called \"" + target + "\" in that window. Use computer_look and say which by its #number.");
            throw new HelperRefusal("Nothing called \"" + target + "\" in that window. Use computer_look to see what's there.");
        }

        public static Dictionary<string, object> Run(Plan plan)
        {
            if (plan.Action == "open")
            {
                var before = Native.GetForegroundWindow();
                Process.Start(new ProcessStartInfo(plan.Target) { UseShellExecute = true });
                for (int i = 0; i < 20 && Native.GetForegroundWindow() == before; i++) Thread.Sleep(300);
                Thread.Sleep(800); // let it finish drawing
                return After("Opened " + plan.Target + ". Look at the window before typing: apps can open something from last time.");
            }
            var h = new IntPtr(plan.Window.Current.NativeWindowHandle);
            if (plan.Action == "window")
            {
                object p;
                var wp = plan.Window.TryGetCurrentPattern(WindowPattern.Pattern, out p) ? (WindowPattern)p : null;
                if (plan.Do == "switch") Native.Front(h);
                else if (plan.Do == "restore") { Native.Front(h); if (wp != null) wp.SetWindowVisualState(WindowVisualState.Normal); }
                else if (plan.Do == "close") { if (wp != null) wp.Close(); else Native.PostMessage(h, Native.WM_CLOSE, IntPtr.Zero, IntPtr.Zero); }
                else if (plan.Do.StartsWith("minimi")) { if (wp != null) wp.SetWindowVisualState(WindowVisualState.Minimized); }
                else if (wp != null) wp.SetWindowVisualState(WindowVisualState.Maximized);
                return After("Done: " + plan.Summary + ".");
            }

            Native.Front(h);
            Thread.Sleep(150);
            var el = plan.Element;
            if (plan.Do == "focus") el.SetFocus();
            else if (plan.Do == "click") Click(el);
            else if (plan.Do == "type")
            {
                object vp;
                if (el != null && el.TryGetCurrentPattern(ValuePattern.Pattern, out vp) && !((ValuePattern)vp).Current.IsReadOnly)
                    ((ValuePattern)vp).SetValue(plan.Text);
                else
                {
                    try { el.SetFocus(); } catch (Exception) { }
                    var f = Focused();
                    if (f == null || f.Current.IsPassword || f.Current.ProcessId != plan.Window.Current.ProcessId)
                        throw new HelperRefusal("The cursor moved before FreeHand could type, so it didn't. Look again and retry.");
                    Keys.Type(plan.Text);
                }
            }
            else if (plan.Do == "keys")
            {
                if (el != null) el.SetFocus();
                Keys.Press(plan.Text);
            }
            return After("Done: " + plan.Summary + ".");
        }

        static void Click(AutomationElement el)
        {
            object p;
            if (el.TryGetCurrentPattern(InvokePattern.Pattern, out p)) { ((InvokePattern)p).Invoke(); return; }
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out p)) { ((TogglePattern)p).Toggle(); return; }
            if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out p)) { ((SelectionItemPattern)p).Select(); return; }
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out p))
            {
                var e = (ExpandCollapsePattern)p;
                if (e.Current.ExpandCollapseState == ExpandCollapseState.Expanded) e.Collapse(); else e.Expand();
                return;
            }
            // No accessible way to press it: click its middle with the mouse.
            var r = el.Current.BoundingRectangle;
            if (r.IsEmpty) throw new HelperRefusal("That element can't be pressed, and isn't on screen to click.");
            Native.SetCursorPos((int)(r.X + r.Width / 2), (int)(r.Y + r.Height / 2));
            var down = new Native.INPUT { type = 0 }; down.u.mi.dwFlags = 0x0002;
            var up = new Native.INPUT { type = 0 }; up.u.mi.dwFlags = 0x0004;
            Native.SendInput(2, new[] { down, up }, Marshal.SizeOf(typeof(Native.INPUT)));
        }

        // What's in front now, so FreeHand knows what happened.
        static Dictionary<string, object> After(string message)
        {
            Thread.Sleep(600);
            var d = new Dictionary<string, object> { { "message", message } };
            try
            {
                var fg = Native.GetForegroundWindow();
                if (fg != IntPtr.Zero) d["window_in_front"] = AutomationElement.FromHandle(fg).Current.Name;
                var f = Focused();
                if (f != null) d["focus"] = Describe(f);
            }
            catch (Exception) { }
            Seen.Clear(); // the old numbers may be wrong now
            SeenWindow = IntPtr.Zero;
            return d;
        }
    }

    // Typing and key presses, as if from the keyboard.
    static class Keys
    {
        static readonly Dictionary<string, ushort> Named = new Dictionary<string, ushort>(StringComparer.OrdinalIgnoreCase) {
            { "enter", 0x0D }, { "return", 0x0D }, { "tab", 0x09 }, { "escape", 0x1B }, { "esc", 0x1B }, { "space", 0x20 },
            { "backspace", 0x08 }, { "delete", 0x2E }, { "del", 0x2E }, { "insert", 0x2D }, { "home", 0x24 }, { "end", 0x23 },
            { "pageup", 0x21 }, { "pagedown", 0x22 }, { "up", 0x26 }, { "down", 0x28 }, { "left", 0x25 }, { "right", 0x27 },
            { "f1", 0x70 }, { "f2", 0x71 }, { "f3", 0x72 }, { "f4", 0x73 }, { "f5", 0x74 }, { "f6", 0x75 }, { "f7", 0x76 },
            { "f8", 0x77 }, { "f9", 0x78 }, { "f10", 0x79 }, { "f11", 0x7A }, { "f12", 0x7B }, { "apps", 0x5D }, { "menu", 0x5D } };
        static readonly Dictionary<string, ushort> Mods = new Dictionary<string, ushort>(StringComparer.OrdinalIgnoreCase) {
            { "ctrl", 0x11 }, { "control", 0x11 }, { "alt", 0x12 }, { "shift", 0x10 } };

        // "Ctrl+S", "Down Down Enter", "Alt+F4" -> a list of (modifiers, key).
        public static List<KeyValuePair<List<ushort>, ushort>> Parse(string spec)
        {
            var list = new List<KeyValuePair<List<ushort>, ushort>>();
            foreach (var combo in spec.Split(new[] { ' ', ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var mods = new List<ushort>();
                ushort key = 0;
                foreach (var part in combo.Split('+'))
                {
                    ushort v;
                    if (Mods.TryGetValue(part, out v)) mods.Add(v);
                    else if (Named.TryGetValue(part, out v)) key = v;
                    else if (part.Length == 1) key = (ushort)(Native.VkKeyScan(char.ToLowerInvariant(part[0])) & 0xff);
                    else throw new HelperRefusal("FreeHand doesn't know the key \"" + part + "\".");
                }
                if (key == 0) throw new HelperRefusal("\"" + combo + "\" has no key to press.");
                if (mods.Contains(0x11) && mods.Contains(0x12) && (key == 0x2E || key == 0x1B))
                    throw new HelperRefusal("FreeHand can't press " + combo + ".");
                list.Add(new KeyValuePair<List<ushort>, ushort>(mods, key));
            }
            if (list.Count == 0 || list.Count > 30) throw new HelperRefusal("Say which keys to press (up to 30).");
            return list;
        }

        public static void Press(string spec)
        {
            foreach (var combo in Parse(spec))
            {
                var inputs = new List<Native.INPUT>();
                foreach (var m in combo.Key) inputs.Add(Key(m, false));
                inputs.Add(Key(combo.Value, false));
                inputs.Add(Key(combo.Value, true));
                for (int i = combo.Key.Count - 1; i >= 0; i--) inputs.Add(Key(combo.Key[i], true));
                Send(inputs);
                Thread.Sleep(60);
            }
        }

        public static void Type(string text)
        {
            foreach (var ch in text.Replace("\r\n", "\n"))
            {
                if (ch == '\n') { Send(new List<Native.INPUT> { Key(0x0D, false), Key(0x0D, true) }); continue; }
                var down = new Native.INPUT { type = 1 }; down.u.ki.scan = ch; down.u.ki.flags = 0x0004;
                var up = new Native.INPUT { type = 1 }; up.u.ki.scan = ch; up.u.ki.flags = 0x0004 | 0x0002;
                Send(new List<Native.INPUT> { down, up });
                Thread.Sleep(5);
            }
        }

        static Native.INPUT Key(ushort vk, bool up)
        {
            var i = new Native.INPUT { type = 1 };
            i.u.ki.vk = vk;
            i.u.ki.flags = up ? 0x0002u : 0u;
            return i;
        }

        static void Send(List<Native.INPUT> inputs)
        {
            Native.SendInput((uint)inputs.Count, inputs.ToArray(), Marshal.SizeOf(typeof(Native.INPUT)));
        }
    }

    // ── Stage 3: Change. Programs (through winget, Windows' own installer)
    // and a fixed list of known fixes. Always asked, every time, and never
    // allowed in advance. Nothing FreeHand sends is run as a command: it
    // can only pick a package id or a fix by name.
    static class Change
    {
        static readonly System.Text.RegularExpressions.Regex PackageId =
            new System.Text.RegularExpressions.Regex(@"^[A-Za-z0-9][A-Za-z0-9.\-_+]{0,99}$");

        // name -> (plain description, needs admin, PowerShell script)
        public static readonly Dictionary<string, string[]> Fixes = new Dictionary<string, string[]> {
            { "sound", new[] { "restart Windows' sound services (fixes sound that stopped working)", "admin",
                "Restart-Service -Name AudioEndpointBuilder -Force; Start-Service -Name Audiosrv" } },
            { "printer", new[] { "clear stuck print jobs and restart printing", "admin",
                "Stop-Service -Name Spooler -Force; Remove-Item -Path \"$env:SystemRoot\\System32\\spool\\PRINTERS\\*\" -Force -ErrorAction SilentlyContinue; Start-Service -Name Spooler" } },
            { "network", new[] { "refresh the network connection (the internet drops for a few seconds)", "admin",
                "ipconfig /flushdns; ipconfig /release; ipconfig /renew" } },
            { "taskbar", new[] { "restart Windows Explorer (fixes a frozen taskbar or Start menu; open folders close)", "",
                "Stop-Process -Name explorer -Force; Start-Sleep -Seconds 3; if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) { Start-Process explorer.exe }" } },
        };

        public static string Winget()
        {
            var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Microsoft\WindowsApps\winget.exe");
            return File.Exists(path) ? path : "winget";
        }

        public static string Run(string exe, string args, int timeoutMs, out int exit)
        {
            var psi = new ProcessStartInfo(exe, args) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
                                                        CreateNoWindow = true, StandardOutputEncoding = Encoding.UTF8 };
            using (var p = Process.Start(psi))
            {
                var output = p.StandardOutput.ReadToEndAsync();
                var errors = p.StandardError.ReadToEndAsync();
                if (!p.WaitForExit(timeoutMs))
                {
                    try { p.Kill(); } catch (Exception) { }
                    throw new HelperRefusal("That took too long, so the Helper stopped waiting.");
                }
                exit = p.ExitCode;
                return output.Result + errors.Result;
            }
        }

        static string Clean(string line)
        {
            // winget draws a spinner with carriage returns before its output
            // (and ends each line with one).
            line = line.TrimEnd('\r', ' ');
            var i = line.LastIndexOf('\r');
            return (i >= 0 ? line.Substring(i + 1) : line).TrimEnd();
        }

        // winget's tables: columns start where the header words start.
        public static List<object> Table(string text, int max)
        {
            var lines = new List<string>();
            foreach (var l in text.Split('\n')) lines.Add(Clean(l));
            int head = lines.FindIndex(l => l.Contains(" Id ") && l.Contains("Version"));
            var rows = new List<object>();
            if (head < 0) return rows;
            var header = lines[head];
            var names = new List<string>(); var starts = new List<int>();
            foreach (System.Text.RegularExpressions.Match m in System.Text.RegularExpressions.Regex.Matches(header, @"\S+"))
            { names.Add(m.Value.ToLowerInvariant()); starts.Add(m.Index); }
            for (int i = head + 2; i < lines.Count && rows.Count < max; i++)
            {
                var l = lines[i];
                if (l.Trim().Length == 0 || l.Length < starts[starts.Count - 1]) continue;
                var row = new Dictionary<string, object>();
                for (int c = 0; c < names.Count; c++)
                {
                    int s = starts[c], e = c + 1 < starts.Count ? starts[c + 1] : l.Length;
                    row[names[c]] = s < l.Length ? l.Substring(s, Math.Min(e, l.Length) - s).Trim() : "";
                }
                rows.Add(row);
            }
            return rows;
        }

        public static Dictionary<string, object> Programs(string query, string where)
        {
            int exit;
            if (query.Length > 0 && !System.Text.RegularExpressions.Regex.IsMatch(query, @"^[\w .\-+]{1,60}$"))
                throw new HelperRefusal("Search with plain words only.");
            var q = query.Length > 0 ? " --query \"" + query + "\"" : "";
            if (where == "updates")
            {
                var ups = Run(Winget(), "upgrade --accept-source-agreements --disable-interactivity", 180000, out exit);
                return new Dictionary<string, object> { { "updates", Table(ups, 60) } };
            }
            if (where == "available")
            {
                if (query.Length == 0) throw new HelperRefusal("Say what program to look for.");
                var text = Run(Winget(), "search" + q + " --accept-source-agreements --disable-interactivity", 120000, out exit);
                return new Dictionary<string, object> { { "available", Table(text, 15) } };
            }
            var installed = Run(Winget(), "list" + q + " --accept-source-agreements --disable-interactivity", 120000, out exit);
            return new Dictionary<string, object> { { "installed", Table(installed, query.Length > 0 ? 30 : 80) } };
        }

        public static Dictionary<string, string> Show(string id)
        {
            int exit;
            var text = Run(Winget(), "show --id " + id + " --exact --accept-source-agreements --disable-interactivity", 120000, out exit);
            var d = new Dictionary<string, string>();
            foreach (var raw in text.Split('\n'))
            {
                var l = Clean(raw);
                if (l.StartsWith("Found ")) d["found"] = l.Substring(6);
                int colon = l.IndexOf(": ");
                if (colon > 0 && !l.StartsWith(" ")) { var k = l.Substring(0, colon).ToLowerInvariant(); if (!d.ContainsKey(k)) d[k] = l.Substring(colon + 2); }
            }
            return d;
        }

        public static Plan Prepare(string action, Dictionary<string, object> args)
        {
            var plan = new Plan { Action = action, Level = "change" };
            Func<string, string> Arg = delegate(string k)
            {
                object v;
                return args != null && args.TryGetValue(k, out v) && v != null ? v.ToString().Trim() : "";
            };
            if (action == "package")
            {
                plan.Do = Arg("do").ToLowerInvariant();
                plan.Target = Arg("id");
                if (plan.Do == "update_all")
                {
                    // One question listing every program that would update.
                    var ups = Programs("", "updates")["updates"] as List<object>;
                    if (ups.Count == 0) throw new HelperRefusal("Every program is already up to date.");
                    var names = new List<string>();
                    foreach (Dictionary<string, object> r in ups)
                        names.Add(r["name"] + (r.ContainsKey("version") && r.ContainsKey("available") ? " (" + r["version"] + " to " + r["available"] + ")" : ""));
                    plan.Summary = "update " + ups.Count + (ups.Count == 1 ? " program" : " programs") + " that have updates";
                    plan.Details = "These would update:\r\n" + string.Join("\r\n", names.ToArray()) +
                        "\r\n\r\nSome programs may close while they update, so save your work first.";
                    foreach (var n in names)
                        if (n.StartsWith("Docker Desktop"))
                            plan.Details += "\r\nDocker Desktop is in the list. FreeHand runs inside Docker on this computer, so " +
                                "updating it restarts FreeHand, and FreeHand may not be able to tell you how the update went.";
                    return plan;
                }
                if (plan.Do != "install" && plan.Do != "update" && plan.Do != "remove")
                    throw new HelperRefusal("For a program, FreeHand can install, update, update everything (update_all) or remove.");
                if (!PackageId.IsMatch(plan.Target))
                    throw new HelperRefusal("Give the program's exact id from computer_programs (like Zoom.Zoom).");
                if (plan.Do == "remove")
                {
                    var mine = Programs(plan.Target, "installed")["installed"] as List<object>;
                    Dictionary<string, object> hit = null;
                    foreach (Dictionary<string, object> r in mine)
                        if (string.Equals(r["id"].ToString(), plan.Target, StringComparison.OrdinalIgnoreCase)) hit = r;
                    if (hit == null) throw new HelperRefusal("No installed program has the id " + plan.Target + ".");
                    plan.Summary = "remove " + hit["name"] + " (" + plan.Target + ") from this computer";
                    plan.Details = "Anything saved only inside that program may be lost.";
                    plan.Sensitive = true;
                }
                else
                {
                    var info = Show(plan.Target);
                    if (!info.ContainsKey("found")) throw new HelperRefusal("Windows' installer doesn't know the id " + plan.Target + ".");
                    plan.Summary = (plan.Do == "install" ? "install " : "update ") + info["found"] +
                        (info.ContainsKey("version") ? ", version " + info["version"] : "");
                    var bits = new List<string>();
                    if (info.ContainsKey("publisher")) bits.Add("Publisher: " + info["publisher"]);
                    if (info.ContainsKey("description")) bits.Add("What it is: " + (info["description"].Length > 200 ? info["description"].Substring(0, 200) + "..." : info["description"]));
                    if (info.ContainsKey("homepage")) bits.Add("Website: " + info["homepage"]);
                    bits.Add("From: winget, Windows' own installer.");
                    plan.Details = string.Join("\r\n", bits.ToArray());
                }
                return plan;
            }
            // fix
            plan.Target = Arg("fix").ToLowerInvariant();
            if (!Fixes.ContainsKey(plan.Target))
                throw new HelperRefusal("FreeHand only knows these fixes: " + string.Join(", ", new List<string>(Fixes.Keys).ToArray()) + ".");
            plan.Summary = Fixes[plan.Target][0];
            plan.Admin = Fixes[plan.Target][1] == "admin";
            return plan;
        }

        public static Dictionary<string, object> Do(Plan plan)
        {
            int exit;
            if (plan.Action == "package")
            {
                var verb = plan.Do == "remove" ? "uninstall" : plan.Do == "install" ? "install" : "upgrade";
                var which = plan.Do == "update_all" ? "--all" : "--id " + plan.Target + " --exact";
                var text = Run(Winget(), verb + " " + which + " --silent --accept-source-agreements" +
                    (verb == "uninstall" ? "" : " --accept-package-agreements") + " --disable-interactivity",
                    plan.Do == "update_all" ? 3500000 : 1100000, out exit);
                var tail = new List<string>();
                foreach (var l in text.Split('\n')) { var c = Clean(l); if (c.Trim().Length > 0 && !c.Contains("█") && !c.Contains("▒")) tail.Add(c.Trim()); }
                var last = tail.Count > 6 ? tail.GetRange(tail.Count - 6, 6) : tail;
                return new Dictionary<string, object> { { "ok", exit == 0 }, { "exit_code", exit }, { "winget_said", string.Join(" / ", last.ToArray()) } };
            }
            var script = Fixes[plan.Target][2];
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
            var args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + encoded;
            if (!plan.Admin)
            {
                Run("powershell.exe", args, 120000, out exit);
                return new Dictionary<string, object> { { "ok", exit == 0 }, { "message", exit == 0 ? "Done: " + plan.Summary + "." : "The fix didn't finish (code " + exit + ")." } };
            }
            // Needs an administrator: Windows asks the person itself ("Do
            // you want to allow this app to make changes?"). FreeHand can't
            // see or press that.
            try
            {
                using (var p = Process.Start(new ProcessStartInfo("powershell.exe", args) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden }))
                {
                    if (!p.WaitForExit(180000)) throw new HelperRefusal("The fix took too long.");
                    return new Dictionary<string, object> { { "ok", p.ExitCode == 0 },
                        { "message", p.ExitCode == 0 ? "Done: " + plan.Summary + "." : "The fix didn't finish (code " + p.ExitCode + ")." } };
                }
            }
            catch (System.ComponentModel.Win32Exception ex)
            {
                if (ex.NativeErrorCode == 1223) throw new HelperRefusal("The person said no at Windows' own prompt, so nothing was changed.");
                throw;
            }
        }

        // About this computer: no changes, so it only needs the Look allowance.
        [StructLayout(LayoutKind.Sequential)]
        struct MEMORYSTATUSEX { public uint length, load; public ulong total, avail, totalPage, availPage, totalVirtual, availVirtual, extended; }
        [DllImport("kernel32.dll")] static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX m);

        public static Dictionary<string, object> About()
        {
            var d = new Dictionary<string, object>();
            try
            {
                using (var k = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                {
                    var build = Convert.ToInt32(k.GetValue("CurrentBuildNumber") ?? "0");
                    var product = (k.GetValue("ProductName") ?? "Windows").ToString();
                    if (build >= 22000) product = product.Replace("Windows 10", "Windows 11");
                    d["windows"] = product + " " + (k.GetValue("DisplayVersion") ?? "") + " (build " + build + ")";
                }
            }
            catch (Exception) { }
            d["computer_name"] = Environment.MachineName;
            d["running_for"] = TimeSpan.FromMilliseconds(Environment.TickCount & int.MaxValue).ToString(@"d\ \d\a\y\s\ h\ \h\o\u\r\s");
            var m = new MEMORYSTATUSEX { length = (uint)Marshal.SizeOf(typeof(MEMORYSTATUSEX)) };
            if (GlobalMemoryStatusEx(ref m))
                d["memory"] = Math.Round(m.avail / 1e9, 1) + " GB free of " + Math.Round(m.total / 1e9, 1) + " GB (" + m.load + "% in use)";
            var disks = new List<object>();
            foreach (var drive in DriveInfo.GetDrives())
                try { if (drive.DriveType == DriveType.Fixed && drive.IsReady)
                        disks.Add(drive.Name + " " + Math.Round(drive.AvailableFreeSpace / 1e9, 1) + " GB free of " + Math.Round(drive.TotalSize / 1e9, 1) + " GB"); }
                catch (Exception) { }
            d["disks"] = disks;
            var power = SystemInformation.PowerStatus;
            if (power.BatteryChargeStatus != BatteryChargeStatus.NoSystemBattery)
                d["battery"] = (int)(power.BatteryLifePercent * 100) + "%" + (power.PowerLineStatus == PowerLineStatus.Online ? ", plugged in" : ", on battery");
            d["internet"] = System.Net.NetworkInformation.NetworkInterface.GetIsNetworkAvailable() ? "connected to a network" : "no network";
            return d;
        }
    }
}
