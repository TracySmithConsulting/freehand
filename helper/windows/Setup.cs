// The FreeHand app's setup screens (docs/design/granny-installer.md, "The
// installer, in more detail"): welcome, checks, switching WSL on (with a
// restart), installing with progress, and done. Everything is reachable
// with the keyboard and a screen reader: each screen's first line has the
// focus when it appears, and progress is a list whose newest line is
// selected, so a screen reader reads each step as it happens.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace FreeHand.Helper
{
    class SetupForm : Form
    {
        readonly Settings settings;
        readonly Panel page = new Panel { Dock = DockStyle.Fill, Padding = new Padding(16), AutoScroll = true };
        readonly FlowLayoutPanel buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Padding = new Padding(12) };
        CancellationTokenSource cancel;
        public bool Installed { get; private set; }

        public SetupForm(Settings settings)
        {
            this.settings = settings;
            Text = "Set up FreeHand";
            Width = 640; Height = 520;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 11f);
            Controls.Add(page);
            Controls.Add(buttons);
            FormClosing += delegate(object s, FormClosingEventArgs e)
            {
                if (cancel != null && !cancel.IsCancellationRequested)
                {
                    if (Ask("FreeHand is still being set up. Stop now? You can start again later, and it carries on from where it stopped."))
                        cancel.Cancel();
                    else e.Cancel = true;
                }
            };
            Load += delegate { Welcome(); };
        }

        // ── Building a screen ───────────────────────────────────────────

        string heading = "";

        TableLayoutPanel Screen(string heading)
        {
            this.heading = heading;
            Text = "Set up FreeHand: " + heading;  // a screen reader reads the window's title
            page.Controls.Clear();
            buttons.Controls.Clear();
            var layout = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 1, AutoSize = true };
            var h = new Label { Text = heading, AutoSize = true, Font = new Font("Segoe UI", 15f, FontStyle.Bold), Margin = new Padding(0, 0, 0, 12),
                AccessibleRole = AccessibleRole.StaticText };
            layout.Controls.Add(h);
            page.Controls.Add(layout);
            return layout;
        }

        static Label Para(TableLayoutPanel layout, string text)
        {
            var l = new Label { Text = text, AutoSize = true, MaximumSize = new Size(570, 0), Margin = new Padding(0, 0, 0, 10) };
            layout.Controls.Add(l);
            return l;
        }

        Button AddButton(string text, Action onClick, bool main = false)
        {
            // The main button comes first when pressing Tab, then the others.
            var b = new Button { Text = text, AutoSize = true, Padding = new Padding(8, 4, 8, 4), TabIndex = main ? 0 : 10 + buttons.Controls.Count };
            b.Click += delegate { onClick(); };
            buttons.Controls.Add(b);
            if (main) AcceptButton = b;
            return b;
        }

        // The first thing a screen reader should read: a read-only box with
        // the screen's words, named after its heading, which takes the focus.
        TextBox Words(TableLayoutPanel layout, string text)
        {
            var box = new TextBox { Text = text, ReadOnly = true, Multiline = true, BorderStyle = BorderStyle.None, Width = 570, Height = 110,
                BackColor = SystemColors.Control, TabStop = true, AccessibleName = heading, ScrollBars = ScrollBars.Vertical };
            layout.Controls.Add(box);
            return box;
        }

        bool Ask(string question)
        {
            return MessageBox.Show(this, question, "FreeHand", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
        }

        // ── A look at every screen, for checking a build ────────────────

        /// Builds each screen in turn (without doing anything) and says what
        /// it shows: its heading, its words, its buttons, and what has the
        /// focus. Used by --selftest-setup.
        public List<Dictionary<string, object>> Preview()
        {
            CreateControl();
            var shots = new List<Dictionary<string, object>>();
            Action<string, Action> shot = (name, build) =>
            {
                build();
                var controls = new List<string>();
                Control focus = null;
                foreach (Control c in page.Controls[0].Controls)
                {
                    controls.Add(c.GetType().Name + ": " + (c.AccessibleName ?? "") + " | " + c.Text);
                    if (c is TextBox && focus == null) focus = c;
                }
                var names = new List<string>();
                foreach (Control b in buttons.Controls) names.Add(b.Text);
                names.Reverse();  // right to left on screen
                shots.Add(new Dictionary<string, object> {
                    { "screen", name }, { "controls", controls }, { "buttons", names },
                    { "default_button", AcceptButton is Button ? ((Button)AcceptButton).Text : "" },
                    { "first_focus", focus == null ? "" : focus.GetType().Name + " (" + (focus.AccessibleName ?? "") + ")" },
                });
            };
            shot("welcome", Welcome);
            shot("problem", () => ShowProblem("This computer can't run FreeHand yet:\r\n\r\n- (example problem)"));
            shot("need_wsl", () => NeedWsl(""));
            shot("ready", () => ReadyToInstall(""));
            shot("done", () => Done("FreeHand is installed and running."));
            return shots;
        }

        // ── 1. Welcome ──────────────────────────────────────────────────

        RadioButton here, server;

        void Welcome()
        {
            var l = Screen("Welcome to FreeHand");
            var words = Words(l, "FreeHand is an assistant you talk to in your own words. It can answer questions, read and write emails, " +
                "keep your diary, remind you of things, and help on this computer.\r\n\r\n" +
                "Where should FreeHand run?");
            here = new RadioButton { Text = "On this computer (install it here)", AutoSize = true, Checked = true };
            server = new RadioButton { Text = "Somewhere else: FreeHand already runs on a server, or was set up another way", AutoSize = true };
            l.Controls.Add(here);
            l.Controls.Add(server);
            AddButton("Cancel", Close);
            AddButton("Next", delegate
            {
                if (server.Checked) { settings.Mode = "server"; settings.Save(); DialogResult = DialogResult.Ignore; Close(); }
                else Checks();
            }, true);
            words.Focus();
        }

        // ── 2. Checks ───────────────────────────────────────────────────

        void Checks()
        {
            var l = Screen("Checking this computer");
            var words = Words(l, "Checking that this computer can run FreeHand...");
            words.Focus();
            Task.Run(() => Engine.Check()).ContinueWith(t =>
            {
                if (t.IsFaulted) { ShowProblem("FreeHand couldn't check this computer: " + t.Exception.GetBaseException().Message); return; }
                var r = t.Result;
                if (r.Problems.Count > 0)
                {
                    ShowProblem("This computer can't run FreeHand yet:\r\n\r\n- " + string.Join("\r\n- ", r.Problems) +
                        "\r\n\r\nIf FreeHand already runs somewhere else, go back and choose \"Somewhere else\".");
                    return;
                }
                if (r.Running) { Done("FreeHand is already installed and running on this computer."); return; }
                var notes = r.Warnings.Count > 0 ? "\r\n\r\nNote: " + string.Join(" ", r.Warnings) : "";
                if (!r.WslReady) { NeedWsl(notes); return; }
                ReadyToInstall(notes);
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        void ShowProblem(string text)
        {
            var l = Screen("FreeHand can't be installed yet");
            var words = Words(l, text);
            AddButton("Close", Close, true);
            AddButton("Back", Welcome);
            words.Focus();
        }

        // ── 3. WSL ──────────────────────────────────────────────────────

        void NeedWsl(string notes)
        {
            var l = Screen("One thing to switch on first");
            var words = Words(l, "FreeHand runs in its own small Linux, inside Windows. Windows needs a part called WSL switched on for that, " +
                "once.\r\n\r\nWhen you press Switch on, Windows asks \"Do you want to allow this app to make changes?\". Choose Yes. " +
                "Then the computer needs to restart. After the restart, FreeHand carries on by itself.\r\n\r\n" +
                "On most computers this takes a few minutes. On an older computer it can take an hour or two: Windows shows how far it has got." + notes);
            AddButton("Cancel", Close);
            AddButton("Switch on", SwitchOnWsl, true);
            words.Focus();
        }

        void SwitchOnWsl()
        {
            var l = Screen("Switching WSL on");
            var words = Words(l, "Windows is switching WSL on. If it asks \"Do you want to allow this app to make changes?\", choose Yes. This usually takes a few minutes, but on an older computer it can take an hour or two. Windows shows how far it has got in its own window.");
            words.Focus();
            Task.Run(() => Engine.EnableWsl()).ContinueWith(t =>
            {
                if (t.IsFaulted) { ShowProblem(t.Exception.GetBaseException().Message); return; }
                if (!t.Result)
                {
                    ShowProblem("WSL wasn't switched on, because Windows was told No (or it didn't finish). FreeHand needs it. " +
                        "Press Back, then Next, to try again.");
                    return;
                }
                // Carry on after the restart, by itself.
                try
                {
                    using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\RunOnce"))
                        key.SetValue("FreeHand setup", "\"" + Application.ExecutablePath + "\" --setup");
                }
                catch (Exception) { }
                var done = Screen("Restart the computer");
                var w = Words(done, "WSL is switched on. The computer needs to restart to finish. Save anything you're working on, " +
                    "then press Restart now. After the restart, FreeHand's setup opens again and carries on.");
                AddButton("Restart later", Close);
                AddButton("Restart now", delegate { Process.Start(new ProcessStartInfo("shutdown.exe", "/r /t 5") { CreateNoWindow = true, UseShellExecute = false }); Close(); }, true);
                w.Focus();
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        // ── 4. Install ──────────────────────────────────────────────────

        void ReadyToInstall(string notes)
        {
            var l = Screen("Ready to install");
            var words = Words(l, "FreeHand will now set itself up. It downloads about 1.8 GB the first time and takes about 7 GB of space, so on a slow internet connection " +
                "this can take an hour or more. It says how far it has got as it goes. You can use the computer while it works, and close this window: it carries on " +
                "from where it stopped next time.\r\n\r\nFreeHand stays on this computer: only this computer can reach it." + notes);
            AddButton("Cancel", Close);
            AddButton("Install", Install, true);
            words.Focus();
        }

        void Install()
        {
            var l = Screen("Installing FreeHand");
            var bar = new ProgressBar { Width = 570, Height = 24, Minimum = 0, Maximum = 100, AccessibleName = "Progress" };
            l.Controls.Add(bar);
            var steps = new ListBox { Width = 570, Height = 200, AccessibleName = "What FreeHand is doing", HorizontalScrollbar = true };
            l.Controls.Add(steps);
            AddButton("Stop", delegate { if (cancel != null) cancel.Cancel(); });
            steps.Focus();
            cancel = new CancellationTokenSource();
            var token = cancel.Token;
            var ui = SynchronizationContext.Current;
            Action<int, string> progress = (pct, text) => ui.Post(_ =>
            {
                if (IsDisposed) return;
                bar.Value = Math.Max(0, Math.Min(100, pct));
                if (text == null) return;  // only the bar moves
                steps.Items.Add(pct + "%: " + text);
                steps.SelectedIndex = steps.Items.Count - 1;  // a screen reader reads the newest step
            }, null);
            Task.Run(() => Engine.Install(progress, token), token).ContinueWith(t =>
            {
                cancel = null;
                if (t.IsCanceled || (t.Exception != null && t.Exception.GetBaseException() is OperationCanceledException))
                {
                    ShowProblem("Setting up stopped. Nothing is lost: press Back, then Next, and FreeHand carries on from where it stopped.");
                    return;
                }
                if (t.IsFaulted)
                {
                    ShowProblem(t.Exception.GetBaseException().Message + "\r\n\r\nPress Back, then Next, to try again. If it keeps happening, send this file to whoever helps you with FreeHand: "
                        + Engine.LogFile);
                    return;
                }
                settings.Mode = "local";
                settings.Save();
                StartWithWindows(true);
                Installed = true;
                Done("FreeHand is installed and running.");
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        // ── 5. Done ─────────────────────────────────────────────────────

        public bool PreviewOnly;

        void Done(string first)
        {
            if (!PreviewOnly)
            {
                settings.Mode = "local";
                settings.Save();
                Installed = true;
            }
            var l = Screen("FreeHand is ready");
            var words = Words(l, first + "\r\n\r\nPress Open FreeHand. Your web browser opens at FreeHand, which helps you choose how it thinks, " +
                "in a few short steps.\r\n\r\nLater, you'll find FreeHand by the clock: right-click its icon to open, update or stop it.");
            var auto = new CheckBox { Text = "Start FreeHand when Windows starts", AutoSize = true, Checked = true };
            auto.CheckedChanged += delegate { if (!PreviewOnly) StartWithWindows(auto.Checked); };
            l.Controls.Add(auto);
            AddButton("Close", Close);
            AddButton("Open FreeHand", delegate { OpenFreeHand(); Close(); }, true);
            words.Focus();
        }

        public static void OpenFreeHand()
        {
            try { Process.Start("http://localhost:" + Engine.Port + "/"); } catch (Exception) { }
        }

        /// The app starts with Windows (in the background, by the clock), and
        /// starts FreeHand.
        public static void StartWithWindows(bool on)
        {
            try
            {
                using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
                {
                    if (on) key.SetValue("FreeHand", "\"" + Application.ExecutablePath + "\" --background");
                    else key.DeleteValue("FreeHand", false);
                }
            }
            catch (Exception) { }
        }
    }

    /// A small window with progress, for Update and Uninstall from the tray.
    class WorkForm : Form
    {
        readonly ListBox steps = new ListBox { Dock = DockStyle.Fill, AccessibleName = "What FreeHand is doing", HorizontalScrollbar = true };

        public WorkForm(string title)
        {
            Text = title;
            Width = 560; Height = 300;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 11f);
            Controls.Add(steps);
        }

        public void Say(string text)
        {
            if (IsDisposed) return;
            if (InvokeRequired) { BeginInvoke((Action)(() => Say(text))); return; }
            steps.Items.Add(text);
            steps.SelectedIndex = steps.Items.Count - 1;
        }

        protected override void OnShown(EventArgs e) { base.OnShown(e); steps.Focus(); }
    }
}
