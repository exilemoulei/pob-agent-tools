<#
.SYNOPSIS
    One-click installer and updater for pob4agents (standalone Windows x64 `pob.exe`).

.EXAMPLE
    irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex

.EXAMPLE
    $env:POB_PROXY = "http://127.0.0.1:7890"
    irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]$Version = $env:POB_VERSION,
    [string]$InstallDir = $env:POB_INSTALL_DIR,
    [string]$Proxy = $(if ($env:POB_PROXY) { $env:POB_PROXY } elseif ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } else { $env:HTTP_PROXY }),
    [string]$Mirror = $env:POB_GITHUB_MIRROR,
    [switch]$NoPath,
    # Check the Ed25519 verifier against the RFC 8032 test vectors and exit (used by CI).
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "exilemoulei/pob4agents"
$LegacyInstallDir = $null
if (-not $InstallDir) {
    if ($env:LOCALAPPDATA) {
        $InstallDir = Join-Path $env:LOCALAPPDATA "pob4agents\bin"
        $LegacyInstallDir = Join-Path $env:LOCALAPPDATA "pob-agent-tools\bin"
    } else {
        $InstallDir = Join-Path (Join-Path $HOME ".pob4agents") "bin"
        $LegacyInstallDir = Join-Path (Join-Path $HOME ".pob-agent-tools") "bin"
    }
}

function Apply-Mirror([string]$Url) {
    if (-not $Mirror) { return $Url }
    $prefix = $Mirror.TrimEnd("/")
    return "$prefix/$Url"
}

function Resolve-LatestTag([string]$ProxyUrl) {
    $latestUrl = Apply-Mirror "https://github.com/$Repo/releases/latest"
    $req = [System.Net.HttpWebRequest]::Create($latestUrl)
    $req.Method = "GET"
    $req.AllowAutoRedirect = $false
    $req.UserAgent = "pob-installer/1.0"
    $req.Timeout = 15000
    if ($ProxyUrl) {
        $req.Proxy = New-Object System.Net.WebProxy($ProxyUrl)
    }
    try {
        $resp = [System.Net.HttpWebResponse]$req.GetResponse()
    } catch [System.Net.WebException] {
        $resp = [System.Net.HttpWebResponse]$_.Exception.Response
        if (-not $resp) { throw }
    }
    try {
        $location = $resp.Headers["Location"]
    } finally {
        $resp.Close()
    }
    if ($location -and $location -match "/releases/tag/(v[^/\s\?#]+)") {
        return $Matches[1]
    }
    throw "Could not resolve latest release tag from $latestUrl (Location: $location)."
}

function Download-File([string]$Url, [string]$OutFile, [string]$ProxyUrl) {
    $iwrParams = @{
        Uri     = $Url
        OutFile = $OutFile
        Headers = @{ "User-Agent" = "pob-installer/1.0" }
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $iwrParams["UseBasicParsing"] = $true
    }
    if ($ProxyUrl) {
        $iwrParams["Proxy"] = $ProxyUrl
    }
    Invoke-WebRequest @iwrParams
}

# Release signing keys: base64 raw Ed25519 public keys, the same list as RELEASE_PUBLIC_KEYS in
# pob_cli/src/pob_cli/updater/signature.py (test_release_signature.py keeps them equal).
$ReleasePublicKeys = @("fvwea+k3NZ54EzRFCoMYgvZLy3XTkKGXxo2UPmsNZIM=")
# Releases after v0.8.0 ship SHA256SUMS.txt.sig, and for them a missing or bad signature stops the
# install. Through a mirror every release needs a valid signature (the mirror also picks the tag
# when none is pinned), so a mirror can serve the files but cannot change them; releases from
# before signing install only straight from GitHub.
$FirstSignedVersion = [version]"0.8.1"
# The tag goes into URLs and file names and decides whether a signature is required.
$TagPattern = '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?\z'

