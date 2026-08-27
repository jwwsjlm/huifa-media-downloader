[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')

function Resolve-ProjectChild([string] $Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to access a path outside the project: $full"
    }
    return $full
}

$buildRoot = Resolve-ProjectChild (Join-Path $Root 'build')
$keepNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($name in @(
    '01_velopack_hook.py',
    'HuifaVideoDownloader.velopack.spec'
)) {
    [void]$keepNames.Add($name)
}

foreach ($item in Get-ChildItem -LiteralPath $buildRoot -Force) {
    if ($keepNames.Contains($item.Name)) {
        continue
    }
    $target = Resolve-ProjectChild $item.FullName
    if ($PSCmdlet.ShouldProcess($target, 'Remove generated build artifact')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed generated build artifact $target"
    }
}

foreach ($obsoletePath in @(
    (Join-Path $Root 'release'),
    (Join-Path $Root 'releases'),
    (Join-Path $Root 'releases-velopack'),
    (Join-Path $Root 'releases_debug'),
    (Join-Path $Root 'releases_icon'),
    (Join-Path $Root 'releases_lean'),
    (Join-Path $Root '.tmp'),
    (Join-Path $Root '.idea'),
    (Join-Path $Root 'tools\uv'),
    (Join-Path $Root 'tools\ffmpeg\x86'),
    (Join-Path $Root 'data\sau'),
    (Join-Path $Root 'data\browser\profile'),
    (Join-Path $Root 'data\browser\cache'),
    (Join-Path $Root 'docs\archive\legacy-release-readme.txt')
)) {
    $full = Resolve-ProjectChild $obsoletePath
    if (-not (Test-Path -LiteralPath $full)) {
        continue
    }
    if ($PSCmdlet.ShouldProcess($full, 'Remove obsolete workspace item')) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Host "Removed obsolete workspace item $full"
    }
}

$chromiumRoot = Resolve-ProjectChild (Join-Path $Root 'tools\chromium')
$chromiumVersions = @(
    Get-ChildItem -LiteralPath $chromiumRoot -Directory -Force |
        Where-Object { $_.Name -match '^chromium-(\d+)$' } |
        ForEach-Object {
            [pscustomobject]@{
                Item = $_
                Revision = [int64]$Matches[1]
            }
        } |
        Sort-Object Revision -Descending
)
$currentChromium = $chromiumVersions | Select-Object -First 1
foreach ($item in Get-ChildItem -LiteralPath $chromiumRoot -Force) {
    if ($currentChromium -and $item.FullName -eq $currentChromium.Item.FullName) {
        continue
    }
    $target = Resolve-ProjectChild $item.FullName
    if ($PSCmdlet.ShouldProcess($target, 'Remove obsolete Playwright browser artifact')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed obsolete Playwright browser artifact $target"
    }
}

$bilibiliProfiles = Resolve-ProjectChild (
    Join-Path $Root 'data\browser\sau-cookies\browser_profiles\bilibili'
)
if (Test-Path -LiteralPath $bilibiliProfiles) {
    foreach ($item in Get-ChildItem -LiteralPath $bilibiliProfiles -Directory -Force) {
        if ($item.Name -notlike 'playwright-smoke-*') {
            continue
        }
        $target = Resolve-ProjectChild $item.FullName
        if ($PSCmdlet.ShouldProcess($target, 'Remove Playwright smoke-test profile')) {
            Remove-Item -LiteralPath $target -Recurse -Force
            Write-Host "Removed Playwright smoke-test profile $target"
        }
    }
}

$ffmpegRoot = Resolve-ProjectChild (Join-Path $Root 'tools\ffmpeg')
foreach ($item in Get-ChildItem -LiteralPath $ffmpegRoot -Force) {
    if ($item.Name -in @('README.md', 'x64')) {
        continue
    }
    $target = Resolve-ProjectChild $item.FullName
    if ($PSCmdlet.ShouldProcess($target, 'Remove obsolete FFmpeg runtime file')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed obsolete FFmpeg runtime file $target"
    }
}

$ffmpegX64 = Resolve-ProjectChild (Join-Path $Root 'tools\ffmpeg\x64')
foreach ($item in Get-ChildItem -LiteralPath $ffmpegX64 -Force) {
    if ($item.Name -in @('ffmpeg.exe', 'ffprobe.exe')) {
        continue
    }
    $target = Resolve-ProjectChild $item.FullName
    if ($PSCmdlet.ShouldProcess($target, 'Remove unused FFmpeg companion file')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed unused FFmpeg companion file $target"
    }
}

$runtimeTemp = Resolve-ProjectChild (Join-Path $Root 'data\temp')
if (Test-Path -LiteralPath $runtimeTemp) {
    foreach ($item in Get-ChildItem -LiteralPath $runtimeTemp -Force) {
        $target = Resolve-ProjectChild $item.FullName
        if ($PSCmdlet.ShouldProcess($target, 'Remove obsolete UI/test temporary artifact')) {
            Remove-Item -LiteralPath $target -Recurse -Force
            Write-Host "Removed temporary artifact $target"
        }
    }
}

$backupRoot = Resolve-ProjectChild (Join-Path $Root 'data\backups')
if (Test-Path -LiteralPath $backupRoot) {
    foreach ($item in Get-ChildItem -LiteralPath $backupRoot -Force -File) {
        if ($item.Name -notlike '.app.db.backup-*.tmp*') {
            continue
        }
        $target = Resolve-ProjectChild $item.FullName
        if ($PSCmdlet.ShouldProcess($target, 'Remove orphaned SQLite backup sidecar')) {
            Remove-Item -LiteralPath $target -Force
            Write-Host "Removed orphaned backup sidecar $target"
        }
    }
}

$archiveDirectory = Resolve-ProjectChild (Join-Path $Root 'docs\archive')
if (Test-Path -LiteralPath $archiveDirectory) {
    $remaining = @(Get-ChildItem -LiteralPath $archiveDirectory -Force)
    if ($remaining.Count -eq 0 -and $PSCmdlet.ShouldProcess($archiveDirectory, 'Remove empty directory')) {
        Remove-Item -LiteralPath $archiveDirectory -Force
    }
}
