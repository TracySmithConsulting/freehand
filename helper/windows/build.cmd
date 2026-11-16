@echo off
rem Build the FreeHand Helper with the C# compiler that comes with Windows.
rem Nothing to install. Makes FreeHandHelper.exe next to this file.
setlocal
cd /d "%~dp0"
set NET=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319
if not exist "%NET%\csc.exe" set NET=%WINDIR%\Microsoft.NET\Framework\v4.0.30319
if not exist "%NET%\csc.exe" (
  echo Couldn't find the .NET Framework C# compiler. It comes with Windows 10 and 11.
  exit /b 1
)
"%NET%\csc.exe" /nologo /noconfig /nostdlib- /r:"%NET%\mscorlib.dll" /target:winexe /optimize+ /out:"%~dp0FreeHandHelper.exe" ^
  /r:"%NET%\System.dll" /r:"%NET%\System.Core.dll" /r:"%NET%\System.Drawing.dll" /r:"%NET%\System.Windows.Forms.dll" ^
  /r:"%NET%\System.Web.Extensions.dll" /r:"%NET%\System.Security.dll" ^
  /r:"%NET%\WPF\UIAutomationClient.dll" /r:"%NET%\WPF\UIAutomationTypes.dll" /r:"%NET%\WPF\WindowsBase.dll" ^
  /resource:"%~dp0..\..\install\compose.yml",compose.yml ^
  "%~dp0FreeHandHelper.cs" "%~dp0Engine.cs"
if errorlevel 1 exit /b 1
echo Built %~dp0FreeHandHelper.exe
