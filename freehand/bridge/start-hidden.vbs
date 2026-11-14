' Starts the WhatsApp bridge with no window, for Windows' Task Scheduler
' (scripts/windows-bridge-autostart.ps1 sets that up). Output goes to
' %LOCALAPPDATA%\FreeHand\whatsapp-bridge.log.
'
' If the bridge stops for any reason, this starts it again 10 seconds
' later. (Task Scheduler's own "restart on failure" doesn't count a
' program that exits as a failure, so it can't be relied on for this.)
' Stopping the "FreeHand WhatsApp bridge" task stops this loop too.
Option Explicit
Dim shell, fso, here, logDir, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\FreeHand"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
shell.CurrentDirectory = here
cmd = "cmd /c node bridge-server.js >> """ & logDir & "\whatsapp-bridge.log"" 2>&1"
Do
    shell.Run cmd, 0, True
    WScript.Sleep 10000
Loop
