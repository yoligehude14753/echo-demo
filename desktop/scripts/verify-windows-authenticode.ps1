[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Preflight", "Verify", "VerifyTree", "VerifyZip")]
  [string]$Mode,

  [Parameter(Mandatory = $true)]
  [ValidatePattern("^[0-9A-Fa-f]{40}$")]
  [string]$Thumbprint,

  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$ExpectedPublisher,

  [Parameter(Mandatory = $false)]
  [string]$ArtifactPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Publisher {
  param(
    [Parameter(Mandatory = $true)]
    [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  if (-not [StringComparer]::OrdinalIgnoreCase.Equals($Certificate.Subject, $ExpectedPublisher)) {
    throw "$Label publisher mismatch. Expected '$ExpectedPublisher', got '$($Certificate.Subject)'."
  }
}

function Assert-CertificateChain {
  param(
    [Parameter(Mandatory = $true)]
    [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
  try {
    $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Online
    $chain.ChainPolicy.RevocationFlag = [System.Security.Cryptography.X509Certificates.X509RevocationFlag]::EntireChain
    $chain.ChainPolicy.VerificationFlags = [System.Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
    $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(30)
    if (-not $chain.Build($Certificate)) {
      $status = ($chain.ChainStatus | ForEach-Object {
          "{0}: {1}" -f $_.Status, $_.StatusInformation.Trim()
        }) -join "; "
      throw "$Label certificate chain is invalid: $status"
    }
  }
  finally {
    $chain.Dispose()
  }
}

function Get-ReleaseCertificate {
  $normalized = $Thumbprint.ToUpperInvariant()
  $matches = @(Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object {
      $_.Thumbprint -and $_.Thumbprint.ToUpperInvariant() -eq $normalized
    })
  if ($matches.Count -ne 1) {
    throw "Expected exactly one CurrentUser/My certificate with thumbprint $normalized; found $($matches.Count)."
  }
  return $matches[0]
}

function Assert-ReleaseCertificateReady {
  $certificate = Get-ReleaseCertificate
  Assert-Publisher -Certificate $certificate -Label "Release certificate"
  if (-not $certificate.HasPrivateKey) {
    throw "Release certificate $($certificate.Thumbprint) does not have an accessible private key."
  }
  $codeSigningEku = @($certificate.EnhancedKeyUsageList | Where-Object {
      $_.ObjectId.Value -eq "1.3.6.1.5.5.7.3.3"
    })
  if ($codeSigningEku.Count -eq 0) {
    throw "Release certificate $($certificate.Thumbprint) is not valid for code signing."
  }
  $now = [DateTime]::UtcNow
  if ($now -lt $certificate.NotBefore.ToUniversalTime() -or $now -gt $certificate.NotAfter.ToUniversalTime()) {
    throw "Release certificate $($certificate.Thumbprint) is outside its validity period."
  }
  Assert-CertificateChain -Certificate $certificate -Label "Release certificate"
  Write-Host "Authenticode preflight passed for $($certificate.Subject) [$($certificate.Thumbprint)]."
}

function Test-PeCoffFile {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $stream = $null
  $reader = $null
  try {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    if ($stream.Length -lt 64) { return $false }
    $reader = [IO.BinaryReader]::new($stream)
    $dosHeader = $reader.ReadBytes(2)
    if ($dosHeader.Length -ne 2 -or $dosHeader[0] -ne 0x4D -or $dosHeader[1] -ne 0x5A) { return $false }
    $stream.Position = 0x3C
    $peOffset = $reader.ReadUInt32()
    if ($peOffset -lt 64 -or $peOffset + 4 -gt $stream.Length) { return $false }
    $stream.Position = $peOffset
    $signature = $reader.ReadBytes(4)
    return $signature.Length -eq 4 -and $signature[0] -eq 0x50 -and $signature[1] -eq 0x45 -and $signature[2] -eq 0 -and $signature[3] -eq 0
  }
  catch {
    return $false
  }
  finally {
    if ($null -ne $reader) { $reader.Dispose() }
    elseif ($null -ne $stream) { $stream.Dispose() }
  }
}

function Get-ActualPeFiles {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
  $rootItem = Get-Item -LiteralPath $resolvedRoot -ErrorAction Stop
  if (-not $rootItem.PSIsContainer) {
    throw "PE/COFF scan root is not a directory: $resolvedRoot"
  }
  return @(
    Get-ChildItem -LiteralPath $resolvedRoot -File -Recurse -Force -ErrorAction Stop |
      Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and (Test-PeCoffFile -Path $_.FullName)
      } |
      Sort-Object FullName
  )
}

function Assert-ArtifactSignature {
  if ([string]::IsNullOrWhiteSpace($ArtifactPath)) {
    throw "ArtifactPath is required in Verify mode."
  }
  $resolvedArtifact = (Resolve-Path -LiteralPath $ArtifactPath -ErrorAction Stop).Path
  $signature = Get-AuthenticodeSignature -LiteralPath $resolvedArtifact
  if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Authenticode signature is not valid for '$resolvedArtifact': $($signature.Status) $($signature.StatusMessage)"
  }
  if ($null -eq $signature.SignerCertificate) {
    throw "Authenticode signer certificate is missing for '$resolvedArtifact'."
  }
  if ($signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $Thumbprint.ToUpperInvariant()) {
    throw "Authenticode signer thumbprint mismatch for '$resolvedArtifact'."
  }
  Assert-Publisher -Certificate $signature.SignerCertificate -Label "Artifact signer"
  Assert-CertificateChain -Certificate $signature.SignerCertificate -Label "Artifact signer"

  if ($null -eq $signature.TimeStamperCertificate) {
    throw "RFC 3161 timestamp is missing for '$resolvedArtifact'."
  }
  Assert-CertificateChain -Certificate $signature.TimeStamperCertificate -Label "Timestamp signer"
  Write-Host "Verified Authenticode chain and timestamp for '$resolvedArtifact'."
  $fileHash = Get-FileHash -LiteralPath $resolvedArtifact -Algorithm SHA256
  return [PSCustomObject]@{
    path = $resolvedArtifact
    sha256 = $fileHash.Hash.ToLowerInvariant()
    digest_algorithm = "sha256"
    authenticode_status = $signature.Status.ToString()
    thumbprint = $signature.SignerCertificate.Thumbprint.ToUpperInvariant()
    publisher = $signature.SignerCertificate.Subject
    timestamp_status = "rfc3161_chain_valid"
  }
}

function Assert-TreeSignatures {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
  $files = @(Get-ActualPeFiles -Root $resolvedRoot)
  if ($files.Count -eq 0) {
    throw "No actual PE/COFF files were found recursively below '$resolvedRoot'."
  }
  $records = foreach ($file in $files) {
    $record = Assert-ArtifactSignature -ArtifactPath $file.FullName
    $record | Add-Member -NotePropertyName relative_path -NotePropertyValue ([IO.Path]::GetRelativePath($resolvedRoot, $file.FullName)) -PassThru
  }
  return @($records)
}

function Assert-ZipSignatures {
  if ([string]::IsNullOrWhiteSpace($ArtifactPath)) {
    throw "ArtifactPath is required in VerifyZip mode."
  }
  $resolvedArchive = (Resolve-Path -LiteralPath $ArtifactPath -ErrorAction Stop).Path
  if ([IO.Path]::GetExtension($resolvedArchive).ToLowerInvariant() -ne ".zip") {
    throw "VerifyZip requires a .zip artifact: $resolvedArchive"
  }
  $temporaryRoot = Join-Path $env:TEMP ("echodesk-authenticode-" + [Guid]::NewGuid().ToString("N"))
  try {
    Expand-Archive -LiteralPath $resolvedArchive -DestinationPath $temporaryRoot -Force
    return @(Assert-TreeSignatures -Root $temporaryRoot)
  }
  finally {
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}

if ($Mode -eq "Preflight") {
  Assert-ReleaseCertificateReady
}
elseif ($Mode -eq "Verify") {
  $record = Assert-ArtifactSignature
  $record | ConvertTo-Json -Depth 5 -Compress
}
elseif ($Mode -eq "VerifyTree") {
  $records = @(Assert-TreeSignatures -Root $ArtifactPath)
  $records | ConvertTo-Json -Depth 5 -Compress
}
else {
  $records = @(Assert-ZipSignatures)
  $records | ConvertTo-Json -Depth 5 -Compress
}