# --- Ed25519 signature check (RFC 8032, section 5.1.7): verification only, plain PowerShell -------
# Windows PowerShell has no Ed25519, so this uses .NET BigInteger and SHA-512 directly. Every
# arithmetic expression starts with a BigInteger so PowerShell never converts to double.
$Big = [System.Numerics.BigInteger]
$EdP = $Big::Pow(2, 255) - $Big::new(19)
$EdL = $Big::Pow(2, 252) + $Big::Parse("27742317777372353535851937790883648493")
$Ed2P255 = $Big::Pow(2, 255)

function Ed-Mod($a) {
    $r = [System.Numerics.BigInteger]::Remainder($a, $EdP)
    if ($r.Sign -lt 0) { $r = $r + $EdP }
    return $r
}

function Ed-Inv($a) {
    return [System.Numerics.BigInteger]::ModPow((Ed-Mod $a), $EdP - 2, $EdP)
}

$EdD = Ed-Mod ($Big::new(-121665) * (Ed-Inv $Big::new(121666)))
$EdSqrtM1 = $Big::ModPow(2, $Big::Divide($EdP - 1, 4), $EdP)

function Ed-Add($p, $q) {
    $a = Ed-Mod (($p[1] - $p[0]) * ($q[1] - $q[0]))
    $b = Ed-Mod (($p[1] + $p[0]) * ($q[1] + $q[0]))
    $c = Ed-Mod ($p[3] * $q[3] * 2 * $EdD)
    $d = Ed-Mod ($p[2] * $q[2] * 2)
    $e = $b - $a; $f = $d - $c; $g = $d + $c; $h = $b + $a
    return , @((Ed-Mod ($e * $f)), (Ed-Mod ($g * $h)), (Ed-Mod ($f * $g)), (Ed-Mod ($e * $h)))
}

function Ed-Mul($s, $pt) {
    $q = @([System.Numerics.BigInteger]::Zero, [System.Numerics.BigInteger]::One, [System.Numerics.BigInteger]::One, [System.Numerics.BigInteger]::Zero)
    while ($s.Sign -gt 0) {
        if (-not $s.IsEven) { $q = Ed-Add $q $pt }
        $pt = Ed-Add $pt $pt
        $s = [System.Numerics.BigInteger]::Divide($s, 2)
    }
    return , $q
}

function Ed-Equal($p, $q) {
    return (Ed-Mod ($p[0] * $q[2] - $q[0] * $p[2])).IsZero -and (Ed-Mod ($p[1] * $q[2] - $q[1] * $p[2])).IsZero
}

function Ed-RecoverX($y, [int]$sign) {
    if ($y -ge $EdP) { return $null }
    $x2 = Ed-Mod (($y * $y - 1) * (Ed-Inv ($y * $y * $EdD + 1)))
    if ($x2.IsZero) {
        if ($sign -eq 1) { return $null }
        return [System.Numerics.BigInteger]::Zero
    }
    $x = [System.Numerics.BigInteger]::ModPow($x2, [System.Numerics.BigInteger]::Divide($EdP + 3, 8), $EdP)
    if (-not (Ed-Mod ($x * $x - $x2)).IsZero) { $x = Ed-Mod ($x * $EdSqrtM1) }
    if (-not (Ed-Mod ($x * $x - $x2)).IsZero) { return $null }
    $odd = if ($x.IsEven) { 0 } else { 1 }
    if ($odd -ne $sign) { $x = $EdP - $x }
    return $x
}

function Ed-Int([byte[]]$Bytes) {
    # Little-endian, with a zero byte appended so the value is never read as negative.
    $buf = New-Object byte[] ($Bytes.Length + 1)
    [Array]::Copy($Bytes, $buf, $Bytes.Length)
    return [System.Numerics.BigInteger]::new($buf)
}

function Ed-Decompress([byte[]]$Raw) {
    $y = Ed-Int $Raw
    $sign = 0
    if ($y -ge $Ed2P255) { $sign = 1; $y = $y - $Ed2P255 }
    $x = Ed-RecoverX $y $sign
    if ($null -eq $x) { return $null }
    return , @($x, $y, [System.Numerics.BigInteger]::One, (Ed-Mod ($x * $y)))
}

