$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Spec = Join-Path $Root 'build\HuifaVideoDownloader.lean.spec'
$ReleaseVerifier = Join-Path $Root 'scripts\verify_single_exe_release.py'
$BuildRunId = [Guid]::NewGuid().ToString('N')
# Use a private directory for every build. A tester may still be running an
# older staged EXE, which keeps its adjacent data/app.db open on Windows. A
# fixed staging path would then make an unrelated release build fail or tempt
# the build script to remove runtime data that belongs to that process.
$StagingRoot = Join-Path $Root ("build\single-exe-dist-$BuildRunId")
$WorkRoot = Join-Path $Root ("build\pyinstaller-onefile-$BuildRunId")
$SmokeRoot = Join-Path $Root ("build\single-exe-smoke-$BuildRunId")
$StagedExecutable = Join-Path $StagingRoot 'HuifaVideoDownloader.exe'
$SmokeExecutable = Join-Path $SmokeRoot 'HuifaVideoDownloader.exe'
$SmokeReport = Join-Path $SmokeRoot 'packaged-smoke.json'
$ReleaseRoot = Join-Path $Root 'releases'
$Executable = Join-Path $ReleaseRoot 'HuifaVideoDownloader.exe'
$IncomingExecutable = Join-Path $ReleaseRoot 'HuifaVideoDownloader.exe.new'
$BackupExecutable = Join-Path $ReleaseRoot 'HuifaVideoDownloader.exe.previous'
$LegacyStage = Join-Path $ReleaseRoot 'HuifaVideoDownloader'
$ObsoleteDeliveryFiles = @(
    (Join-Path $ReleaseRoot 'HuifaVideoDownloader-win-x64.zip'),
    (Join-Path $ReleaseRoot 'HuifaVideoDownloader.zip'),
    (Join-Path $ReleaseRoot 'SHA256SUMS.txt')
)

function Assert-ProjectChildPath([string] $Candidate) {
    $RootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $CandidateFull = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    if (-not $CandidateFull.StartsWith($RootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $CandidateFull"
    }
    return $CandidateFull
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python virtual environment was not found: $Python"
}
if (-not (Test-Path -LiteralPath $Spec -PathType Leaf)) {
    throw "PyInstaller spec was not found: $Spec"
}
if (-not (Test-Path -LiteralPath $ReleaseVerifier -PathType Leaf)) {
    throw "Single-EXE release verifier was not found: $ReleaseVerifier"
}

# This recipe deliberately targets Windows x64 because it embeds the x64
# FFmpeg runtime. Fail before analysis rather than producing an EXE whose
# downloader cannot start FFmpeg.
$PythonBits = [string] (& $Python -c "import struct; print(struct.calcsize('P') * 8)")
$PythonBits = $PythonBits.Trim()
if ($LASTEXITCODE -ne 0 -or $PythonBits -ne '64') {
    throw "The single-EXE release must be built with 64-bit Python; detected: $PythonBits-bit"
}

& $Python -c "import PyInstaller, PySide6, yt_dlp"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are incomplete. Install requirements.txt and PyInstaller in .venv first."
}

$ExpectedAppVersion = [string] (& $Python -c "from app.core.version import APP_VERSION; print(APP_VERSION)")
$ExpectedAppVersion = $ExpectedAppVersion.Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ExpectedAppVersion)) {
    throw "Could not read APP_VERSION from app.core.version"
}
$ExpectedProductNameBase64 = [string] (& $Python -c "import base64; from app.core.version import APP_NAME; print(base64.b64encode(APP_NAME.encode('utf-8')).decode('ascii'))")
$ExpectedProductNameBase64 = $ExpectedProductNameBase64.Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ExpectedProductNameBase64)) {
    throw "Could not read APP_NAME from app.core.version"
}
$ExpectedPublisher = [string] (& $Python -c "from app.core.version import APP_PUBLISHER; print(APP_PUBLISHER)")
$ExpectedPublisher = $ExpectedPublisher.Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ExpectedPublisher)) {
    throw "Could not read APP_PUBLISHER from app.core.version"
}
$ExpectedWindowsFileVersion = [string] (& $Python -c "import sys; sys.path.insert(0, 'scripts'); from app.core.version import APP_VERSION; from windows_version_info import normalize_windows_version; print('.'.join(map(str, normalize_windows_version(APP_VERSION)[0])))")
$ExpectedWindowsFileVersion = $ExpectedWindowsFileVersion.Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ExpectedWindowsFileVersion)) {
    throw "Could not normalize APP_VERSION for the Windows file resource"
}

