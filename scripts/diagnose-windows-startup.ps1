[CmdletBinding()]
param([string]$Executable)

$ErrorActionPreference = 'Stop'

if (-not $Executable) {
    $guess = $null
    foreach ($directory in @($PSScriptRoot, (Get-Location).Path)) {
        foreach ($name in @('HuifaDownloader.exe', 'huifa-desktop.exe')) {
            $candidate = Join-Path $directory $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $guess = $candidate; break }
        }
        if ($guess) { break }
    }
    $prompt = 'Enter or drag in the application EXE path'
    if ($guess) { $prompt += " (Enter uses $guess)" }
    $Executable = Read-Host $prompt
    if (-not $Executable) { $Executable = $guess }
}
if (-not $Executable) { throw 'An application EXE path is required.' }
$Executable = (Resolve-Path -LiteralPath $Executable.Trim().Trim('"').Trim("'")).ProviderPath
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf) -or [IO.Path]::GetExtension($Executable) -ine '.exe') {
    throw 'Choose an existing application EXE file.'
}

function Protect-Text([string]$Text) {
    if ($env:USERPROFILE) {
        $Text = [regex]::Replace($Text, [regex]::Escape($env:USERPROFILE), '<USERPROFILE>', 'IgnoreCase')
        $Text = [regex]::Replace($Text, [regex]::Escape($env:USERPROFILE.Replace('\', '/')), '<USERPROFILE>', 'IgnoreCase')
    }
    return $Text
}

$resultDirectory = Join-Path ([IO.Path]::GetTempPath()) ('HuifaStartup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $resultDirectory | Out-Null
$reportPath = Join-Path $resultDirectory 'startup-report.txt'
$report = New-Object System.Text.StringBuilder
[void]$report.AppendLine('Huifa Windows startup diagnostic')
[void]$report.AppendLine("Executable: $Executable")
$executableVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($Executable)
[void]$report.AppendLine("Executable version: product $($executableVersion.ProductVersion); file $($executableVersion.FileVersion)")

$version = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$architecture = $env:PROCESSOR_ARCHITEW6432
if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
[void]$report.AppendLine("Windows registry product: $($version.ProductName); release $($version.DisplayVersion); build $($version.CurrentBuildNumber).$($version.UBR); architecture $architecture")
[void]$report.AppendLine('WebView2 registry versions:')
$webviewKeys = @(
    'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
)
foreach ($key in $webviewKeys) {
    try {
        $pv = (Get-ItemProperty -LiteralPath $key -Name pv).pv
        [void]$report.AppendLine("  $key : $pv")
    } catch {
        [void]$report.AppendLine("  $key : unavailable ($($_.Exception.Message))")
    }
}

$started = Get-Date
[void]$report.AppendLine("Started: $($started.ToString('o'))")
$process = New-Object System.Diagnostics.Process
$process.StartInfo.FileName = $Executable
$process.StartInfo.WorkingDirectory = Split-Path -Parent $Executable
$process.StartInfo.UseShellExecute = $false
$process.StartInfo.RedirectStandardError = $true
try {
    if (-not $process.Start()) { throw 'Windows did not start the process.' }
    $stderr = $process.StandardError.ReadToEndAsync()
    [void]$report.AppendLine("Process ID: $($process.Id)")
    if ($process.WaitForExit(15000)) {
        $exitCode = $process.ExitCode
        $unsignedCode = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$exitCode), 0)
        [void]$report.AppendLine(('Exited: {0} (0x{1:X8})' -f $exitCode, $unsignedCode))
        [void]$report.AppendLine('Standard error:')
        if ($stderr.Wait(1000)) {
            [void]$report.AppendLine($stderr.GetAwaiter().GetResult())
        } else {
            [void]$report.AppendLine('Capture did not close after the application exited.')
        }
        $process.Dispose()
    } else {
        [void]$report.AppendLine('Still running after 15 seconds. The application was left open.')
        [void]$report.AppendLine('Standard error capture is incomplete while the application is running.')
    }
} catch {
    [void]$report.AppendLine("Launch/capture error: $($_.Exception.Message)")
}
$finished = Get-Date
[void]$report.AppendLine("Finished: $($finished.ToString('o'))")
[void]$report.AppendLine('Matching Application Error / Windows Error Reporting events during this attempt:')
try {
    $events = Get-WinEvent -FilterHashtable @{
        LogName = 'Application'
        ProviderName = @('Application Error', 'Windows Error Reporting')
        StartTime = $started
        EndTime = $finished
    } -ErrorAction Stop
    $namePattern = '(?i)(?<![\w.-])' + [regex]::Escape([IO.Path]::GetFileName($Executable)) + '(?![\w.-])'
    $matched = @($events | Where-Object { $_.Message -match $namePattern })
    if ($matched.Count -eq 0) { [void]$report.AppendLine('None found.') }
    foreach ($event in $matched) {
        [void]$report.AppendLine("$($event.TimeCreated.ToString('o')) | $($event.ProviderName) | event $($event.Id)")
        [void]$report.AppendLine($event.Message)
    }
} catch {
    [void]$report.AppendLine("Event query: $($_.Exception.Message)")
}

[IO.File]::WriteAllText($reportPath, (Protect-Text $report.ToString()), (New-Object System.Text.UTF8Encoding($true)))
Write-Host "Result folder: $resultDirectory"
Write-Host "Send this file: $reportPath"