$EdGy = Ed-Mod ($Big::new(4) * (Ed-Inv $Big::new(5)))
$EdGx = Ed-RecoverX $EdGy 0
$EdG = @($EdGx, $EdGy, $Big::One, (Ed-Mod ($EdGx * $EdGy)))

function Test-Ed25519Signature([byte[]]$PublicKey, [byte[]]$Message, [byte[]]$Signature) {
    if ($null -eq $Message) { $Message = New-Object byte[] 0 }
    if ($null -eq $PublicKey -or $null -eq $Signature) { return $false }
    if ($PublicKey.Length -ne 32 -or $Signature.Length -ne 64) { return $false }
    $rBytes = New-Object byte[] 32
    $sBytes = New-Object byte[] 32
    [Array]::Copy($Signature, 0, $rBytes, 0, 32)
    [Array]::Copy($Signature, 32, $sBytes, 0, 32)
    $a = Ed-Decompress $PublicKey
    $r = Ed-Decompress $rBytes
    if ($null -eq $a -or $null -eq $r) { return $false }
    $s = Ed-Int $sBytes
    if ($s -ge $EdL) { return $false }
    $data = New-Object byte[] (64 + $Message.Length)
    [Array]::Copy($rBytes, 0, $data, 0, 32)
    [Array]::Copy($PublicKey, 0, $data, 32, 32)
    [Array]::Copy($Message, 0, $data, 64, $Message.Length)
    $sha = [System.Security.Cryptography.SHA512]::Create()
    try { $digest = $sha.ComputeHash($data) } finally { $sha.Dispose() }
    $h = [System.Numerics.BigInteger]::Remainder((Ed-Int $digest), $EdL)
    return Ed-Equal (Ed-Mul $s $EdG) (Ed-Add $r (Ed-Mul $h $a))
}

function Test-ReleaseSums([byte[]]$Sums, [string]$SigText) {
    try { $sig = [Convert]::FromBase64String($SigText.Trim()) } catch { return $false }
    foreach ($key in $ReleasePublicKeys) {
        if (Test-Ed25519Signature ([Convert]::FromBase64String($key)) $Sums $sig) { return $true }
    }
    return $false
}

function ConvertFrom-Hex([string]$Hex) {
    $bytes = New-Object byte[] ($Hex.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($Hex.Substring($i * 2, 2), 16) }
    return , $bytes
}

if ($SelfTest) {
    # RFC 8032 section 7.1, TEST 1..3: public key, message, signature.
    $vectors = @(
        @("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
          "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        @("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
          "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        @("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
          "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a")
    )
    foreach ($v in $vectors) {
        $pk = ConvertFrom-Hex $v[0]
        $msg = ConvertFrom-Hex $v[1]
        $sig = ConvertFrom-Hex $v[2]
        if (-not (Test-Ed25519Signature $pk $msg $sig)) { throw "Ed25519 self-test: a valid RFC 8032 signature was rejected" }
        $changed = New-Object byte[] ($msg.Length + 1)
        [Array]::Copy($msg, $changed, $msg.Length)
        if (Test-Ed25519Signature $pk $changed $sig) { throw "Ed25519 self-test: a changed message was accepted" }
        $sig[63] = $sig[63] -bxor 1
        if (Test-Ed25519Signature $pk $msg $sig) { throw "Ed25519 self-test: a changed signature was accepted" }
    }
    Write-Host "Ed25519 self-test passed (RFC 8032 vectors, PowerShell $($PSVersionTable.PSVersion))."
    return
}

if ([Environment]::Is64BitOperatingSystem -eq $false) {
    throw "pob4agents standalone binary requires 64-bit Windows (x64)."
}

if ($Version) {
    $Tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
} else {
    Write-Host "Resolving latest release from github.com/$Repo ..."
    $Tag = Resolve-LatestTag -ProxyUrl $Proxy
}
if ($Tag -cnotmatch $TagPattern) {
    throw "'$Tag' is not a release tag (expected vX.Y.Z); refusing to install."
}