& $Python -m compileall app tests
if ($LASTEXITCODE -ne 0) {
    throw "Python compile check failed with exit code $LASTEXITCODE"
}
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Unit tests failed with exit code $LASTEXITCODE"
}

# Build away from releases/. A user may have run the previous EXE in that
# directory, so releases/data can contain their database and settings. The
# build must never recursively delete that runtime data.
$StagingRootFull = Assert-ProjectChildPath $StagingRoot
$WorkRootFull = Assert-ProjectChildPath $WorkRoot
$SmokeRootFull = Assert-ProjectChildPath $SmokeRoot
foreach ($GeneratedPath in ($StagingRootFull, $WorkRootFull, $SmokeRootFull)) {
    if (Test-Path -LiteralPath $GeneratedPath) {
        Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $StagingRootFull -Force | Out-Null

# PyInstaller onefile embeds Python, the required PySide6/QtWidgets runtime,
# yt-dlp and FFmpeg. They are extracted to a temporary directory at launch, so the
# end user does not install Python or PySide6 separately.
& $Python -m PyInstaller --noconfirm --clean --distpath $StagingRootFull --workpath $WorkRootFull $Spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $StagedExecutable -PathType Leaf)) {
    throw "PyInstaller completed without creating $StagedExecutable"
}

$StagedVersionInfo = (Get-Item -LiteralPath $StagedExecutable).VersionInfo
if ([string] $StagedVersionInfo.ProductVersion -ne $ExpectedAppVersion) {
    throw "Packaged ProductVersion mismatch: expected $ExpectedAppVersion, found $($StagedVersionInfo.ProductVersion)"
}
if ([string] $StagedVersionInfo.FileVersion -ne $ExpectedWindowsFileVersion) {
    throw "Packaged FileVersion mismatch: expected $ExpectedWindowsFileVersion, found $($StagedVersionInfo.FileVersion)"
}
$ActualProductNameBase64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes([string] $StagedVersionInfo.ProductName)
)
# Keep the PowerShell script ASCII-only for this comparison. Windows
# PowerShell 5.1 decodes UTF-8-without-BOM source files through the legacy
# system code page, which can make an identical Chinese product name compare
# unequal. Base64 also keeps APP_NAME in app.core.version as the single source.
if (-not [string]::Equals($ActualProductNameBase64, $ExpectedProductNameBase64, [StringComparison]::Ordinal)) {
    throw "Packaged ProductName is incorrect: $($StagedVersionInfo.ProductName)"
}
if (-not [string]::Equals([string] $StagedVersionInfo.CompanyName, $ExpectedPublisher, [StringComparison]::Ordinal)) {
    throw "Packaged CompanyName is incorrect: $($StagedVersionInfo.CompanyName)"
}
if ([string] $StagedVersionInfo.OriginalFilename -ne 'HuifaVideoDownloader.exe') {
    throw "Packaged OriginalFilename is incorrect: $($StagedVersionInfo.OriginalFilename)"
}

$StagedItems = @(Get-ChildItem -LiteralPath $StagingRootFull -Force)
if ($StagedItems.Count -ne 1 -or $StagedItems[0].Name -ne 'HuifaVideoDownloader.exe' -or $StagedItems[0].PSIsContainer) {
    $Names = ($StagedItems | ForEach-Object { $_.Name }) -join ', '
    throw "Single-EXE staging directory must contain only HuifaVideoDownloader.exe; found: $Names"
}

