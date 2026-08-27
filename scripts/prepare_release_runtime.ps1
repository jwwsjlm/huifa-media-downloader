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

if (-not $SkipFFmpeg) {
    $Release = Get-GitHubRelease -Repository 'yt-dlp/FFmpeg-Builds'
    $Assets = @(
        $Release.assets |
            Where-Object { $_.name -eq 'ffmpeg-master-latest-win64-gpl.zip' }
    )
    if ($Assets.Count -ne 1) {
        throw "Expected one yt-dlp FFmpeg win64 GPL asset, found $($Assets.Count)."
    }
    $Asset = $Assets[0]
    if ([long]$Asset.size -le 0) {
        throw 'The yt-dlp FFmpeg release asset is empty.'
    }

    $Work = Assert-ProjectChildPath (Join-Path $RuntimeTemp ([guid]::NewGuid().ToString('N')))
    $Archive = Join-Path $Work 'ffmpeg.zip'
    $Extracted = Join-Path $Work 'extracted'
    $Target = Assert-ProjectChildPath (Join-Path $Root 'tools\ffmpeg\x64')
    New-Item -ItemType Directory -Path $Work, $Extracted, $Target -Force | Out-Null
    try {
        Invoke-WebRequest `
            -Uri ([string]$Asset.browser_download_url) `
            -Headers @{'User-Agent' = 'HuifaMediaDownloader-ReleaseBuild'} `
            -OutFile $Archive

        $ExpectedDigest = ([string]$Asset.digest).Trim()
        if ($ExpectedDigest -match '^sha256:([0-9A-Fa-f]{64})$') {
            $ActualDigest = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
            if ($ActualDigest -ine $Matches[1]) {
                throw 'The downloaded yt-dlp FFmpeg archive failed SHA-256 verification.'
            }
        }

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
