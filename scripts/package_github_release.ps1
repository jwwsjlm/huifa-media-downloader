[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$ReleaseNotes
)

$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$VelopackRoot = Join-Path $Root 'releases-velopack'
$OutputRoot = Join-Path $Root 'release-assets'
$StageRoot = Join-Path $Root "build\github-release-$Version"
$InstallerStage = Join-Path $StageRoot 'installer'
$PortableName = "HuifaMediaDownloader-$Version-portable-win-x64.zip"
$InstallerName = "HuifaMediaDownloader-$Version-installer-win-x64.zip"
$PortableArchive = Join-Path $OutputRoot $PortableName
$InstallerArchive = Join-Path $OutputRoot $InstallerName

function Resolve-ProjectPath([string] $Value) {
    $Candidate = if ([System.IO.Path]::IsPathRooted($Value)) {
        $Value
    } else {
        Join-Path $Root $Value
    }
    return [System.IO.Path]::GetFullPath($Candidate)
}

function Assert-ProjectChildPath([string] $Value) {
    $FullPath = (Resolve-ProjectPath $Value).TrimEnd('\')
    if (-not $FullPath.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $FullPath"
    }
    return $FullPath
}

function Assert-ZipEntries(
    [string] $ArchivePath,
    [string[]] $RequiredNames
) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = $null
    try {
        $Archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
        if ($Archive.Entries.Count -lt $RequiredNames.Count) {
            throw "archive contains too few entries"
        }
        foreach ($RequiredName in $RequiredNames) {
            $Matches = @(
                $Archive.Entries |
                    Where-Object {
                        -not [string]::IsNullOrWhiteSpace($_.Name) -and
                        $_.FullName -ieq $RequiredName -and
                        $_.Length -gt 0
                    }
            )
            if ($Matches.Count -ne 1) {
                throw "archive must contain one non-empty $RequiredName"
            }
        }
    }
    catch {
        throw "Invalid release archive $ArchivePath ($($_.Exception.Message))"
    }
    finally {
        if ($null -ne $Archive) {
            $Archive.Dispose()
        }
    }
}

function Assert-ZipEntryPattern(
    [string] $ArchivePath,
    [string] $Pattern,
    [string] $Label,
    [bool] $RequireNonEmpty = $true
) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = $null
    try {
        $Archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
        $Matches = @(
            $Archive.Entries |
                Where-Object {
                    $_.FullName -match $Pattern -and
                    (-not $RequireNonEmpty -or $_.Length -gt 0)
                }
        )
        if ($Matches.Count -ne 1) {
            throw "archive must contain one $Label"
        }
    }
    catch {
        throw "Invalid release archive $ArchivePath ($($_.Exception.Message))"
    }
    finally {
        if ($null -ne $Archive) {
            $Archive.Dispose()
        }
    }
}

$ReleaseNotesFull = Resolve-ProjectPath $ReleaseNotes
if (-not (Test-Path -LiteralPath $ReleaseNotesFull -PathType Leaf)) {
    throw "Release notes file does not exist: $ReleaseNotesFull"
}
if ([string]::IsNullOrWhiteSpace((Get-Content -LiteralPath $ReleaseNotesFull -Raw -Encoding UTF8))) {
    throw "Release notes file is empty: $ReleaseNotesFull"
}
$SetupMatches = @(
    Get-ChildItem -LiteralPath $VelopackRoot -Filter 'Huifa.VideoDownloader*-Setup.exe' -File -ErrorAction SilentlyContinue
)
if ($SetupMatches.Count -ne 1 -or $SetupMatches[0].Length -le 0) {
    throw "Expected exactly one non-empty Velopack Setup.exe; found $($SetupMatches.Count)"
}
$Setup = $SetupMatches[0]
$PortableMatches = @(
    Get-ChildItem -LiteralPath $VelopackRoot -Filter 'Huifa.VideoDownloader*-Portable.zip' -File -ErrorAction SilentlyContinue
)
if ($PortableMatches.Count -ne 1 -or $PortableMatches[0].Length -le 0) {
    throw "Expected exactly one non-empty Velopack Portable.zip; found $($PortableMatches.Count)"
}
$VelopackPortable = $PortableMatches[0]

