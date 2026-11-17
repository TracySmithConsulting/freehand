// FreeHand's own small Linux, and the free Docker engine in it (option A2
// of docs/design/granny-installer.md, proven on a real PC on 16 November
// 2026). The FreeHand app uses this to install, start, update and remove
// FreeHand, so nobody needs Docker Desktop, a terminal or a licence.
//
//   %LOCALAPPDATA%\FreeHand\linux     the Linux's disk (WSL keeps it here)
//   %LOCALAPPDATA%\FreeHand\download  the Linux system, while installing
//   /opt/freehand (inside the Linux)  compose.yml, .env and FreeHand's data
//
// FreeHand's data lives inside the Linux's own disk, not on a Windows
// folder shared into it: databases on shared folders are slow and can be
// damaged by file locking.
//
// Nothing here runs by itself. The FreeHand app calls it when the person
// presses Install, Update, Stop or Uninstall, and it says what it's doing
// through the progress callback, in plain words.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace FreeHand.Helper
{
    class EngineError : Exception { public EngineError(string m) : base(m) { } }

    static class Engine
    {
        public const string Distro = "FreeHand";
        public const string AppDir = "/opt/freehand";
        public const int Port = 8000;

        // Alpine Linux's minimal system: 3.7 MB, checked against Alpine's
        // published SHA-256 before it's used.
        const string AlpineFile = "alpine-minirootfs-3.24.4-x86_64.tar.gz";
        const string AlpineUrl = "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/" + AlpineFile;
        const string AlpineSha256 = "cdaa38cb8de3537967ee5b6c9b6cd00cc962da2b018e6eea48696bb274923012";

        // Its own network ranges, so it can't clash with Docker Desktop or
        // the home network.
        const string DockerdArgs = "--bip 172.31.200.1/24 --default-address-pool base=172.31.208.0/20,size=24";

        public static string Root { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FreeHand"); } }

        /// What happened while installing, step by step, so a person can send
        /// it to whoever helps them. It never holds secrets: commands aren't
        /// written to it, only the steps and what went wrong.
        public static string LogFile { get { return Path.Combine(Root, "install-log.txt"); } }

        public static void Note(string text)
        {
            try
            {
                Directory.CreateDirectory(Root);
                File.AppendAllText(LogFile, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + text + Environment.NewLine);
            }
            catch (Exception) { }
        }

        // ── Checks ──────────────────────────────────────────────────────

        public class Readiness
        {
            public bool WindowsOk, WslReady, Installed, Running;
            public double MemoryGb, FreeDiskGb;
            public List<string> Problems = new List<string>();
            public List<string> Warnings = new List<string>();
        }

        /// What this computer has, and anything in the way, in plain words.
        public static Readiness Check()
        {
            var r = new Readiness();
            // Environment.OSVersion says "Windows 8" to apps without a
            // compatibility manifest, so read the real build number.
            r.WindowsOk = WindowsBuild() >= 19041;
            if (!r.WindowsOk)
                r.Problems.Add("FreeHand needs Windows 10 (version 2004 or newer) or Windows 11. Windows Update can bring this computer up to date.");
            r.MemoryGb = TotalMemoryGb();
            if (r.MemoryGb > 0 && r.MemoryGb < 6)
                r.Problems.Add(string.Format("FreeHand needs a computer with at least 8 GB of memory; this one has about {0:0} GB.", r.MemoryGb));
            else if (r.MemoryGb > 0 && r.MemoryGb < 8)
                r.Warnings.Add(string.Format("This computer has about {0:0} GB of memory. FreeHand will work, but may be slow.", r.MemoryGb));
            try
            {
                var drive = new DriveInfo(Path.GetPathRoot(Root));
                r.FreeDiskGb = drive.AvailableFreeSpace / 1073741824.0;
                if (r.FreeDiskGb < 12)
                    r.Problems.Add(string.Format("FreeHand needs about 12 GB of free space on drive {0}; there's about {1:0} GB. Free some space, then try again.",
                        drive.Name.TrimEnd('\\'), r.FreeDiskGb));
            }
            catch (Exception) { }
            r.WslReady = WslReady();
            r.Installed = r.WslReady && DistroExists();
            r.Running = r.Installed && Answers();
            return r;
        }

        /// Is WSL 2 there and working? (It needs switching on once, with a
        /// Windows "allow changes?" prompt and a restart.)
        public static bool WslReady()
        {
            var o = Run("wsl.exe", "--status", 30);
            return o.Code == 0 && !o.Text.ToLowerInvariant().Contains("not installed");
        }

        public static bool DistroExists()
        {
            var o = Run("wsl.exe", "--list --quiet", 30);
            foreach (var line in o.Text.Split('\n'))
                if (line.Trim() == Distro) return true;
            return false;
        }

        /// Does FreeHand answer at its usual address?
        public static bool Answers()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://localhost:" + Port + "/api/page-version");
                req.Timeout = 4000;
                using (var resp = (HttpWebResponse)req.GetResponse()) return resp.StatusCode == HttpStatusCode.OK;
            }
            catch (Exception) { return false; }
        }

        // ── Installing ──────────────────────────────────────────────────

        /// Switch WSL on. Windows asks "allow changes?" (the person answers),
        /// and a restart is needed afterwards. Returns false if they said no.
        public static bool EnableWsl()
        {
            var psi = new ProcessStartInfo("wsl.exe", "--install --no-distribution") { UseShellExecute = true, Verb = "runas" };
            try
            {
                using (var p = Process.Start(psi)) { p.WaitForExit(); return p.ExitCode == 0; }
            }
            catch (System.ComponentModel.Win32Exception ex)
            {
                if (ex.NativeErrorCode == 1223) return false;  // the person said no
                throw new EngineError("Windows couldn't switch WSL on: " + ex.Message);
            }
        }

        /// Everything after WSL is on: the Linux, the engine, FreeHand's
        /// settings and images, then start it. Safe to run again: each step
        /// is skipped if it's already done.
        public static void Install(Action<int, string> report, CancellationToken cancel)
        {
            Action<int, string> progress = (pct, text) => { Note(pct + "%: " + text); report(pct, text); };
            Note("Installing, on Windows build " + WindowsBuild() + string.Format(" with {0:0.0} GB of memory.", TotalMemoryGb()));
            Note("WSL: " + Run("wsl.exe", "--version", 30).Text.Trim().Replace("\r", "").Replace("\n", "; "));
            if (!WslReady()) throw new EngineError("WSL isn't switched on yet. Press Install again to switch it on.");
            if (!DistroExists())
            {
                progress(5, "Downloading FreeHand's small Linux system (about 4 MB)...");
                var file = DownloadAlpine(cancel);
                progress(10, "Setting up FreeHand's small Linux...");
                Directory.CreateDirectory(Path.Combine(Root, "linux"));  // WSL says "path not found" otherwise
                Must(Run("wsl.exe", string.Format("--import {0} \"{1}\" \"{2}\" --version 2", Distro, Path.Combine(Root, "linux"), file), 300),
                    "Windows couldn't set up FreeHand's Linux");
                try { File.Delete(file); } catch (Exception) { }
            }
            cancel.ThrowIfCancellationRequested();
            if (!Yes("command -v dockerd >/dev/null", 60))
            {
                progress(15, "Downloading the engine that runs FreeHand (about 100 MB)...");
                // A download stopped part-way last time may still be running.
                Must(Sh("pkill -x apk 2>/dev/null; for i in 1 2 3; do apk update && apk add --no-progress docker docker-cli-compose && exit 0; sleep 10; done; exit 1", 3600),
                    "The engine couldn't be downloaded. Check the internet connection, then press Install again");
            }
            cancel.ThrowIfCancellationRequested();
            progress(25, "Writing FreeHand's settings...");
            WriteSettings();
            progress(28, "Starting the engine...");
            StartEngine();
            progress(30, "Downloading FreeHand (about 6 GB the first time). On a slow connection this can take an hour or more...");
            Must(Compose("pull --quiet", 6 * 3600), "FreeHand couldn't be downloaded. Check the internet connection, then press Install again");
            cancel.ThrowIfCancellationRequested();
            progress(90, "Starting FreeHand...");
            Must(Compose("up -d", 900), "FreeHand didn't start");
            progress(95, "Waiting for FreeHand to answer...");
            for (int i = 0; i < 90 && !Answers(); i++) { cancel.ThrowIfCancellationRequested(); Thread.Sleep(2000); }
            if (!Answers()) throw new EngineError("FreeHand started, but isn't answering yet. Wait a minute, then choose Open FreeHand.");
            progress(100, "FreeHand is ready.");
        }

        static string DownloadAlpine(CancellationToken cancel)
        {
            var dir = Path.Combine(Root, "download");
            Directory.CreateDirectory(dir);
            var file = Path.Combine(dir, AlpineFile);
            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    using (var web = new WebClient())
                    {
                        var task = web.DownloadFileTaskAsync(new Uri(AlpineUrl), file);
                        while (!task.Wait(500)) if (cancel.IsCancellationRequested) { web.CancelAsync(); cancel.ThrowIfCancellationRequested(); }
                    }
                    break;
                }
                catch (AggregateException ex)
                {
                    var why = ex.GetBaseException();
                    Note("The Linux download didn't work (try " + attempt + "): " + why.Message);
                    if (attempt >= 3)
                    {
                        var web = why as WebException;
                        if (web != null && (web.Status == WebExceptionStatus.NameResolutionFailure || web.Status == WebExceptionStatus.ConnectFailure))
                            throw new EngineError("This computer couldn't reach the internet to download FreeHand's Linux. Check the connection, then try again.");
                        throw new EngineError("FreeHand's Linux couldn't be downloaded: " + why.Message + " Try again in a little while.");
                    }
                    Thread.Sleep(5000);
                }
            }
            if (Sha256(file) != AlpineSha256)
            {
                File.Delete(file);
                throw new EngineError("The Linux system that was downloaded isn't the one FreeHand expects, so it wasn't used. Try again later.");
            }
            return file;
        }

        /// compose.yml (built into this app) and .env (random secrets, made
        /// once and kept: changing them later would lock FreeHand out of its
        /// own connections).
        public static void WriteSettings()
        {
            Must(Sh("mkdir -p " + AppDir + "/vault", 30), "FreeHand's folder couldn't be made");
            WriteFile(AppDir + "/compose.yml", ComposeFile());
            if (!Yes("test -s " + AppDir + "/.env", 60))
            {
                var env = new StringBuilder();
                env.Append("OC_ADMIN_TOKEN=").Append(RandomHex(32)).Append('\n');
                env.Append("OC_RUNTIME_TOKEN=").Append(RandomHex(32)).Append('\n');
                env.Append("OC_ENCRYPTION_KEY=").Append(RandomBase64(32)).Append('\n');
                env.Append("FREEHAND_LLM_PROXY_TOKEN=").Append(RandomHex(32)).Append('\n');
                env.Append("BRIDGE_SECRET=").Append(RandomHex(32)).Append('\n');
                WriteFile(AppDir + "/.env", env.ToString());
                Must(Sh("chmod 600 " + AppDir + "/.env", 30), "FreeHand's secrets couldn't be protected");
            }
        }

        static string ComposeFile()
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("compose.yml"))
            {
                if (s == null) throw new EngineError("This copy of the FreeHand app is incomplete (compose.yml is missing). Download it again.");
                using (var reader = new StreamReader(s)) return reader.ReadToEnd().Replace("\r\n", "\n");
            }
        }

        // ── Running ─────────────────────────────────────────────────────

        static Process engine;

        /// Start the Docker engine and keep it running while this app runs.
        /// Alpine has no service manager under WSL, so dockerd only runs
        /// while something holds it: this app does.
        public static void StartEngine()
        {
            if (Yes("docker info >/dev/null 2>&1", 60)) return;
            if (Yes("pgrep -x dockerd >/dev/null", 60))
                Note("The engine is already starting; waiting for it rather than starting a second one.");
            else
            {
                Sh("rm -f /var/run/docker.pid", 30);  // left behind if the computer was switched off
                var psi = new ProcessStartInfo("wsl.exe", "-d " + Distro + " -- sh -c \"mkdir -p /var/log; exec dockerd " + DockerdArgs + " > /var/log/dockerd.log 2>&1\"")
                { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
                engine = Process.Start(psi);
            }
            // An older computer can take a few minutes to start it the first time.
            var until = DateTime.Now.AddMinutes(4);
            while (DateTime.Now < until)
            {
                Thread.Sleep(3000);
                if (Yes("docker info >/dev/null 2>&1", 60)) return;
                if (engine != null && engine.HasExited) break;  // it stopped by itself: waiting longer won't help
            }
            var why = Sh("tail -n 25 /var/log/dockerd.log", 30).Text.Trim();
            Note("The engine didn't start. Its last words:" + Environment.NewLine + why);
            Note("What the check saw: " + Sh("docker info 2>&1 | tail -n 15", 60).Text.Trim());
            throw new EngineError("The engine that runs FreeHand didn't start. Restarting the computer usually helps." + Reason(why));
        }

        /// Start FreeHand (when the app starts with Windows, for example).
        public static void Start()
        {
            StartEngine();
            Must(Compose("up -d", 900), "FreeHand didn't start");
        }

        /// Download the newest FreeHand and restart it. The data stays.
        public static void Update(Action<int, string> progress)
        {
            StartEngine();
            WriteSettings();  // a newer app may bring a newer compose.yml
            progress(10, "Downloading the newest FreeHand...");
            Must(Compose("pull --quiet", 6 * 3600), "The update couldn't be downloaded. Check the internet connection, then try again");
            progress(80, "Restarting FreeHand...");
            Must(Compose("up -d", 900), "FreeHand didn't restart");
            progress(100, "FreeHand is up to date.");
        }

        /// Stop FreeHand and the engine. The data stays.
        public static void Stop()
        {
            if (DistroExists()) Compose("down", 300);
            Run("wsl.exe", "--terminate " + Distro, 60);
            engine = null;
        }

        /// Remove FreeHand. With keepData, FreeHand's data (memories,
        /// settings, connections) is first copied to a Windows folder; the
        /// folder's path is returned.
        public static string Uninstall(bool keepData)
        {
            string kept = null;
            if (DistroExists() && keepData)
            {
                kept = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "FreeHand data " + DateTime.Now.ToString("yyyy-MM-dd"));
                Directory.CreateDirectory(kept);
                var tar = Path.Combine(kept, "freehand-data.tar");
                Compose("down", 300);
                Must(Sh("tar -cf \"$(wslpath -a '" + tar.Replace("'", "'\\''") + "')\" -C " + AppDir + " .env vault", 1800),
                    "FreeHand's data couldn't be copied, so nothing was removed");
            }
            Stop();
            if (DistroExists()) Must(Run("wsl.exe", "--unregister " + Distro, 300), "Windows couldn't remove FreeHand's Linux");
            try { Directory.Delete(Path.Combine(Root, "linux"), true); } catch (Exception) { }
            return kept;
        }

        // ── Helpers ─────────────────────────────────────────────────────

        // Text is everything, for error messages; Out is only the command's
        // own answer. On some computers WSL adds notices of its own (about a
        // proxy, or a Windows path it can't use), so checks read only Out.
        public class Output { public int Code; public string Text = ""; public string Out = ""; }

        /// Does this check say yes? Only the command's own answer counts.
        static bool Yes(string check, int seconds)
        {
            var o = Sh(check + " && echo yes", seconds);
            foreach (var line in o.Out.Split('\n'))
                if (line.Trim() == "yes") return true;
            return false;
        }

        public static Output Run(string file, string args, int seconds)
        {
            var psi = new ProcessStartInfo(file, args)
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            if (file == "wsl.exe") psi.EnvironmentVariables["WSL_UTF8"] = "1";  // wsl.exe's own messages, as plain text
            using (var p = Process.Start(psi))
            {
                var stdout = p.StandardOutput.ReadToEndAsync();
                var stderr = p.StandardError.ReadToEndAsync();
                if (!p.WaitForExit(seconds * 1000))
                {
                    try { p.Kill(); } catch (Exception) { }
                    return new Output { Code = -1, Text = "It took too long." };
                }
                return new Output { Code = p.ExitCode, Text = (stdout.Result + stderr.Result).Replace("\0", ""), Out = stdout.Result.Replace("\0", "") };
            }
        }

        static Output Sh(string command, int seconds)
        {
            return Run("wsl.exe", "-d " + Distro + " -- sh -c \"" + command.Replace("\"", "\\\"") + "\"", seconds);
        }

        static Output Compose(string args, int seconds)
        {
            return Sh("cd " + AppDir + " && docker compose " + args, seconds);
        }

        static void WriteFile(string path, string text)
        {
            var b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(text));
            Must(Sh("echo " + b64 + " | base64 -d > " + path, 60), "FreeHand's settings couldn't be written");
        }

        static void Must(Output o, string plain)
        {
            if (o.Code != 0)
            {
                var detail = o.Text.Trim();
                Note(plain + ". " + detail);
                if (detail.Length > 300) detail = detail.Substring(detail.Length - 300);
                throw new EngineError(plain + "." + (detail.Length > 0 ? " (Details: " + detail + ")" : ""));
            }
        }

        /// The engine's own reason, when it's one a person can do something about.
        static string Reason(string log)
        {
            var l = log.ToLowerInvariant();
            if (l.Contains("iptables") || l.Contains("nf_tables") || l.Contains("kernel"))
                return " (The engine says this computer's Linux is missing a part it needs. Updating WSL can fix that: the FreeHand team can help.)";
            if (l.Contains("no space left"))
                return " (The computer's disk is full. Free some space, then try again.)";
            return "";
        }

        static string Sha256(string file)
        {
            using (var sha = SHA256.Create())
            using (var s = File.OpenRead(file))
                return BitConverter.ToString(sha.ComputeHash(s)).Replace("-", "").ToLowerInvariant();
        }

        static byte[] RandomBytes(int n)
        {
            var b = new byte[n];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(b);
            return b;
        }

        static string RandomHex(int n) { return BitConverter.ToString(RandomBytes(n)).Replace("-", "").ToLowerInvariant(); }

        static string RandomBase64(int n) { return Convert.ToBase64String(RandomBytes(n)); }

        [StructLayout(LayoutKind.Sequential)]
        class MemoryStatus
        {
            public uint Length = (uint)Marshal.SizeOf(typeof(MemoryStatus));
            public uint MemoryLoad;
            public ulong TotalPhys, AvailPhys, TotalPageFile, AvailPageFile, TotalVirtual, AvailVirtual, AvailExtendedVirtual;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool GlobalMemoryStatusEx([In, Out] MemoryStatus status);

        static int WindowsBuild()
        {
            try
            {
                using (var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                {
                    int build;
                    return key != null && int.TryParse(Convert.ToString(key.GetValue("CurrentBuildNumber")), out build) ? build : 0;
                }
            }
            catch (Exception) { return 0; }
        }

        static double TotalMemoryGb()
        {
            var m = new MemoryStatus();
            return GlobalMemoryStatusEx(m) ? m.TotalPhys / 1073741824.0 : 0;
        }
    }
}
