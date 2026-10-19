param([string]$Version = $env:RELEASE_VERSION)

$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') { throw 'An explicit release version is required' }
$taskWorkspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath.TrimEnd('\')
$taskMetadata = cargo metadata --locked --format-version 1 --no-deps | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the desktop build directory' }
if (($taskMetadata.packages | Where-Object name -eq 'huifa-desktop').version -ne $Version) { throw 'Portable version differs from the compiled desktop' }
$taskSourceRevision = git rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $taskSourceRevision -notmatch '^[a-f0-9]{40}$') { throw 'Could not resolve portable build provenance' }
$taskExecutable = Join-Path $taskMetadata.target_directory 'release/huifa-desktop.exe'
$taskTools = Join-Path $taskWorkspace 'runtime-tools'
$taskReceipt = Get-Content -LiteralPath (Join-Path $taskTools 'manifest.json') -Raw | ConvertFrom-Json
if ($taskReceipt.target -notin @('windows-x86_64', 'windows-aarch64')) { throw 'Portable packages require Windows tools' }
$taskArchitecture = if ($taskReceipt.target -eq 'windows-aarch64') { 'arm64' } else { 'x64' }
$taskTemporary = [IO.Path]::GetFullPath((Join-Path $taskWorkspace ('.tmp/portable-' + [guid]::NewGuid())))
if (-not $taskTemporary.StartsWith($taskWorkspace + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Portable staging escapes the workspace' }
$taskContents = Join-Path $taskTemporary 'package'
$taskBundle = Join-Path $taskWorkspace 'target/release/bundle/portable'
$taskArchivePath = Join-Path $taskBundle "HuifaDownloader-$Version-windows-$taskArchitecture-portable.zip"
New-Item -ItemType Directory -Path $taskContents -Force | Out-Null
try {
    Copy-Item -LiteralPath $taskExecutable -Destination (Join-Path $taskContents 'HuifaDownloader.exe')
    Copy-Item -LiteralPath (Join-Path $taskMetadata.target_directory 'release/huifa-mcp-bridge.exe') -Destination (Join-Path $taskContents 'huifa-mcp-bridge.exe')
    New-Item -ItemType Directory -Path (Join-Path $taskContents 'tools') -Force | Out-Null
    if (-not $taskReceipt.files -or @(@('yt-dlp', 'ffmpeg', 'ffprobe') | Where-Object { -not $taskReceipt.tools.$_ }).Count -or (-not $taskReceipt.tools.qjs -and -not $taskReceipt.tools.deno)) { throw 'Portable runtime receipt is incomplete' }
    foreach ($taskFile in $taskReceipt.files.PSObject.Properties) {
        if ($taskFile.Name -notmatch '^(?:(?:yt-dlp|ffmpeg|ffprobe|qjs|deno)\.exe|(?:avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale)-\d+\.dll)$') { throw 'Unexpected runtime payload' }
        $taskSource = Join-Path $taskTools $taskFile.Name
        if ((Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash -ine $taskFile.Value) { throw "Tool checksum mismatch: $($taskFile.Name)" }
        Copy-Item -LiteralPath $taskSource -Destination (Join-Path $taskContents "tools/$($taskFile.Name)")
    }
    Copy-Item -LiteralPath (Join-Path $taskTools 'manifest.json') -Destination (Join-Path $taskContents 'tools/manifest.json')
    [IO.File]::WriteAllText((Join-Path $taskContents 'portable.flag'), '')
    [IO.File]::WriteAllText((Join-Path $taskContents 'README.txt'), @"
Huifa Media Downloader $Version - Windows $taskArchitecture portable
Portable build source: https://github.com/jwwsjlm/huifa-media-downloader/tree/$taskSourceRevision

Extract the whole ZIP and run HuifaDownloader.exe.
Runtime components are included. Keep the tools folder beside the executable.
Requires Microsoft Edge WebView2 Runtime: https://developer.microsoft.com/microsoft-edge/webview2/

Tasks and settings use %LOCALAPPDATA%\HuifaDownloaderRust, shared with the installed edition.
Media uses the HuifaDownloaderRust folder under your Windows Downloads directory.
Close the other edition before switching. Moving the program folder does not move your data.

To update, close the app and extract the latest portable ZIP over the program folder.
Portable editions do not run installer updates. Keep portable.flag beside the executable.
Download ZIP signatures and SHA256SUMS from the same release page:
https://github.com/jwwsjlm/huifa-media-downloader/releases/latest
"@)

    $taskProcess = Start-Process -FilePath (Join-Path $taskContents 'HuifaDownloader.exe') -WorkingDirectory $taskContents -WindowStyle Hidden -PassThru -Environment @{
        HUIFA_DATA_DIR = (Join-Path $taskTemporary 'data')
        HUIFA_DOWNLOAD_DIR = (Join-Path $taskTemporary 'downloads')
        WEBVIEW2_USER_DATA_FOLDER = (Join-Path $taskTemporary 'webview')
    }
    try {
        $taskDeadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            if ($taskProcess.HasExited) { throw "Portable desktop exited during startup: $($taskProcess.ExitCode)" }
            $taskBrowser = Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe' AND ParentProcessId = $($taskProcess.Id)"
            $taskReady = (Test-Path -LiteralPath (Join-Path $taskTemporary 'data/rust-core.sqlite3')) -and [bool]$taskBrowser
            if ($taskReady) { break }
            Start-Sleep -Milliseconds 250
        } while ([DateTime]::UtcNow -lt $taskDeadline)
        if (-not $taskReady) { throw 'Portable desktop did not initialize its backend and WebView2' }
    } finally {
        if (-not $taskProcess.HasExited) {
            $taskProcess.CloseMainWindow() | Out-Null
            if (-not $taskProcess.WaitForExit(10000)) { $taskProcess.Kill($true); $taskProcess.WaitForExit() }
        }
    }

    New-Item -ItemType Directory -Path $taskBundle -Force | Out-Null
    $taskStagedArchive = Join-Path $taskTemporary 'archive.zip'
    [IO.Compression.ZipFile]::CreateFromDirectory($taskContents, $taskStagedArchive)
    $taskArchive = [IO.Compression.ZipFile]::OpenRead($taskStagedArchive)
    try {
        $taskExpected = @('HuifaDownloader.exe', 'huifa-mcp-bridge.exe', 'portable.flag', 'README.txt', 'tools/manifest.json') + @($taskReceipt.files.PSObject.Properties | ForEach-Object { "tools/$($_.Name)" })
        $taskEntries = @($taskArchive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        if ($taskEntries.Count -ne $taskExpected.Count -or @(Compare-Object $taskExpected $taskEntries).Count) { throw 'Portable archive contents differ from the required program and tools' }
    } finally { $taskArchive.Dispose() }
    Move-Item -LiteralPath $taskStagedArchive -Destination $taskArchivePath -Force
    Write-Output "Portable archive verified: $taskArchivePath"
} finally {
    $taskCleanup = (Resolve-Path -LiteralPath $taskTemporary).ProviderPath
    if (-not $taskCleanup.StartsWith($taskWorkspace + '\', [StringComparison]::OrdinalIgnoreCase) -or (Get-Item -LiteralPath $taskCleanup).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe portable staging cleanup target' }
    for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
        try {
            if (Test-Path -LiteralPath $taskCleanup) { Remove-Item -LiteralPath $taskCleanup -Recurse -Force }
            break
        } catch {
            if ($taskAttempt -eq 19) { throw }
            Start-Sleep -Milliseconds 250
        }
    }
}
