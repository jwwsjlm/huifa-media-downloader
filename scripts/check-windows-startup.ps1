[CmdletBinding()]
param([string]$Executable)

$ErrorActionPreference = 'Stop'
$taskWorkspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
if (-not $Executable) {
    $taskMetadata = cargo metadata --locked --format-version 1 --no-deps | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the desktop build directory' }
    $Executable = Join-Path $taskMetadata.target_directory 'release/huifa-desktop.exe'
}
$Executable = (Resolve-Path -LiteralPath $Executable).ProviderPath
if ([IO.Path]::GetExtension($Executable) -ine '.exe') { throw 'Choose a desktop EXE file' }
$taskOutput = Join-Path $taskWorkspace ('output/windows-startup/' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskOutput | Out-Null
$taskReport = Join-Path $taskOutput 'result.txt'

if (-not ('HuifaStartupWindows' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class HuifaStartupWindows {
    private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int length);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    public static IntPtr[] ErrorDialogs(int targetProcessId) {
        var found = new List<IntPtr>();
        EnumWindows(delegate(IntPtr window, IntPtr parameter) {
            uint processId;
            GetWindowThreadProcessId(window, out processId);
            if (processId != (uint)targetProcessId || !IsWindowVisible(window)) return true;
            var name = new StringBuilder(256);
            var title = new StringBuilder(256);
            GetClassName(window, name, name.Capacity);
            GetWindowText(window, title, title.Capacity);
            if (name.ToString() == "#32770" && title.ToString() == "Huifa Media Downloader") found.Add(window);
            return true;
        }, IntPtr.Zero);
        return found.ToArray();
    }
    public static bool Accept(IntPtr window) {
        return PostMessage(window, 0x0111, new IntPtr(1), IntPtr.Zero);
    }
}
'@
}

function Start-Desktop([string]$Scenario, [string]$DataPath) {
    $directory = Join-Path $taskOutput $Scenario
    foreach ($name in @('local', 'temp', 'downloads', 'webview')) {
        New-Item -ItemType Directory -Path (Join-Path $directory $name) -Force | Out-Null
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.WorkingDirectory = Split-Path -Parent $Executable
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.EnvironmentVariables['HUIFA_DATA_DIR'] = $DataPath
    $process.StartInfo.EnvironmentVariables['HUIFA_DOWNLOAD_DIR'] = Join-Path $directory 'downloads'
    $process.StartInfo.EnvironmentVariables['WEBVIEW2_USER_DATA_FOLDER'] = Join-Path $directory 'webview'
    $process.StartInfo.EnvironmentVariables['LOCALAPPDATA'] = Join-Path $directory 'local'
    $process.StartInfo.EnvironmentVariables['TEMP'] = Join-Path $directory 'temp'
    $process.StartInfo.EnvironmentVariables['TMP'] = Join-Path $directory 'temp'
    if (-not $process.Start()) { throw "$Scenario process did not start" }
    return @{ Process = $process; ErrorOutput = $process.StandardError.ReadToEndAsync(); Directory = $directory }
}

function Stop-Desktop($Run) {
    if (-not $Run) { return }
    $process = $Run.Process
    if (-not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-File -LiteralPath (Join-Path $Run.Directory 'cleanup.txt')
        [void]$process.WaitForExit(10000)
    }
    if ($Run.ErrorOutput.Wait(2000)) {
        [IO.File]::WriteAllText((Join-Path $Run.Directory 'stderr.txt'), $Run.ErrorOutput.GetAwaiter().GetResult())
    }
    $process.Dispose()
}

$taskNormal = $null
$taskFailure = $null
try {
    "Executable: $Executable" | Out-File -LiteralPath $taskReport
    $taskData = Join-Path $taskOutput 'normal/data'
    $taskNormal = Start-Desktop 'normal' $taskData
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(45)
    $taskReady = $false
    do {
        if ($taskNormal.Process.HasExited) { throw "Normal startup exited: $($taskNormal.Process.ExitCode)" }
        $taskBrowsers = @(Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe' AND ParentProcessId = $($taskNormal.Process.Id)")
        $taskReady = (Test-Path -LiteralPath (Join-Path $taskData 'rust-core.sqlite3') -PathType Leaf) -and $taskBrowsers.Count -gt 0
        if ($taskReady) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $taskDeadline)
    if (-not $taskReady) { throw 'Normal startup did not initialize SQLite and its WebView2 child' }
    Start-Sleep -Seconds 2
    if ($taskNormal.Process.HasExited) { throw 'Desktop exited immediately after initialization' }
    if (-not $taskNormal.Process.CloseMainWindow()) { throw 'Could not request normal desktop shutdown' }
    if (-not $taskNormal.Process.WaitForExit(15000)) { throw 'Desktop did not finish saving and exit after close' }
    if ($taskNormal.Process.ExitCode -ne 0) { throw "Normal shutdown exited: $($taskNormal.Process.ExitCode)" }
    if (-not (Test-Path -LiteralPath (Join-Path $taskData 'rust-core.sqlite3') -PathType Leaf)) { throw 'Database missing after shutdown' }
    'PASS: normal startup, SQLite, WebView2 child, stable process and graceful shutdown' | Add-Content -LiteralPath $taskReport
    Stop-Desktop $taskNormal
    $taskNormal = $null

    $taskBlocked = Join-Path $taskOutput 'failure/data-is-a-file'
    New-Item -ItemType Directory -Path (Split-Path -Parent $taskBlocked) -Force | Out-Null
    [IO.File]::WriteAllText($taskBlocked, 'keep original data')
    $taskOriginalHash = (Get-FileHash -LiteralPath $taskBlocked -Algorithm SHA256).Hash
    $taskFailure = Start-Desktop 'failure' $taskBlocked
    $taskLog = Join-Path $taskFailure.Directory 'local/HuifaDownloaderRust/startup-error.log'
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(45)
    $taskDialog = [IntPtr]::Zero
    do {
        if ($taskFailure.Process.HasExited) { throw "Failure exited before showing its native error dialog: $($taskFailure.Process.ExitCode)" }
        $taskDialogs = @([HuifaStartupWindows]::ErrorDialogs($taskFailure.Process.Id))
        if ($taskDialogs.Count -eq 1 -and (Test-Path -LiteralPath $taskLog -PathType Leaf)) {
            $taskDialog = $taskDialogs[0]
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $taskDeadline)
    if ($taskDialog -eq [IntPtr]::Zero) { throw 'Startup failure did not show its native Huifa error dialog and write the fallback log' }
    $taskLogText = [IO.File]::ReadAllText($taskLog)
    if ($taskLogText -notmatch 'Could not create the application data folder') { throw 'Startup log did not preserve the data-folder failure context' }
    if ((Get-FileHash -LiteralPath $taskBlocked -Algorithm SHA256).Hash -ne $taskOriginalHash) { throw 'Startup failure modified the original data-path file' }
    if (-not [HuifaStartupWindows]::Accept($taskDialog)) { throw 'Could not acknowledge the target error dialog' }
    if (-not $taskFailure.Process.WaitForExit(10000)) { throw 'Desktop did not exit after its startup error was acknowledged' }
    if ($taskFailure.Process.ExitCode -eq 0) { throw 'Startup failure reported a successful exit code' }
    'PASS: native error dialog, fallback startup log, preserved original data and nonzero exit' | Add-Content -LiteralPath $taskReport
    Write-Output "Windows startup checks passed: $taskOutput"
} catch {
    "FAIL: $($_.Exception.Message)" | Add-Content -LiteralPath $taskReport
    throw
} finally {
    Stop-Desktop $taskNormal
    Stop-Desktop $taskFailure
    Write-Output "Windows startup evidence: $taskOutput"
}
