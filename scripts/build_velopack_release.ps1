[CmdletBinding()]
# Managed Windows release pipeline used by the GitHub tag workflow. This
# command produces Setup.exe, the portable package, feeds, and update packages.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')]
    [string]$Version,

    [ValidatePattern('^win(?:-[A-Za-z0-9][A-Za-z0-9_.-]*)?$')]
    [string]$Channel = 'win',

    [ValidateSet('win-x64')]
    [string]$Runtime = 'win-x64',

    [string]$ReleaseNotes = '',

    [ValidatePattern('^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$')]
    [string]$PreviousReleaseRepo = '',

    [switch]$BuildMsi,
    [switch]$SkipTests,
    [switch]$ValidateEnvironmentOnly
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$PyInstaller = Join-Path $Root '.venv\Scripts\pyinstaller.exe'
$Spec = Join-Path $Root 'build\HuifaVideoDownloader.velopack.spec'
$ToolManifest = Join-Path $Root '.config\dotnet-tools.json'
$ApplicationIcon = Join-Path $Root 'assets\huifa.ico'
$StageRoot = Join-Path $Root 'build\velopack-dist'
$StageApp = Join-Path $StageRoot 'HuifaVideoDownloader'
$WorkRoot = Join-Path $Root 'build\pyinstaller-velopack'
$ReleaseRoot = Join-Path $Root 'releases-velopack'
$ReleaseToolsRoot = Join-Path $Root 'tools'
$ReleaseNotesFull = ''
$ExpectedVelopackVersion = '1.2.0'

function Resolve-DotnetSdkExecutable {
    # A 32-bit dotnet host is commonly placed before the 64-bit SDK host in
    # PATH. Velopack needs an SDK, so inspect candidates instead of trusting
    # the first executable returned by Get-Command.
    $Candidates = [System.Collections.Generic.List[string]]::new()
    foreach ($RootCandidate in @(
        $env:ProgramW6432,
        $env:ProgramFiles,
        $env:DOTNET_ROOT_X64,
        $env:DOTNET_ROOT
    )) {
        if (-not [string]::IsNullOrWhiteSpace($RootCandidate)) {
            $Candidates.Add((Join-Path $RootCandidate 'dotnet\dotnet.exe'))
            $Candidates.Add((Join-Path $RootCandidate 'dotnet.exe'))
        }
    }
    $PathCommand = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($PathCommand -and $PathCommand.Source) {
        $Candidates.Add($PathCommand.Source)
    }

    $Seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $Diagnostics = [System.Collections.Generic.List[string]]::new()
    foreach ($Candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($Candidate)) {
            continue
        }
        try {
            $CandidateFull = [System.IO.Path]::GetFullPath($Candidate)
        }
        catch {
            continue
        }
        if (-not $Seen.Add($CandidateFull) -or -not (Test-Path -LiteralPath $CandidateFull -PathType Leaf)) {
            continue
        }

        try {
            $SdkOutput = @(& $CandidateFull --list-sdks 2>&1)
            $SdkExitCode = $LASTEXITCODE
        }
        catch {
            $Diagnostics.Add("$CandidateFull ($($_.Exception.Message))")
            continue
        }
        $SdkLines = @(
            $SdkOutput |
                ForEach-Object { ([string]$_).Trim() } |
                Where-Object { $_ -match '^\d+\.\d+\.\d+(?:[-+][^\s]+)?\s+\[[^\]]+\]$' }
        )
        if ($SdkExitCode -eq 0 -and $SdkLines.Count -gt 0) {
            return [pscustomobject]@{
                Path = $CandidateFull
                Sdks = $SdkLines
            }
        }
        $Reason = if ($SdkExitCode -ne 0) {
            "exit code $SdkExitCode"
        } else {
            'runtime only; no SDKs reported'
        }
        $Diagnostics.Add("$CandidateFull ($Reason)")
    }

    $Checked = if ($Diagnostics.Count -gt 0) {
        $Diagnostics -join '; '
    } else {
        'no dotnet executable was found'
    }
    throw "A 64-bit .NET SDK is required by Velopack vpk. Checked: $Checked"
}

