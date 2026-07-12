[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Preflight", "Verify")]
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
}

if ($Mode -eq "Preflight") {
  Assert-ReleaseCertificateReady
}
else {
  Assert-ArtifactSignature
}
