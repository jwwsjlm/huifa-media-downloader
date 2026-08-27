[CmdletBinding()]
param(
    [switch]$SkipFFmpeg,
    [switch]$SkipChromium
)

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$RuntimeTemp = Join-Path $Root '.tmp\release-runtime'
$GitHubApiVersion = '2026-03-10'
$GitHubToken = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN')
if ([string]::IsNullOrWhiteSpace($GitHubToken)) {
    $GitHubToken = [Environment]::GetEnvironmentVariable('GH_TOKEN')
}

function Assert-ProjectChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $FullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $FullPath.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a runtime path outside the project: $FullPath"
    }
    return $FullPath
}

function Get-GitHubRelease {
    param([Parameter(Mandatory = $true)][string]$Repository)
    $Headers = @{
        Accept = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = $GitHubApiVersion
        'User-Agent' = 'HuifaMediaDownloader-ReleaseBuild'
    }
    if (-not [string]::IsNullOrWhiteSpace($GitHubToken)) {
        $Headers.Authorization = "Bearer $GitHubToken"
    }
    return Invoke-RestMethod `
        -Uri "https://api.github.com/repos/$Repository/releases/latest" `
        -Headers $Headers
}

function Test-WindowsExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Stream = [System.IO.File]::OpenRead($Path)
    try {
        return $Stream.ReadByte() -eq 0x4d -and $Stream.ReadByte() -eq 0x5a
    }
    finally {
        $Stream.Dispose()
    }
}

function Get-SingleReleaseAsset {
    param(
        [Parameter(Mandatory = $true)][object]$Release,
        [Parameter(Mandatory = $true)][scriptblock]$Filter,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $Assets = @($Release.assets | Where-Object $Filter)
    if ($Assets.Count -ne 1) {
        throw "Expected one $Label asset, found $($Assets.Count)."
    }
    if ([long]$Assets[0].size -le 0) {
        throw "The $Label release asset is empty."
    }
    return $Assets[0]
}

function Save-ReleaseAsset {
    param(
        [Parameter(Mandatory = $true)][object]$Asset,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Invoke-WebRequest `
        -Uri ([string]$Asset.browser_download_url) `
        -Headers @{'User-Agent' = 'HuifaMediaDownloader-ReleaseBuild'} `
        -OutFile $Destination

    $ExpectedDigest = ([string]$Asset.digest).Trim()
    if ($ExpectedDigest -match '^sha256:([0-9A-Fa-f]{64})$') {
        $ActualDigest = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($ActualDigest -ine $Matches[1]) {
            throw "The downloaded asset failed SHA-256 verification: $($Asset.name)"
        }
    }
}

function Install-ReleaseExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$AssetName,
        [Parameter(Mandatory = $true)][string]$Target
    )
    $Release = Get-GitHubRelease -Repository $Repository
    $Asset = Get-SingleReleaseAsset `
        -Release $Release `
        -Filter { $_.name -eq $AssetName } `
        -Label "$Repository $AssetName"
    $TargetFull = Assert-ProjectChildPath $Target
    New-Item -ItemType Directory -Path (Split-Path -Parent $TargetFull) -Force | Out-Null
    $Incoming = "$TargetFull.download"
    try {
        Save-ReleaseAsset -Asset $Asset -Destination $Incoming
        if (-not (Test-WindowsExecutable -Path $Incoming)) {
            throw "$AssetName is not a Windows executable."
        }
        Move-Item -LiteralPath $Incoming -Destination $TargetFull -Force
    }
    finally {
        Remove-Item -LiteralPath $Incoming -Force -ErrorAction SilentlyContinue
    }
}

function Install-ZipExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$AssetName,
        [Parameter(Mandatory = $true)][string]$ExecutableName,
        [Parameter(Mandatory = $true)][string]$Target
    )
    $Release = Get-GitHubRelease -Repository $Repository
    $Asset = Get-SingleReleaseAsset `
        -Release $Release `
        -Filter { $_.name -eq $AssetName } `
        -Label "$Repository $AssetName"
    $Work = Assert-ProjectChildPath (Join-Path $RuntimeTemp ([guid]::NewGuid().ToString('N')))
    $Archive = Join-Path $Work $AssetName
    $Extracted = Join-Path $Work 'extracted'
    $TargetFull = Assert-ProjectChildPath $Target
    New-Item -ItemType Directory -Path $Work, $Extracted, (Split-Path -Parent $TargetFull) -Force | Out-Null
    try {
        Save-ReleaseAsset -Asset $Asset -Destination $Archive
        Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted -Force
        $Matches = @(Get-ChildItem -LiteralPath $Extracted -Recurse -File -Filter $ExecutableName)
        if ($Matches.Count -ne 1 -or $Matches[0].Length -le 0) {
            throw "$AssetName does not contain exactly one valid $ExecutableName."
        }
        if (-not (Test-WindowsExecutable -Path $Matches[0].FullName)) {
            throw "$ExecutableName is not a Windows executable."
        }
        Copy-Item -LiteralPath $Matches[0].FullName -Destination $TargetFull -Force
    }
    finally {
        if (Test-Path -LiteralPath $Work) {
            Remove-Item -LiteralPath $Work -Recurse -Force
        }
    }
}

function Install-EjsWheel {
    $Release = Get-GitHubRelease -Repository 'yt-dlp/ejs'
    $Asset = Get-SingleReleaseAsset `
        -Release $Release `
        -Filter { $_.name -match '^yt_dlp_ejs-.+-py3-none-any\.whl$' } `
        -Label 'yt-dlp/ejs Python wheel'
    $TargetRoot = Assert-ProjectChildPath (Join-Path $Root 'tools\yt-dlp-ejs')
    New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
    $Incoming = Join-Path $TargetRoot ([string]$Asset.name + '.download')
    $Target = Join-Path $TargetRoot ([string]$Asset.name)
    try {
        Save-ReleaseAsset -Asset $Asset -Destination $Incoming
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $Archive = [System.IO.Compression.ZipFile]::OpenRead($Incoming)
        try {
            $HasPackage = @(
                $Archive.Entries |
                    Where-Object { $_.FullName -match '^yt_dlp_ejs/[^/]+$' -and $_.Length -gt 0 }
            ).Count -gt 0
            if (-not $HasPackage) {
                throw 'The yt-dlp-ejs wheel does not contain its runtime package.'
            }
        }
        finally {
            $Archive.Dispose()
        }
        if (Test-Path -LiteralPath $Target -PathType Leaf) {
            $CurrentDigest = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash
            $IncomingDigest = (Get-FileHash -LiteralPath $Incoming -Algorithm SHA256).Hash
            if ($CurrentDigest -eq $IncomingDigest) {
                Remove-Item -LiteralPath $Incoming -Force
                return
            }
        }
        # Do not remove older wheels here. A running packaged app may have the
        # selected wheel open through zipimport. The release staging step picks
        # only the newest version, so keeping an older local build cache is safe.
        Move-Item -LiteralPath $Incoming -Destination $Target -Force
    }
    finally {
        Remove-Item -LiteralPath $Incoming -Force -ErrorAction SilentlyContinue
    }
}