function Get-SingleNonEmptyReleaseFile {
    param(
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $Matches = @(Get-ChildItem -LiteralPath $ReleaseRoot -Filter $Pattern -File -ErrorAction SilentlyContinue)
    if ($Matches.Count -ne 1) {
        throw "Velopack output is incomplete; expected exactly one $Label matching '$Pattern', found $($Matches.Count)."
    }
    if ($Matches[0].Length -le 0) {
        throw "Velopack output is invalid; $Label is empty: $($Matches[0].FullName)"
    }
    return $Matches[0]
}

function Assert-ValidZipArchive {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$File,
        [string]$RequiredFileName = ''
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = $null
    try {
        $Archive = [System.IO.Compression.ZipFile]::OpenRead($File.FullName)
        if ($Archive.Entries.Count -eq 0) {
            throw 'archive has no entries'
        }
        if ($RequiredFileName) {
            $RequiredEntry = @(
                $Archive.Entries |
                    Where-Object { [System.IO.Path]::GetFileName($_.FullName) -ieq $RequiredFileName }
            )
            if ($RequiredEntry.Count -eq 0) {
                throw "archive does not contain $RequiredFileName"
            }
        }
    }
    catch {
        throw "Velopack output is not a valid archive: $($File.FullName) ($($_.Exception.Message))"
    }
    finally {
        if ($null -ne $Archive) {
            $Archive.Dispose()
        }
    }
}

function Copy-RequiredReleaseFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf) -or (Get-Item -LiteralPath $Source).Length -le 0) {
        throw "Required portable runtime is missing or empty: $Source"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Stage-PortableRuntimeTools {
    $ToolTarget = Join-Path $StageApp 'tools'
    Copy-RequiredReleaseFile `
        -Source (Join-Path $ReleaseToolsRoot 'ffmpeg\x64\ffmpeg.exe') `
        -Destination (Join-Path $ToolTarget 'ffmpeg\x64\ffmpeg.exe')
    Copy-RequiredReleaseFile `
        -Source (Join-Path $ReleaseToolsRoot 'ffmpeg\x64\ffprobe.exe') `
        -Destination (Join-Path $ToolTarget 'ffmpeg\x64\ffprobe.exe')
    Copy-RequiredReleaseFile `
        -Source (Join-Path $ReleaseToolsRoot 'yt-dlp\x64\yt-dlp.exe') `
        -Destination (Join-Path $ToolTarget 'yt-dlp\x64\yt-dlp.exe')
    Copy-RequiredReleaseFile `
        -Source (Join-Path $ReleaseToolsRoot 'deno\x64\deno.exe') `
        -Destination (Join-Path $ToolTarget 'deno\x64\deno.exe')

    $EjsWheels = @(
        Get-ChildItem -LiteralPath (Join-Path $ReleaseToolsRoot 'yt-dlp-ejs') -File -Filter 'yt_dlp_ejs-*.whl' -ErrorAction SilentlyContinue |
            Sort-Object {
                if ($_.Name -match '^yt_dlp_ejs-([0-9]+(?:\.[0-9]+){1,3})-') {
                    [version]$Matches[1]
                }
                else {
                    [version]'0.0'
                }
            }
    )
    if ($EjsWheels.Count -lt 1 -or $EjsWheels[-1].Length -le 0) {
        throw 'No non-empty yt-dlp-ejs wheel was found.'
    }
    $EjsWheel = $EjsWheels[-1]
    Copy-RequiredReleaseFile `
        -Source $EjsWheel.FullName `
        -Destination (Join-Path $ToolTarget ('yt-dlp-ejs\' + $EjsWheel.Name))

    $ChromiumExecutables = @(
        Get-ChildItem -LiteralPath (Join-Path $ReleaseToolsRoot 'chromium') -Recurse -File -Filter 'chrome.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -eq 'chrome-win64' }
    )
    if ($ChromiumExecutables.Count -lt 1) {
        throw 'No complete Playwright Chromium runtime was found.'
    }
    $ChromiumExecutable = @(
        $ChromiumExecutables |
            Sort-Object {
                if ($_.FullName -match 'chromium-(\d+)') { [int]$Matches[1] } else { 0 }
            }
    )[-1]
    $ChromiumSource = $ChromiumExecutable.Directory.FullName
    $ChromiumTarget = Join-Path $ToolTarget 'chromium\chrome-win64'
    New-Item -ItemType Directory -Path (Split-Path -Parent $ChromiumTarget) -Force | Out-Null
    Copy-Item -LiteralPath $ChromiumSource -Destination $ChromiumTarget -Recurse -Force

    if ($ReleaseNotesFull) {
        Copy-RequiredReleaseFile `
            -Source $ReleaseNotesFull `
            -Destination (Join-Path $StageApp 'RELEASE_NOTES.md')
    }
}

foreach ($RequiredFile in @($Python, $PyInstaller, $Spec, $ToolManifest, $ApplicationIcon)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required build file is missing: $RequiredFile"
    }
}

if ($Version.Contains('-') -and $Channel -eq 'win') {
    throw "Prerelease version '$Version' must use an explicit preview channel such as 'win-beta'; refusing to publish it to stable channel 'win'."
}

if ($ReleaseNotes) {
    $ReleaseNotesCandidate = if ([System.IO.Path]::IsPathRooted($ReleaseNotes)) {
        $ReleaseNotes
    } else {
        Join-Path $Root $ReleaseNotes
    }
    $ReleaseNotesFull = [System.IO.Path]::GetFullPath($ReleaseNotesCandidate)
    if (-not (Test-Path -LiteralPath $ReleaseNotesFull -PathType Leaf)) {
        throw "Release notes file does not exist: $ReleaseNotesFull"
    }
}

& $Python -c "import sys; from importlib.metadata import version; import velopack; assert version('velopack') == sys.argv[1]; assert hasattr(velopack, 'GithubSource'); assert hasattr(velopack, 'UpdateManager')" $ExpectedVelopackVersion
if ($LASTEXITCODE -ne 0) {
    throw "Velopack $ExpectedVelopackVersion is not installed. Run: .\.venv\Scripts\python.exe -m pip install -r requirements-velopack.txt"
}
$AppVersion = (& $Python -c "from app.core.version import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or $AppVersion -ne $Version) {
    throw "Package version '$Version' must match app.core.version.APP_VERSION '$AppVersion'"
}
$ExpectedPublisher = (& $Python -c "from app.core.version import APP_PUBLISHER; print(APP_PUBLISHER)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ExpectedPublisher)) {
    throw 'Unable to resolve app.core.version.APP_PUBLISHER'
}

$DotnetResolution = Resolve-DotnetSdkExecutable
$DotnetExe = $DotnetResolution.Path
$InstalledSdks = @($DotnetResolution.Sdks)
Write-Host "Using .NET SDK host: $DotnetExe"
Write-Host "Installed SDKs: $($InstalledSdks -join ', ')"

if ($ValidateEnvironmentOnly) {
    Write-Host "Velopack build environment is ready for version $Version on channel $Channel."
    return
}

if (-not $SkipTests) {
    & $Python -m compileall app tests
    if ($LASTEXITCODE -ne 0) {
        throw "Python compile check failed with exit code $LASTEXITCODE"
    }
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE"
    }
}

# Velopack requires a PyInstaller onedir staging area.
foreach ($Target in @($StageRoot, $WorkRoot, $ReleaseRoot)) {
    $TargetFull = [System.IO.Path]::GetFullPath($Target).TrimEnd('\')
    if (-not $TargetFull.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside the project: $TargetFull"
    }
    if (Test-Path -LiteralPath $TargetFull) {
        Remove-Item -LiteralPath $TargetFull -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null

& $PyInstaller --noconfirm --clean --distpath $StageRoot --workpath $WorkRoot $Spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller onedir build failed with exit code $LASTEXITCODE"
}
$MainExecutable = Join-Path $StageApp 'HuifaVideoDownloader.exe'
if (-not (Test-Path -LiteralPath $MainExecutable -PathType Leaf)) {
    throw "PyInstaller completed without creating $MainExecutable"
}
Stage-PortableRuntimeTools

# Keep the deployment tool pinned to the same version as the Python SDK.
Push-Location $Root
try {
    & $DotnetExe tool restore
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet tool restore failed with exit code $LASTEXITCODE"
    }
    $ToolListRaw = (& $DotnetExe tool list --local --format json) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet tool list failed with exit code $LASTEXITCODE"
    }
    try {
        $ToolList = $ToolListRaw | ConvertFrom-Json
    }
    catch {
        throw "dotnet returned an invalid local-tool manifest: $($_.Exception.Message)"
    }
    $VpkTools = @(
        $ToolList.data |
            Where-Object {
                $_.packageId -eq 'vpk' -and
                [string]$_.version -eq $ExpectedVelopackVersion -and
                @($_.commands) -contains 'vpk'
            }
    )
    if ($VpkTools.Count -ne 1) {
        throw "Restored vpk version does not match the required $ExpectedVelopackVersion."
    }

    # Downloading the previous release is optional and read-only. It lets vpk
    # create a delta package without this script ever uploading to GitHub.
    if ($PreviousReleaseRepo) {
        $DownloadArgs = @(
            'tool', 'run', 'vpk', '--', 'download', 'github',
            '--repoUrl', $PreviousReleaseRepo.TrimEnd('/'),
            '--outputDir', $ReleaseRoot,
            '--channel', $Channel
        )
        & $DotnetExe @DownloadArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Downloading the previous Velopack release failed with exit code $LASTEXITCODE"
        }
    }

    $PackArgs = @(
        'tool', 'run', 'vpk', '--', 'pack',
        '--packId', 'Huifa.VideoDownloader',
        '--packVersion', $Version,
        '--packDir', $StageApp,
        '--mainExe', 'HuifaVideoDownloader.exe',
        '--packTitle', 'Huifa Media Downloader',
        '--packAuthors', 'Huifa',
        '--icon', $ApplicationIcon,
        '--outputDir', $ReleaseRoot,
        '--channel', $Channel,
        '--runtime', $Runtime,
        '--delta', 'BestSpeed'
    )
    if ($ReleaseNotesFull) {
        $PackArgs += @('--releaseNotes', $ReleaseNotesFull)
    }
    if ($BuildMsi) {
        $PackArgs += @('--msi', '--instLocation', 'Either')
    }
    & $DotnetExe @PackArgs
    if ($LASTEXITCODE -ne 0) {
        throw "vpk pack failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Setup = Get-SingleNonEmptyReleaseFile -Pattern 'Huifa.VideoDownloader*-Setup.exe' -Label 'installer'
$Portable = Get-SingleNonEmptyReleaseFile -Pattern 'Huifa.VideoDownloader*-Portable.zip' -Label 'portable package'
$ReleaseFeed = Get-SingleNonEmptyReleaseFile -Pattern "releases.$Channel.json" -Label 'release feed'
$AssetsFeed = Get-SingleNonEmptyReleaseFile -Pattern "assets.$Channel.json" -Label 'assets feed'
if ($BuildMsi) {
    $Msi = Get-SingleNonEmptyReleaseFile -Pattern 'Huifa.VideoDownloader*-Setup.msi' -Label 'MSI installer'
}

try {
    $ReleaseIndex = (Get-Content -LiteralPath $ReleaseFeed.FullName -Raw) | ConvertFrom-Json
    $null = (Get-Content -LiteralPath $AssetsFeed.FullName -Raw) | ConvertFrom-Json
}
catch {
    throw "Velopack generated an invalid JSON feed: $($_.Exception.Message)"
}
$CurrentFullAssets = @(
    $ReleaseIndex.Assets |
        Where-Object {
            $_.PackageId -eq 'Huifa.VideoDownloader' -and
            [string]$_.Version -eq $Version -and
            [string]$_.Type -eq 'Full'
        }
)
if ($CurrentFullAssets.Count -ne 1) {
    throw "Release feed must contain exactly one full package for Huifa.VideoDownloader $Version; found $($CurrentFullAssets.Count)."
}
$FullAsset = $CurrentFullAssets[0]
$FullFileName = [System.IO.Path]::GetFileName([string]$FullAsset.FileName)
if (-not $FullFileName -or $FullFileName -ne [string]$FullAsset.FileName) {
    throw "Release feed contains an unsafe package filename: $($FullAsset.FileName)"
}
$FullPackagePath = Join-Path $ReleaseRoot $FullFileName
if (-not (Test-Path -LiteralPath $FullPackagePath -PathType Leaf)) {
    throw "Release feed references a missing full package: $FullPackagePath"
}
$FullPackage = Get-Item -LiteralPath $FullPackagePath
if ($FullPackage.Length -le 0) {
    throw "Velopack full package is empty: $FullPackagePath"
}
if ([long]($FullAsset.Size) -gt 0 -and [long]($FullAsset.Size) -ne $FullPackage.Length) {
    throw "Release feed size does not match the full package: $FullPackagePath"
}
$ExpectedHash = ([string]$FullAsset.SHA256).Trim().ToLowerInvariant()
if ($ExpectedHash) {
    $ActualHash = (Get-FileHash -LiteralPath $FullPackagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        throw "Release feed SHA-256 does not match the full package: $FullPackagePath"
    }
}

Assert-ValidZipArchive -File $FullPackage
Assert-ValidZipArchive -File $Portable -RequiredFileName 'HuifaVideoDownloader.exe'

# Exercise the exact directory-style portable artifact that users download.
# Running from current/ proves Velopack management is detected and that the
# external tools copied beside the application are preferred over PyInstaller
# extraction fallbacks.
$PortableSmokeRoot = Join-Path $Root ('build\velopack-portable-smoke-' + [guid]::NewGuid().ToString('N'))
$PortableSmokeReport = Join-Path $PortableSmokeRoot 'portable-smoke.json'
try {
    Expand-Archive -LiteralPath $Portable.FullName -DestinationPath $PortableSmokeRoot -Force
    $PortableCurrent = Join-Path $PortableSmokeRoot 'current'
    $PortableExecutable = Join-Path $PortableCurrent 'HuifaVideoDownloader.exe'
    foreach ($RequiredPortablePath in @(
        (Join-Path $PortableSmokeRoot 'Update.exe'),
        (Join-Path $PortableSmokeRoot '.portable'),
        (Join-Path $PortableCurrent 'sq.version'),
        $PortableExecutable,
        (Join-Path $PortableCurrent 'tools\ffmpeg\x64\ffmpeg.exe'),
        (Join-Path $PortableCurrent 'tools\ffmpeg\x64\ffprobe.exe'),
        (Join-Path $PortableCurrent 'tools\yt-dlp\x64\yt-dlp.exe'),
        (Join-Path $PortableCurrent 'tools\deno\x64\deno.exe'),
        (Join-Path $PortableCurrent 'tools\chromium\chrome-win64\chrome.exe')
    )) {
        if (-not (Test-Path -LiteralPath $RequiredPortablePath -PathType Leaf)) {
            throw "Portable package is missing required file: $RequiredPortablePath"
        }
    }
    $PortableEjsWheels = @(
        Get-ChildItem -LiteralPath (Join-Path $PortableCurrent 'tools\yt-dlp-ejs') -File -Filter 'yt_dlp_ejs-*.whl' -ErrorAction SilentlyContinue
    )
    if ($PortableEjsWheels.Count -ne 1 -or $PortableEjsWheels[0].Length -le 0) {
        throw "Portable package must contain exactly one yt-dlp-ejs wheel; found $($PortableEjsWheels.Count)."
    }

    $SmokeStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $SmokeStartInfo.FileName = $PortableExecutable
    $SmokeStartInfo.WorkingDirectory = $PortableCurrent
    $SmokeStartInfo.UseShellExecute = $false
    $SmokeStartInfo.CreateNoWindow = $true
    $SmokeStartInfo.EnvironmentVariables['QT_QPA_PLATFORM'] = 'offscreen'
    $SmokeStartInfo.EnvironmentVariables['HUIFA_PACKAGED_SMOKE_OUTPUT'] = $PortableSmokeReport
    $SmokeProcess = [System.Diagnostics.Process]::Start($SmokeStartInfo)
    if ($null -eq $SmokeProcess -or -not $SmokeProcess.WaitForExit(90000)) {
        if ($null -ne $SmokeProcess) {
            try { $SmokeProcess.Kill($true) } catch { Stop-Process -Id $SmokeProcess.Id -Force -ErrorAction SilentlyContinue }
        }
        throw 'Velopack portable smoke test timed out after 90 seconds.'
    }
    if ($SmokeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $PortableSmokeReport -PathType Leaf)) {
        throw "Velopack portable smoke test failed with exit code $($SmokeProcess.ExitCode)."
    }
    $SmokeResult = Get-Content -LiteralPath $PortableSmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        -not $SmokeResult.ok -or
        $SmokeResult.application_update_mode -ne 'velopack' -or
        $SmokeResult.application_version -ne $Version -or
        $SmokeResult.organization_name -ne $ExpectedPublisher -or
        -not $SmokeResult.yt_dlp.core_ready -or
        [string]::IsNullOrWhiteSpace([string]$SmokeResult.ffmpeg.version) -or
        [string]::IsNullOrWhiteSpace([string]$SmokeResult.ffprobe.version) -or
        [string]::IsNullOrWhiteSpace([string]$SmokeResult.pyside6_version) -or
        [string]::IsNullOrWhiteSpace([string]$SmokeResult.secure_store_backend)
    ) {
        throw 'Velopack portable smoke report did not confirm a managed portable runtime.'
    }
    foreach ($ToolPath in @($SmokeResult.ffmpeg.runtime_path, $SmokeResult.ffprobe.runtime_path)) {
        if (-not ([System.IO.Path]::GetFullPath([string]$ToolPath)).StartsWith(
            [System.IO.Path]::GetFullPath((Join-Path $PortableCurrent 'tools')) + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Portable runtime resolved a tool outside current\tools: $ToolPath"
        }
    }

    # Prove that a real Velopack apply operation replaces the complete current
    # directory (including external tools) while preserving portable data.
    $PersistentMarker = Join-Path $PortableSmokeRoot 'data\portable-update-preserve.txt'
    New-Item -ItemType Directory -Path (Split-Path -Parent $PersistentMarker) -Force | Out-Null
    Set-Content -LiteralPath $PersistentMarker -Value 'preserve' -Encoding ASCII
    $DenoPath = Join-Path $PortableCurrent 'tools\deno\x64\deno.exe'
    $ExpectedDenoHash = (Get-FileHash -LiteralPath $DenoPath -Algorithm SHA256).Hash
    [System.IO.File]::WriteAllBytes($DenoPath, [byte[]](0x4d, 0x5a, 0x00, 0x01))
    $PortablePackages = Join-Path $PortableSmokeRoot 'packages'
    New-Item -ItemType Directory -Path $PortablePackages -Force | Out-Null
    $LocalFullPackage = Join-Path $PortablePackages $FullPackage.Name
    Copy-Item -LiteralPath $FullPackage.FullName -Destination $LocalFullPackage -Force

    $UpdateStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $UpdateStartInfo.FileName = Join-Path $PortableSmokeRoot 'Update.exe'
    $UpdateStartInfo.WorkingDirectory = $PortableSmokeRoot
    $UpdateStartInfo.UseShellExecute = $false
    $UpdateStartInfo.CreateNoWindow = $true
    foreach ($Argument in @(
        '--silent',
        '--rootDir', $PortableSmokeRoot,
        '--packageDir', $PortablePackages,
        'apply',
        '--package', $LocalFullPackage,
        '--norestart'
    )) {
        $UpdateStartInfo.ArgumentList.Add($Argument)
    }
    $UpdateProcess = [System.Diagnostics.Process]::Start($UpdateStartInfo)
    if ($null -eq $UpdateProcess -or -not $UpdateProcess.WaitForExit(120000)) {
        if ($null -ne $UpdateProcess) {
            try { $UpdateProcess.Kill($true) } catch { Stop-Process -Id $UpdateProcess.Id -Force -ErrorAction SilentlyContinue }
        }
        throw 'Velopack portable update apply test timed out after 120 seconds.'
    }
    if ($UpdateProcess.ExitCode -ne 0) {
        throw "Velopack portable update apply test failed with exit code $($UpdateProcess.ExitCode)."
    }
    $ActualDenoHash = (Get-FileHash -LiteralPath $DenoPath -Algorithm SHA256).Hash
    if ($ActualDenoHash -ne $ExpectedDenoHash) {
        throw 'Velopack update did not restore the bundled Deno runtime.'
    }
    if (-not (Test-Path -LiteralPath $PersistentMarker -PathType Leaf)) {
        throw 'Velopack update removed portable user data outside current/.'
    }
}
finally {
    if (Test-Path -LiteralPath $PortableSmokeRoot) {
        Remove-Item -LiteralPath $PortableSmokeRoot -Recurse -Force
    }
}

Write-Host "Velopack release directory: $ReleaseRoot"
Write-Host "Installer: $($Setup.Name) ($($Setup.Length) bytes)"
Write-Host "Portable: $($Portable.Name) ($($Portable.Length) bytes)"
Write-Host "Full package: $($FullPackage.Name) ($($FullPackage.Length) bytes)"
if ($BuildMsi) {
    Write-Host "MSI: $($Msi.Name) ($($Msi.Length) bytes)"
}
Write-Host 'Created installer, self-updating portable package, full update package and release feeds.'
Write-Host 'Nothing was uploaded to GitHub.'