$TagVersion = $Tag.Substring(1)
# Releases from v0.9.0 name the zip pob4agents-<version>-windows-x64.zip; earlier ones (still
# installable with -Version / POB_VERSION) used pob-v<version>-windows-x64.zip.
$AssetCandidates = @("pob4agents-$TagVersion-windows-x64.zip", "pob-$Tag-windows-x64.zip")
$ReleaseBase = "https://github.com/$Repo/releases/download/$Tag"
$SumsUrl = Apply-Mirror "$ReleaseBase/SHA256SUMS.txt"

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pob-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

try {
    $SumsPath = Join-Path $TempDir "SHA256SUMS.txt"
    $StageDir = Join-Path $TempDir "stage"

    Write-Host "Downloading SHA256SUMS.txt ($Tag) ..."
    Download-File -Url $SumsUrl -OutFile $SumsPath -ProxyUrl $Proxy
    $SigPath = Join-Path $TempDir "SHA256SUMS.txt.sig"
    $HaveSig = $true
    try {
        Download-File -Url (Apply-Mirror "$ReleaseBase/SHA256SUMS.txt.sig") -OutFile $SigPath -ProxyUrl $Proxy
    } catch {
        $HaveSig = $false
    }
    $PlainVersion = [version]($TagVersion -replace "-.*$", "")
    $SignedEra = $PlainVersion -ge $FirstSignedVersion
    if ($HaveSig) {
        $sigText = Get-Content -LiteralPath $SigPath -Raw
        if (-not (Test-ReleaseSums ([System.IO.File]::ReadAllBytes($SumsPath)) $sigText)) {
            throw "SHA256SUMS.txt of $Tag is not signed by a pob4agents release key; refusing to install."
        }
        Write-Host "Release signature verified (Ed25519)."
    } elseif ($Mirror -and -not $SignedEra) {
        throw "Could not download SHA256SUMS.txt.sig for $Tag; a release from before signing installs only straight from GitHub, without POB_GITHUB_MIRROR."
    } elseif ($Mirror -or $SignedEra) {
        throw "Could not download SHA256SUMS.txt.sig for $Tag; refusing to install an unsigned release."
    } else {
        Write-Host "$Tag predates signed releases; checking its SHA-256 only."
    }
    $SumsLines = Get-Content -LiteralPath $SumsPath -Encoding UTF8

    $AssetName = $null
    $expectedHash = $null
    foreach ($candidate in $AssetCandidates) {
        foreach ($line in $SumsLines) {
            if ($line -match "^\s*([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($candidate))\s*$") {
                $AssetName = $candidate
                $expectedHash = $Matches[1].ToLowerInvariant()
                break
            }
        }
        if ($AssetName) { break }
    }
    if (-not $AssetName) {
        throw "SHA256SUMS.txt of $Tag lists no Windows zip (looked for: $($AssetCandidates -join ', '))."
    }

    $ZipPath = Join-Path $TempDir $AssetName
    Write-Host "Downloading $AssetName ..."
    Download-File -Url (Apply-Mirror "$ReleaseBase/$AssetName") -OutFile $ZipPath -ProxyUrl $Proxy

    $actualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "SHA256 mismatch for $AssetName! Expected $expectedHash, got $actualHash."
    }
    Write-Host "SHA256 verified: $actualHash"

    Expand-Archive -LiteralPath $ZipPath -DestinationPath $StageDir -Force
    if (-not (Test-Path -LiteralPath (Join-Path $StageDir "pob.exe"))) {
        throw "Invalid archive layout: pob.exe not found in $AssetName."
    }

    # Stop any running daemon before replacing binaries (check both new and legacy install dirs)
    $ExistingExes = @((Join-Path $InstallDir "pob.exe"))
    if ($LegacyInstallDir -and -not $LegacyInstallDir.TrimEnd("\").Equals($InstallDir.TrimEnd("\"), [System.StringComparison]::OrdinalIgnoreCase)) {
        $ExistingExes += (Join-Path $LegacyInstallDir "pob.exe")
    }
    foreach ($exePath in $ExistingExes) {
        if (Test-Path -LiteralPath $exePath) {
            Write-Host "Stopping existing pob daemon ($exePath) before upgrading ..."
            try { & $exePath stop 2>$null | Out-Null } catch {}
            Start-Sleep -Milliseconds 300
        }
    }

    $ParentDir = Split-Path -Parent $InstallDir
    if ($ParentDir -and -not (Test-Path -LiteralPath $ParentDir)) {
        New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
    }

    $BackupDir = "$InstallDir.old"
    if (Test-Path -LiteralPath $BackupDir) {
        Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $InstallDir) {
        Move-Item -LiteralPath $InstallDir -Destination $BackupDir -Force
    }
    try {
        Move-Item -LiteralPath $StageDir -Destination $InstallDir -Force
        if (Test-Path -LiteralPath $BackupDir) {
            Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # A move that failed halfway can leave part of the new build behind; the backup wins.
        if (Test-Path -LiteralPath $BackupDir) {
            if (Test-Path -LiteralPath $InstallDir) {
                Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            Move-Item -LiteralPath $BackupDir -Destination $InstallDir -Force
        }
        throw
    }

    if (-not $NoPath) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $entries = @()
        if ($userPath) {
            $entries = $userPath.Split(";") | Where-Object { $_ -and $_.Trim() -ne "" }
        }
        $normalizedTarget = $InstallDir.TrimEnd("\")
        $normalizedLegacy = if ($LegacyInstallDir) { $LegacyInstallDir.TrimEnd("\") } else { $null }
        $filteredEntries = @()
        $removedLegacy = $false
        $alreadyInPath = $false
        foreach ($e in $entries) {
            $norm = $e.TrimEnd("\")
            if ($normalizedLegacy -and $norm.Equals($normalizedLegacy, [System.StringComparison]::OrdinalIgnoreCase) -and -not $norm.Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
                $removedLegacy = $true
                continue
            }
            if ($norm.Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
                $alreadyInPath = $true
            }
            $filteredEntries += $e
        }
        if (-not $alreadyInPath -or $removedLegacy) {
            $finalEntries = if ($alreadyInPath) { $filteredEntries } else { $filteredEntries + $InstallDir }
            $newUserPath = $finalEntries -join ";"
            [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
            if ($removedLegacy) {
                Write-Host "Replaced legacy $LegacyInstallDir with $InstallDir in your User PATH."
            } else {
                Write-Host "Added $InstallDir to your User PATH."
            }
        }
        $sessionEntries = $env:Path.Split(";") | Where-Object {
            $norm = $_.TrimEnd("\")
            -not ($normalizedLegacy -and $norm.Equals($normalizedLegacy, [System.StringComparison]::OrdinalIgnoreCase) -and -not $norm.Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase))
        }
        if (-not ($sessionEntries | Where-Object { $_.TrimEnd("\").Equals($normalizedTarget, [System.StringComparison]::OrdinalIgnoreCase) })) {
            $env:Path = "$InstallDir;" + ($sessionEntries -join ";")
        } else {
            $env:Path = $sessionEntries -join ";"
        }
    }

    $InstalledExe = Join-Path $InstallDir "pob.exe"
    $VerOut = & $InstalledExe --version
    Write-Host ""
    Write-Host "Installed successfully: $VerOut ($InstalledExe)"
    Write-Host "Next steps:"
    Write-Host "  1. Open a new terminal (or run directly in this shell)."
    Write-Host "  2. Bootstrap the headless PoB engine (pass --proxy if GitHub requires a proxy):"
    Write-Host "       pob bootstrap"
    Write-Host "       pob bootstrap --proxy http://127.0.0.1:7890"
    Write-Host "  3. Verify with: pob status"
} finally {
    if (Test-Path -LiteralPath $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