if (-not $SkipFFmpeg) {
    $Release = Get-GitHubRelease -Repository 'yt-dlp/FFmpeg-Builds'
    $Asset = Get-SingleReleaseAsset `
        -Release $Release `
        -Filter { $_.name -eq 'ffmpeg-master-latest-win64-gpl.zip' } `
        -Label 'yt-dlp FFmpeg win64 GPL'

    $Work = Assert-ProjectChildPath (Join-Path $RuntimeTemp ([guid]::NewGuid().ToString('N')))
    $Archive = Join-Path $Work 'ffmpeg.zip'
    $Extracted = Join-Path $Work 'extracted'
    $Target = Assert-ProjectChildPath (Join-Path $Root 'tools\ffmpeg\x64')
    New-Item -ItemType Directory -Path $Work, $Extracted, $Target -Force | Out-Null
    try {
        Save-ReleaseAsset -Asset $Asset -Destination $Archive

        Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted -Force
        foreach ($Name in @('ffmpeg.exe', 'ffprobe.exe')) {
            $ExecutableMatches = @(
                Get-ChildItem -LiteralPath $Extracted -Recurse -File -Filter $Name |
                    Where-Object { $_.Directory.Name -eq 'bin' }
            )
            if ($ExecutableMatches.Count -ne 1 -or $ExecutableMatches[0].Length -le 0) {
                throw "The yt-dlp FFmpeg archive does not contain exactly one valid $Name."
            }
            if (-not (Test-WindowsExecutable -Path $ExecutableMatches[0].FullName)) {
                throw "$Name is not a Windows executable."
            }
            Copy-Item -LiteralPath $ExecutableMatches[0].FullName -Destination (Join-Path $Target $Name) -Force
        }
    }
    finally {
        if (Test-Path -LiteralPath $Work) {
            Remove-Item -LiteralPath $Work -Recurse -Force
        }
    }
}

Install-ReleaseExecutable `
    -Repository 'yt-dlp/yt-dlp' `
    -AssetName 'yt-dlp.exe' `
    -Target (Join-Path $Root 'tools\yt-dlp\x64\yt-dlp.exe')
Install-ZipExecutable `
    -Repository 'denoland/deno' `
    -AssetName 'deno-x86_64-pc-windows-msvc.zip' `
    -ExecutableName 'deno.exe' `
    -Target (Join-Path $Root 'tools\deno\x64\deno.exe')
Install-EjsWheel

if (-not $SkipChromium) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Release virtual environment is missing: $Python"
    }
    $ChromiumRoot = Assert-ProjectChildPath (Join-Path $Root 'tools\chromium')
    New-Item -ItemType Directory -Path $ChromiumRoot -Force | Out-Null
    $env:PLAYWRIGHT_BROWSERS_PATH = $ChromiumRoot
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright Chromium installation failed with exit code $LASTEXITCODE"
    }
    $Chrome = @(
        Get-ChildItem -LiteralPath $ChromiumRoot -Recurse -File -Filter 'chrome.exe' |
            Where-Object { $_.Directory.Name -eq 'chrome-win64' }
    )
    if ($Chrome.Count -lt 1) {
        throw 'Playwright completed without installing a usable Chromium executable.'
    }
}

Write-Host 'Release runtimes are ready.'