# Source tests cannot prove that PyInstaller actually retained the GUI and
# download-core imports. Launch a copy of the newly built EXE from an isolated
# directory, let it construct the real MainWindow on Qt's offscreen platform,
# and require a machine-readable report before publishing the artifact. The
# isolated data marker prevents the smoke run from importing any legacy user
# database, and the whole directory is removed after a successful build.
New-Item -ItemType Directory -Path $SmokeRootFull -Force | Out-Null
Copy-Item -LiteralPath $StagedExecutable -Destination $SmokeExecutable
$SmokeData = Join-Path $SmokeRootFull 'data'
New-Item -ItemType Directory -Path $SmokeData -Force | Out-Null
Set-Content -LiteralPath (Join-Path $SmokeData '.legacy_db_migrated') -Value "1" -Encoding ASCII

$SmokeStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
$SmokeStartInfo.FileName = $SmokeExecutable
$SmokeStartInfo.WorkingDirectory = $SmokeRootFull
$SmokeStartInfo.UseShellExecute = $false
$SmokeStartInfo.CreateNoWindow = $true
$SmokeStartInfo.EnvironmentVariables['QT_QPA_PLATFORM'] = 'offscreen'
$SmokeStartInfo.EnvironmentVariables['HUIFA_PACKAGED_SMOKE_OUTPUT'] = $SmokeReport
$SmokeProcess = [System.Diagnostics.Process]::Start($SmokeStartInfo)
if ($null -eq $SmokeProcess) {
    throw "Packaged runtime smoke test could not start the staged executable"
}
if (-not $SmokeProcess.WaitForExit(60000)) {
    try {
        $SmokeProcess.Kill($true)
    }
    catch {
        Stop-Process -Id $SmokeProcess.Id -Force -ErrorAction SilentlyContinue
    }
    throw "Packaged runtime smoke test timed out after 60 seconds"
}
if ($SmokeProcess.ExitCode -ne 0) {
    throw "Packaged runtime smoke test failed with exit code $($SmokeProcess.ExitCode)"
}
if (-not (Test-Path -LiteralPath $SmokeReport -PathType Leaf)) {
    throw "Packaged runtime smoke test did not create its JSON report"
}
$SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $SmokeResult.ok) {
    throw "Packaged runtime smoke report marked the executable unhealthy"
}
if (-not $SmokeResult.frozen -or $SmokeResult.application_update_mode -ne 'single-exe') {
    throw "Packaged runtime did not identify itself as an updatable single EXE"
}
if ($SmokeResult.application_version -ne $ExpectedAppVersion -or $SmokeResult.organization_name -ne 'Huifa') {
    throw "Packaged QApplication identity does not match the release metadata"
}
if (-not $SmokeResult.yt_dlp.core_ready -or [string]::IsNullOrWhiteSpace([string] $SmokeResult.yt_dlp.version)) {
    throw "Packaged runtime did not load the embedded yt-dlp download core"
}
if ([string]::IsNullOrWhiteSpace([string] $SmokeResult.pyside6_version)) {
    throw "Packaged runtime did not report its embedded PySide6 version"
}
if ($SmokeResult.secure_store_backend -ne 'keyring.backends.Windows.WinVaultKeyring') {
    throw "Packaged runtime did not load the Windows Credential Manager backend"
}
if ([string]::IsNullOrWhiteSpace([string] $SmokeResult.ffmpeg.version) -or [string]::IsNullOrWhiteSpace([string] $SmokeResult.ffmpeg.runtime_path)) {
    throw "Packaged runtime did not execute its embedded FFmpeg"
}
if ([string]::IsNullOrWhiteSpace([string] $SmokeResult.ffprobe.version) -or [string]::IsNullOrWhiteSpace([string] $SmokeResult.ffprobe.runtime_path)) {
    throw "Packaged runtime did not execute its embedded FFprobe"
}
$ReportedExecutable = [System.IO.Path]::GetFullPath([string] $SmokeResult.executable)
$ExpectedSmokeExecutable = [System.IO.Path]::GetFullPath($SmokeExecutable)
if (-not $ReportedExecutable.Equals($ExpectedSmokeExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Packaged runtime smoke report came from an unexpected executable: $ReportedExecutable"
}
Write-Host "Packaged runtime smoke test: yt-dlp $($SmokeResult.yt_dlp.version), FFmpeg $($SmokeResult.ffmpeg.version), FFprobe $($SmokeResult.ffprobe.version), PySide6 $($SmokeResult.pyside6_version), update mode $($SmokeResult.application_update_mode)"

$ReleaseRootFull = Assert-ProjectChildPath $ReleaseRoot
New-Item -ItemType Directory -Path $ReleaseRootFull -Force | Out-Null

# Copy first, then replace the previous EXE within the same directory. This
# preserves the old executable if copying fails and leaves releases/data
# untouched. File.Replace also creates a short-lived rollback copy.
foreach ($TemporaryPath in ($IncomingExecutable, $BackupExecutable)) {
    if (Test-Path -LiteralPath $TemporaryPath) {
        # File.Replace consumes the incoming path atomically. Antivirus and
        # filesystem notifications can make the preceding Test-Path briefly
        # observe a file that no longer exists by the time Remove-Item runs.
        # Cleanup is best-effort and must stay idempotent.
        Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
    }
}
try {
    Copy-Item -LiteralPath $StagedExecutable -Destination $IncomingExecutable
    if ((Get-Item -LiteralPath $IncomingExecutable).Length -le 0) {
        throw "The staged executable is empty"
    }
    if (Test-Path -LiteralPath $Executable -PathType Leaf) {
        [System.IO.File]::Replace($IncomingExecutable, $Executable, $BackupExecutable, $true)
        Remove-Item -LiteralPath $BackupExecutable -Force -ErrorAction SilentlyContinue
    }
    else {
        Move-Item -LiteralPath $IncomingExecutable -Destination $Executable
    }
}
finally {
    foreach ($TemporaryPath in ($IncomingExecutable, $BackupExecutable)) {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

# Delete only obsolete delivery files created by older build recipes. Do not
# enumerate and remove the release directory itself: data/ is local user state,
# not an attachment that should be shipped with the EXE. An old onedir folder
# is removed only when it contains no data directory of its own.
if (Test-Path -LiteralPath $LegacyStage -PathType Container) {
    if (Test-Path -LiteralPath (Join-Path $LegacyStage 'data')) {
        Write-Warning "Preserving legacy release folder because it contains runtime data: $LegacyStage"
    }
    else {
        Remove-Item -LiteralPath $LegacyStage -Recurse -Force
    }
}
foreach ($ObsoleteDeliveryFile in $ObsoleteDeliveryFiles) {
    if (Test-Path -LiteralPath $ObsoleteDeliveryFile -PathType Leaf) {
        Remove-Item -LiteralPath $ObsoleteDeliveryFile -Force
    }
}

if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Final executable is missing after replacement: $Executable"
}

# This is the single-file delivery gate. It examines top-level files only, so
# releases/data and other local runtime directories remain untouched. The
# optional Velopack command is intentionally separate and is never called by
# this script; no ZIP archive is generated or copied by the single-EXE build.
& $Python $ReleaseVerifier --release-dir $ReleaseRootFull --expected-exe 'HuifaVideoDownloader.exe'
if ($LASTEXITCODE -ne 0) {
    throw "Single-EXE release layout validation failed with exit code $LASTEXITCODE"
}

$Hash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash

# The staged EXE and PyInstaller work tree are build-only copies. Remove them
# after the final executable has been verified so a successful one-file build
# does not leave hundreds of megabytes of duplicate artifacts behind.
foreach ($GeneratedPath in ($StagingRootFull, $WorkRootFull, $SmokeRootFull)) {
    if (Test-Path -LiteralPath $GeneratedPath) {
        Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
    }
}

Write-Host "Release (the only delivery file): $Executable"
Write-Host "SHA256:                          $Hash"
Write-Host "Local releases/data, if present, was preserved and is not part of the delivery."
Write-Host "Portable tools are detected from the EXE directory first: yt-dlp.exe (diagnostic), ffmpeg.exe, deno.exe and sau.exe."