$ReleaseFeed = Join-Path $VelopackRoot 'releases.win.json'
$AssetsFeed = Join-Path $VelopackRoot 'assets.win.json'
foreach ($RequiredFeed in @($ReleaseFeed, $AssetsFeed)) {
    if (-not (Test-Path -LiteralPath $RequiredFeed -PathType Leaf)) {
        throw "Velopack update feed is missing: $RequiredFeed"
    }
}
$ReleaseIndex = (Get-Content -LiteralPath $ReleaseFeed -Raw -Encoding UTF8) | ConvertFrom-Json
$FullAssets = @(
    $ReleaseIndex.Assets |
        Where-Object {
            $_.PackageId -eq 'Huifa.VideoDownloader' -and
            [string]$_.Version -eq $Version -and
            [string]$_.Type -eq 'Full'
        }
)
if ($FullAssets.Count -ne 1) {
    throw "Velopack feed must contain one full package for $Version; found $($FullAssets.Count)"
}
$FullPackageName = [System.IO.Path]::GetFileName([string]$FullAssets[0].FileName)
$FullPackage = Join-Path $VelopackRoot $FullPackageName
if (-not $FullPackageName -or -not (Test-Path -LiteralPath $FullPackage -PathType Leaf)) {
    throw "Velopack full update package is missing: $FullPackageName"
}

$OutputRootFull = Assert-ProjectChildPath $OutputRoot
$StageRootFull = Assert-ProjectChildPath $StageRoot
foreach ($Target in @($OutputRootFull, $StageRootFull)) {
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $OutputRootFull -Force | Out-Null
New-Item -ItemType Directory -Path $InstallerStage -Force | Out-Null

Copy-Item -LiteralPath $Setup.FullName -Destination (Join-Path $InstallerStage 'HuifaMediaDownloader-Setup.exe')
Copy-Item -LiteralPath $ReleaseNotesFull -Destination (Join-Path $InstallerStage 'RELEASE_NOTES.md')

Copy-Item -LiteralPath $VelopackPortable.FullName -Destination $PortableArchive
Compress-Archive -Path (Join-Path $InstallerStage '*') -DestinationPath $InstallerArchive -CompressionLevel Optimal
Assert-ZipEntries $PortableArchive @(
    'Huifa Media Downloader.exe',
    'Update.exe',
    'current/HuifaVideoDownloader.exe',
    'current/sq.version',
    'current/RELEASE_NOTES.md',
    'current/tools/ffmpeg/x64/ffmpeg.exe',
    'current/tools/ffmpeg/x64/ffprobe.exe',
    'current/tools/yt-dlp/x64/yt-dlp.exe',
    'current/tools/deno/x64/deno.exe',
    'current/tools/chromium/chrome-win64/chrome.exe'
)
Assert-ZipEntryPattern $PortableArchive '^\.portable$' 'Velopack portable marker' $false
Assert-ZipEntryPattern `
    $PortableArchive `
    '^current/tools/yt-dlp-ejs/yt_dlp_ejs-.+-py3-none-any\.whl$' `
    'yt-dlp-ejs wheel'
Assert-ZipEntries $InstallerArchive @('HuifaMediaDownloader-Setup.exe', 'RELEASE_NOTES.md')

Copy-Item -LiteralPath $ReleaseNotesFull -Destination (Join-Path $OutputRoot 'RELEASE_NOTES.md')

# The installed build uses Velopack's GitHub source. Publish its feed and full
# update packages, but keep the raw Setup.exe and Velopack Portable.zip out of
# the user-facing asset list because both distributions are wrapped above.
$VelopackUpdateFiles = @(
    Get-ChildItem -LiteralPath $VelopackRoot -File |
        Where-Object {
            $_.Name -notlike '*-Setup.exe' -and
            $_.Name -notlike '*-Setup.msi' -and
            $_.Name -notlike '*-Portable.zip'
        }
)
foreach ($File in $VelopackUpdateFiles) {
    Copy-Item -LiteralPath $File.FullName -Destination (Join-Path $OutputRoot $File.Name)
}

foreach ($RequiredOutput in @(
    $PortableName,
    $InstallerName,
    'RELEASE_NOTES.md',
    'releases.win.json',
    'assets.win.json',
    $FullPackageName
)) {
    $Path = Join-Path $OutputRoot $RequiredOutput
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or (Get-Item -LiteralPath $Path).Length -le 0) {
        throw "Required GitHub Release asset is missing or empty: $RequiredOutput"
    }
}

$ChecksumLines = @(
    Get-ChildItem -LiteralPath $OutputRoot -File |
        Sort-Object Name |
        ForEach-Object {
            $Digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$Digest  $($_.Name)"
        }
)
$ChecksumLines | Set-Content -LiteralPath (Join-Path $OutputRoot 'SHA256SUMS.txt') -Encoding utf8NoBOM

Remove-Item -LiteralPath $StageRootFull -Recurse -Force
Write-Host "Portable ZIP: $PortableArchive"
Write-Host "Installer ZIP: $InstallerArchive"
Write-Host "GitHub Release assets: $OutputRootFull"
