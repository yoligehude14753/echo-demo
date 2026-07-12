[CmdletBinding()]
param(
  [string]$InstallerPath = "",
  [string]$ApplicationDirectory = "",
  [int]$Port = 18769,
  [string]$ExpectedVersion = "",
  [string]$EvidenceDirectory = "",
  [string]$ExpectedAuthenticodeThumbprint = "",
  [string]$ExpectedAuthenticodePublisher = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $IsWindows) {
  throw "windows-installed-smoke.ps1 must run on Windows"
}
if ($Port -lt 1024 -or $Port -gt 65535) {
  throw "Port must be between 1024 and 65535 (received $Port)"
}

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packageJsonPath = Join-Path $desktopRoot "package.json"
$packageJson = Get-Content $packageJsonPath -Raw | ConvertFrom-Json
if (-not $ExpectedVersion) {
  $ExpectedVersion = [string]$packageJson.version
}
if (-not $ExpectedVersion) {
  throw "$packageJsonPath does not contain a version"
}

$authenticodeThumbprintConfigured = -not [string]::IsNullOrWhiteSpace(
  $ExpectedAuthenticodeThumbprint
)
$authenticodePublisherConfigured = -not [string]::IsNullOrWhiteSpace(
  $ExpectedAuthenticodePublisher
)
if ($authenticodeThumbprintConfigured -ne $authenticodePublisherConfigured) {
  throw "ExpectedAuthenticodeThumbprint and ExpectedAuthenticodePublisher must be provided together"
}
$authenticodeVerificationEnabled = $authenticodeThumbprintConfigured
$normalizedAuthenticodeThumbprint = ""
if ($authenticodeVerificationEnabled) {
  $normalizedAuthenticodeThumbprint = (
    $ExpectedAuthenticodeThumbprint -replace '[\s:]', ''
  ).ToUpperInvariant()
  if ($normalizedAuthenticodeThumbprint -notmatch '^[0-9A-F]{40}$') {
    throw "ExpectedAuthenticodeThumbprint must contain exactly 40 hexadecimal characters"
  }
}

$portableMode = -not [string]::IsNullOrWhiteSpace($ApplicationDirectory)
if ($portableMode) {
  $ApplicationDirectory = (Resolve-Path $ApplicationDirectory).Path
} else {
  if (-not $InstallerPath) {
    $InstallerPath = Join-Path $desktopRoot "release/EchoDesk.Setup.$ExpectedVersion.exe"
  }
  $InstallerPath = (Resolve-Path $InstallerPath).Path
  $expectedInstallerName = "EchoDesk.Setup.$ExpectedVersion.exe"
  if ((Split-Path $InstallerPath -Leaf) -cne $expectedInstallerName) {
    throw "installer name must be $expectedInstallerName (received $(Split-Path $InstallerPath -Leaf))"
  }
}

$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
if (-not $tempRoot) {
  throw "RUNNER_TEMP and TEMP are both unavailable"
}
if (-not $env:LOCALAPPDATA -or -not $env:APPDATA) {
  throw "LOCALAPPDATA and APPDATA are required for the installed-product smoke"
}

$runToken = "$PID-$([guid]::NewGuid().ToString('N').Substring(0, 10))"
$smokeRoot = [System.IO.Path]::GetFullPath(
  (Join-Path $tempRoot "echodesk-installed-smoke-$runToken")
)
if (-not $EvidenceDirectory) {
  $EvidenceDirectory = Join-Path $tempRoot "echodesk-windows-smoke-evidence/$runToken"
}
$evidenceRoot = [System.IO.Path]::GetFullPath($EvidenceDirectory)
$separator = [System.IO.Path]::DirectorySeparatorChar
$smokePrefix = $smokeRoot.TrimEnd($separator) + $separator
if (
  $evidenceRoot.Equals($smokeRoot, [StringComparison]::OrdinalIgnoreCase) -or
  $evidenceRoot.StartsWith($smokePrefix, [StringComparison]::OrdinalIgnoreCase)
) {
  throw "EvidenceDirectory must be outside the disposable smoke data directory"
}
$backendUserData = Join-Path $smokeRoot "user-data/backend"
$electronUserData = Join-Path $smokeRoot "user-data/electron"
$artifactRuntimeRoot = Join-Path $smokeRoot "artifact-runtime"
$dbPath = Join-Path $backendUserData "echodesk.db"
$storageDir = Join-Path $backendUserData "storage"
$ragIndexDir = Join-Path $backendUserData "rag"
$skillBuildDir = Join-Path $backendUserData "skill-build"
$workspaceStateFile = Join-Path $backendUserData "workspace-state.json"

$installerDirectoryName = [string]$packageJson.name
$productName = [string]$packageJson.build.productName
if (-not $installerDirectoryName -or -not $productName) {
  throw "desktop/package.json must define name and build.productName"
}
$installDir = if ($portableMode) {
  $ApplicationDirectory
} else {
  Join-Path $env:LOCALAPPDATA "Programs/$installerDirectoryName"
}
$installedApp = Join-Path $installDir "$productName.exe"
$installedBackend = Join-Path $installDir "resources/backend/echodesk-backend.exe"
$uninstaller = Join-Path $installDir "Uninstall $productName.exe"

$logPaths = [ordered]@{
  InstallerStdout = Join-Path $evidenceRoot "installer.stdout.log"
  InstallerStderr = Join-Path $evidenceRoot "installer.stderr.log"
  ArtifactStdout  = Join-Path $evidenceRoot "artifact-runtime.stdout.log"
  ArtifactStderr  = Join-Path $evidenceRoot "artifact-runtime.stderr.log"
  FirstStdout     = Join-Path $evidenceRoot "electron-first.stdout.log"
  FirstStderr     = Join-Path $evidenceRoot "electron-first.stderr.log"
  SecondStdout    = Join-Path $evidenceRoot "electron-second.stdout.log"
  SecondStderr    = Join-Path $evidenceRoot "electron-second.stderr.log"
  UninstallStdout = Join-Path $evidenceRoot "uninstaller.stdout.log"
  UninstallStderr = Join-Path $evidenceRoot "uninstaller.stderr.log"
}

# electron-builder's default uninstaller can delete all three names only when
# --delete-app-data (or deleteAppDataOnUninstall) is enabled. Probe the two
# distinct names generated by this package without touching pre-existing data.
$contractProbeRoots = if ($portableMode) {
  @()
} else {
  @(
    (Join-Path $env:APPDATA $productName),
    (Join-Path $env:APPDATA $installerDirectoryName)
  ) | Select-Object -Unique
}
$contractProbeSentinels = [ordered]@{}
$createdContractProbeRoots = [System.Collections.Generic.List[string]]::new()

$environmentOverrides = [ordered]@{
  ECHO_BACKEND_PORT                    = "$Port"
  ECHO_BACKEND_BIND_HOST               = "127.0.0.1"
  ECHO_FORCE_LOCAL_BACKEND             = "1"
  ECHO_PUBLIC_DEMO                     = "0"
  ECHO_SPAWN_BACKEND                   = "1"
  ECHO_ALLOW_PACKAGED_SOURCE_BACKEND   = "0"
  ECHO_USER_DIR                        = $backendUserData
  DB_PATH                              = $dbPath
  STORAGE_DIR                          = $storageDir
  RAG_INDEX_DIR                        = $ragIndexDir
  SKILL_EXECUTOR_BUILD_DIR             = $skillBuildDir
  WORKSPACE_STATE_FILE                 = $workspaceStateFile
  WORKSPACE_SCAN_ON_STARTUP            = "false"
  ECHO_WORKSPACE_DIRS                  = ""
  DIARIZER_ENABLED                     = "false"
  TTS_ENABLED                          = "false"
  PORT                                 = "$Port"
  ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD = "1"
  ECHODESK_AUTO_UPDATE_CHECK_DELAY_MS  = "3600000"
  ECHODESK_NODE_RUNTIME                = $installedApp
  ECHODESK_NODE_RUNTIME_IS_ELECTRON    = "1"
  ECHO_PYTHON                          = $null
  ECHO_BACKEND_CWD                     = $null
  ELECTRON_DEV                         = $null
  VITE_DEV_URL                         = $null
  PYTHONPATH                           = $null
  VIRTUAL_ENV                          = $null
}
$originalEnvironment = [ordered]@{}
foreach ($name in $environmentOverrides.Keys) {
  $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Set-SmokeEnvironment {
  foreach ($name in $environmentOverrides.Keys) {
    [Environment]::SetEnvironmentVariable(
      $name,
      $environmentOverrides[$name],
      [EnvironmentVariableTarget]::Process
    )
  }
}

function Restore-SmokeEnvironment {
  foreach ($name in $originalEnvironment.Keys) {
    [Environment]::SetEnvironmentVariable(
      $name,
      $originalEnvironment[$name],
      [EnvironmentVariableTarget]::Process
    )
  }
}

function Quote-NativeArgument {
  param([Parameter(Mandatory = $true)][string]$Value)

  if ($Value.Contains('"')) {
    throw "native argument contains an unsupported quote: $Value"
  }
  if ($Value -match '\s') {
    return '"' + $Value + '"'
  }
  return $Value
}

function Show-LogTail {
  param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$Path
  )

  if (-not (Test-Path $Path -PathType Leaf)) {
    return
  }
  Write-Host "[windows-smoke] $Label (last 200 lines): $Path"
  Get-Content $Path -Tail 200 -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host $_
  }
}

function Invoke-CheckedNativeProcess {
  param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string]$Arguments = "",
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath
  )

  Write-Host "[windows-smoke] $Label"
  $startParameters = @{
    FilePath               = $FilePath
    Wait                   = $true
    PassThru               = $true
    RedirectStandardOutput = $StdoutPath
    RedirectStandardError  = $StderrPath
  }
  if ($Arguments) {
    $startParameters.ArgumentList = $Arguments
  }
  $process = Start-Process @startParameters
  if ($process.ExitCode -ne 0) {
    Show-LogTail -Label "$Label stdout" -Path $StdoutPath
    Show-LogTail -Label "$Label stderr" -Path $StderrPath
    throw "$Label exited with code $($process.ExitCode)"
  }
}

function Get-SmokePortListeners {
  return @(
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
      Select-Object LocalAddress, LocalPort, OwningProcess, State
  )
}

function Assert-SmokePortFree {
  $listeners = @(Get-SmokePortListeners)
  if ($listeners.Count -gt 0) {
    $owners = ($listeners | ForEach-Object { $_.OwningProcess }) -join ","
    throw "smoke port $Port is already in use (owner pid(s): $owners)"
  }
}

function Wait-SmokePortClosed {
  param(
    [int]$TimeoutSeconds = 30,
    [string]$Context = "application shutdown"
  )

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    $listeners = @(Get-SmokePortListeners)
    if ($listeners.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 500
  } while ([DateTime]::UtcNow -lt $deadline)

  $owners = (@(Get-SmokePortListeners) | ForEach-Object { $_.OwningProcess }) -join ","
  throw "$Context did not release port $Port within ${TimeoutSeconds}s (owner pid(s): $owners)"
}

function Invoke-SmokeApi {
  param(
    [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
    [Parameter(Mandatory = $true)][string]$Path,
    [int]$TimeoutSeconds = 10
  )

  $request = @{
    Method     = $Method
    Uri        = "http://127.0.0.1:$Port$Path"
    TimeoutSec = $TimeoutSeconds
    NoProxy    = $true
  }
  return Invoke-RestMethod @request
}

function Start-InstalledApplication {
  param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath
  )

  $userDataArgument = "--user-data-dir=$(Quote-NativeArgument $electronUserData)"
  Write-Host "[windows-smoke] launching installed application ($Label): $installedApp"
  $startParameters = @{
    PassThru               = $true
    FilePath               = $installedApp
    ArgumentList           = $userDataArgument
    RedirectStandardOutput = $StdoutPath
    RedirectStandardError  = $StderrPath
  }
  return Start-Process @startParameters
}

function Wait-BackendReady {
  param(
    [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
    [Parameter(Mandatory = $true)][string]$Label,
    [int]$TimeoutSeconds = 90
  )

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $lastError = "health endpoint was not reachable"
  do {
    $Process.Refresh()
    if ($Process.HasExited) {
      throw "installed EchoDesk exited during $Label startup (code=$($Process.ExitCode))"
    }
    try {
      $health = Invoke-SmokeApi -Method GET -Path "/healthz" -TimeoutSeconds 2
      if ($health.status -eq "ok") {
        return $health
      }
      $lastError = "health status was '$($health.status)'"
    } catch {
      $lastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 1000
  } while ([DateTime]::UtcNow -lt $deadline)

  throw "installed EchoDesk did not start its bundled backend during $Label within ${TimeoutSeconds}s: $lastError"
}

function Assert-BackendContract {
  param(
    [Parameter(Mandatory = $true)]$Health,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if ([string]$Health.version -ne $ExpectedVersion) {
    throw "$Label backend health version $($Health.version) does not match $ExpectedVersion"
  }
  $bootstrap = Invoke-SmokeApi -Method GET -Path "/bootstrap" -TimeoutSeconds 10
  if ([string]$bootstrap.backend_version -ne $ExpectedVersion) {
    throw "$Label bootstrap backend version $($bootstrap.backend_version) does not match $ExpectedVersion"
  }
  if ([string]$bootstrap.app_version -ne $ExpectedVersion) {
    throw "$Label bootstrap app version $($bootstrap.app_version) does not match $ExpectedVersion"
  }
  if ([string]$bootstrap.api_version -ne "0.3") {
    throw "$Label bootstrap API version $($bootstrap.api_version) does not match 0.3"
  }
  if ([string]$bootstrap.capabilities.workflow_kernel -ne "dispatcher-v1") {
    throw "$Label bootstrap does not expose workflow kernel dispatcher-v1"
  }
}

function Stop-InstalledApplicationGracefully {
  param(
    [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
    [Parameter(Mandatory = $true)][string]$Label
  )

  $Process.Refresh()
  if ($Process.HasExited) {
    throw "installed EchoDesk exited before the $Label shutdown request (code=$($Process.ExitCode))"
  }
  if (-not $Process.CloseMainWindow()) {
    throw "could not send a graceful window-close request during $Label shutdown"
  }
  if (-not $Process.WaitForExit(20000)) {
    throw "installed EchoDesk did not exit gracefully during $Label shutdown within 20s"
  }
  $Process.WaitForExit()
  Wait-SmokePortClosed -TimeoutSeconds 30 -Context "$Label shutdown"
}

function Stop-ProcessTreeBestEffort {
  param([Parameter(Mandatory = $true)][int]$ProcessId)

  try {
    $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
    if ($null -ne $taskkill) {
      $taskkillParameters = @{
        FilePath     = $taskkill.Source
        ArgumentList = "/PID $ProcessId /T /F"
        Wait         = $true
        PassThru     = $true
        WindowStyle  = "Hidden"
        ErrorAction  = "SilentlyContinue"
      }
      $null = Start-Process @taskkillParameters
      return
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
  } catch {
    Write-Warning "failed to terminate process tree ${ProcessId}: $($_.Exception.Message)"
  }
}

function Wait-InstallDirectoryRemoved {
  param([int]$TimeoutSeconds = 60)

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  while ((Test-Path $installDir) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 500
  }
  if (Test-Path $installDir) {
    throw "silent uninstall did not remove program directory within ${TimeoutSeconds}s: $installDir"
  }
}

function Invoke-SilentUninstall {
  param(
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath
  )

  if (-not (Test-Path $uninstaller -PathType Leaf)) {
    throw "installed uninstaller is missing: $uninstaller"
  }
  $uninstallParameters = @{
    Label      = "silent uninstall"
    FilePath   = $uninstaller
    Arguments  = "/S"
    StdoutPath = $StdoutPath
    StderrPath = $StderrPath
  }
  Invoke-CheckedNativeProcess @uninstallParameters
  Wait-InstallDirectoryRemoved
}

function Write-FailureDiagnostics {
  param([Parameter(Mandatory = $true)]$Failure)

  try {
    $scriptStack = ""
    if ($null -ne $Failure.PSObject.Properties["ScriptStackTrace"]) {
      $scriptStack = [string]$Failure.ScriptStackTrace
    }
    $payload = [ordered]@{
      ok             = $false
      timestamp_utc  = [DateTime]::UtcNow.ToString("o")
      error          = [string]$Failure
      script_stack   = $scriptStack
      install_dir    = $installDir
      smoke_root     = $smokeRoot
      evidence_root  = $evidenceRoot
      port           = $Port
      port_listeners = @(Get-SmokePortListeners)
    }
    $payload | ConvertTo-Json -Depth 8 |
      Set-Content (Join-Path $evidenceRoot "failure.json") -Encoding utf8
  } catch {
    Write-Warning "failed to write failure.json: $($_.Exception.Message)"
  }

  Write-Host "[windows-smoke] ERROR: $Failure" -ForegroundColor Red
  foreach ($entry in $logPaths.GetEnumerator()) {
    Show-LogTail -Label $entry.Key -Path $entry.Value
  }
  try {
    $processes = Get-CimInstance Win32_Process `
      -Filter "Name='EchoDesk.exe' OR Name='echodesk-backend.exe'" |
      Select-Object Name, ProcessId, ParentProcessId, CommandLine
    if ($null -ne $processes) {
      Write-Host "[windows-smoke] remaining EchoDesk processes"
      Write-Host ($processes | Format-Table -AutoSize | Out-String)
    }
  } catch {
    Write-Warning "failed to collect process diagnostics: $($_.Exception.Message)"
  }
}

$appProcess = $null
$installationOwned = $false
$failure = $null
$result = $null
$firstStartedAt = ""
$meetingId = "windows-smoke-$runToken"

try {
  New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
  New-Item -ItemType Directory -Force -Path $evidenceRoot | Out-Null
  New-Item -ItemType Directory -Force -Path $backendUserData | Out-Null
  New-Item -ItemType Directory -Force -Path $electronUserData | Out-Null
  New-Item -ItemType Directory -Force -Path $artifactRuntimeRoot | Out-Null

  Assert-SmokePortFree
  if (-not $portableMode) {
    if (Test-Path $installDir) {
      throw "refusing to overwrite an existing EchoDesk installation: $installDir"
    }
    foreach ($root in $contractProbeRoots) {
      if (Test-Path $root) {
        throw "refusing to touch existing product user data required by uninstall contract probe: $root"
      }
    }

    # The preflight above proved the install directory did not exist. From this
    # point onward a partial or complete install is owned by this smoke and may
    # be removed in finally; pre-existing installations are never uninstalled.
    $installationOwned = $true
    $installParameters = @{
      Label      = "silent install $InstallerPath"
      FilePath   = $InstallerPath
      Arguments  = "/S"
      StdoutPath = $logPaths.InstallerStdout
      StderrPath = $logPaths.InstallerStderr
    }
    Invoke-CheckedNativeProcess @installParameters
  }

  if (-not (Test-Path $installedApp -PathType Leaf)) {
    throw "packaged application is missing: $installedApp"
  }
  if (-not (Test-Path $installedBackend -PathType Leaf)) {
    throw "packaged backend is missing: $installedBackend"
  }
  if (-not $portableMode -and -not (Test-Path $uninstaller -PathType Leaf)) {
    throw "installed uninstaller is missing: $uninstaller"
  }

  if ($authenticodeVerificationEnabled) {
    $authenticodeVerifier = Join-Path $desktopRoot "scripts/verify-windows-authenticode.ps1"
    foreach ($artifact in @($installedApp, $installedBackend)) {
      & $authenticodeVerifier `
        -Mode Verify `
        -Thumbprint $normalizedAuthenticodeThumbprint `
        -ExpectedPublisher $ExpectedAuthenticodePublisher `
        -ArtifactPath $artifact
    }
  }

  $productVersion = [string](Get-Item $installedApp).VersionInfo.ProductVersion
  if (-not $productVersion.StartsWith($ExpectedVersion, [StringComparison]::OrdinalIgnoreCase)) {
    throw "installed executable version $productVersion does not match $ExpectedVersion"
  }

  Set-SmokeEnvironment

  $artifactArguments = "--artifact-runtime-smoke $(Quote-NativeArgument $artifactRuntimeRoot)"
  $artifactParameters = @{
    Label      = "packaged DOCX/XLSX/PDF/PPTX runtime smoke"
    FilePath   = $installedBackend
    Arguments  = $artifactArguments
    StdoutPath = $logPaths.ArtifactStdout
    StderrPath = $logPaths.ArtifactStderr
  }
  Invoke-CheckedNativeProcess @artifactParameters

  $artifactManifestPath = Join-Path $artifactRuntimeRoot "artifact-runtime-smoke.json"
  if (-not (Test-Path $artifactManifestPath -PathType Leaf)) {
    throw "packaged artifact runtime manifest is missing: $artifactManifestPath"
  }
  $artifactManifest = Get-Content $artifactManifestPath -Raw | ConvertFrom-Json
  if ($artifactManifest.ok -ne $true) {
    throw "packaged artifact runtime did not report success"
  }
  $artifactExtensions = [ordered]@{
    docx = ".docx"
    xlsx = ".xlsx"
    pdf  = ".pdf"
    pptx = ".pptx"
  }
  $resolvedArtifactRoot = [System.IO.Path]::GetFullPath($artifactRuntimeRoot).TrimEnd('\') + '\'
  foreach ($kind in $artifactExtensions.Keys) {
    $property = $artifactManifest.artifacts.PSObject.Properties[$kind]
    if ($null -eq $property) {
      throw "packaged artifact runtime manifest is missing $kind"
    }
    $artifact = $property.Value
    $artifactPath = [System.IO.Path]::GetFullPath([string]$artifact.path)
    if (-not $artifactPath.StartsWith($resolvedArtifactRoot, [StringComparison]::OrdinalIgnoreCase)) {
      throw "packaged $kind artifact escaped the smoke output directory: $artifactPath"
    }
    if (-not (Test-Path $artifactPath -PathType Leaf)) {
      throw "packaged $kind runtime artifact is missing: $artifactPath"
    }
    if ([System.IO.Path]::GetExtension($artifactPath) -cne $artifactExtensions[$kind]) {
      throw "packaged $kind artifact has an unexpected extension: $artifactPath"
    }
    $actualSize = [int64](Get-Item $artifactPath).Length
    if ($actualSize -le 100 -or $actualSize -ne [int64]$artifact.size_bytes) {
      throw "packaged $kind artifact size is invalid (manifest=$($artifact.size_bytes), actual=$actualSize)"
    }
  }
  Copy-Item $artifactManifestPath (Join-Path $evidenceRoot "artifact-runtime-smoke.json") -Force

  $firstLaunchParameters = @{
    Label      = "first lifecycle"
    StdoutPath = $logPaths.FirstStdout
    StderrPath = $logPaths.FirstStderr
  }
  $appProcess = Start-InstalledApplication @firstLaunchParameters
  $firstHealth = Wait-BackendReady -Process $appProcess -Label "first lifecycle"
  Assert-BackendContract -Health $firstHealth -Label "first lifecycle"

  $started = Invoke-SmokeApi -Method POST -Path "/meetings/$meetingId/start"
  if ([string]$started.meeting_id -ne $meetingId -or [string]$started.status -ne "started") {
    throw "meeting start response did not confirm $meetingId"
  }
  $ended = Invoke-SmokeApi -Method POST -Path "/meetings/$meetingId/end"
  if ([string]$ended.meeting_id -ne $meetingId -or [string]$ended.status -ne "ended") {
    throw "meeting end response did not confirm $meetingId"
  }
  $firstMeetings = @(Invoke-SmokeApi -Method GET -Path "/meetings?limit=50")
  $firstMatches = @($firstMeetings | Where-Object { [string]$_.meeting_id -eq $meetingId })
  if ($firstMatches.Count -ne 1) {
    throw "first lifecycle did not return the newly persisted meeting $meetingId"
  }
  if ([string]$firstMatches[0].state -ne "ended") {
    throw "newly persisted meeting $meetingId has unexpected state '$($firstMatches[0].state)'"
  }
  $firstStartedAt = [string]$firstMatches[0].started_at
  if (-not $firstStartedAt) {
    throw "newly persisted meeting $meetingId has no started_at"
  }
  if (-not (Test-Path $dbPath -PathType Leaf) -or (Get-Item $dbPath).Length -le 0) {
    throw "real meeting write did not create a non-empty SQLite database: $dbPath"
  }

  Stop-InstalledApplicationGracefully -Process $appProcess -Label "first lifecycle"
  $appProcess = $null

  $secondLaunchParameters = @{
    Label      = "restart lifecycle"
    StdoutPath = $logPaths.SecondStdout
    StderrPath = $logPaths.SecondStderr
  }
  $appProcess = Start-InstalledApplication @secondLaunchParameters
  $secondHealth = Wait-BackendReady -Process $appProcess -Label "restart lifecycle"
  Assert-BackendContract -Health $secondHealth -Label "restart lifecycle"

  $secondMeetings = @(Invoke-SmokeApi -Method GET -Path "/meetings?limit=50")
  $secondMatches = @($secondMeetings | Where-Object { [string]$_.meeting_id -eq $meetingId })
  if ($secondMatches.Count -ne 1) {
    throw "restart lifecycle did not recover SQLite meeting $meetingId"
  }
  if ([string]$secondMatches[0].state -ne "ended") {
    throw "recovered meeting $meetingId has unexpected state '$($secondMatches[0].state)'"
  }
  if ([string]$secondMatches[0].started_at -ne $firstStartedAt) {
    throw "recovered meeting $meetingId changed started_at across restart"
  }

  Stop-InstalledApplicationGracefully -Process $appProcess -Label "restart lifecycle"
  $appProcess = $null
  Assert-SmokePortFree

  if (-not $portableMode) {
    foreach ($root in $contractProbeRoots) {
      if (Test-Path $root) {
        throw "installed application wrote outside the isolated --user-data-dir: $root"
      }
      New-Item -ItemType Directory -Force -Path $root | Out-Null
      $createdContractProbeRoots.Add($root)
      $sentinel = Join-Path $root ".echodesk-preserve-$runToken"
      Set-Content $sentinel -Value $runToken -Encoding utf8
      $contractProbeSentinels[$root] = $sentinel
    }

    Invoke-SilentUninstall `
      -StdoutPath $logPaths.UninstallStdout `
      -StderrPath $logPaths.UninstallStderr

    foreach ($root in $contractProbeRoots) {
      $sentinel = [string]$contractProbeSentinels[$root]
      if (-not (Test-Path $sentinel -PathType Leaf)) {
        throw "normal product user data was deleted by silent uninstall: $root"
      }
      if ((Get-Content $sentinel -Raw).Trim() -ne $runToken) {
        throw "normal product user-data sentinel changed during uninstall: $sentinel"
      }
    }
    if (-not (Test-Path $dbPath -PathType Leaf)) {
      throw "silent uninstall deleted the isolated product SQLite data: $dbPath"
    }
    Assert-SmokePortFree
  }

  $packageMode = if ($portableMode) { "portable-zip" } else { "installed-nsis" }
  $installDirectoryRemoved = if ($portableMode) { $null } else { $true }
  $normalUserDataPreserved = if ($portableMode) { $null } else { $true }
  $result = [ordered]@{
    ok                         = $true
    package_mode               = $packageMode
    timestamp_utc              = [DateTime]::UtcNow.ToString("o")
    app_version                = $productVersion
    backend_version            = [string]$secondHealth.version
    artifact_formats           = @($artifactExtensions.Keys)
    meeting_id                 = $meetingId
    meeting_started_at         = $firstStartedAt
    sqlite_bytes               = [int64](Get-Item $dbPath).Length
    restart_persistence        = $true
    port_released_after_exits  = $true
    authenticode_verified      = $authenticodeVerificationEnabled
    install_directory_removed  = $installDirectoryRemoved
    normal_user_data_preserved = $normalUserDataPreserved
  }
} catch {
  $failure = $_
  Write-FailureDiagnostics -Failure $_
} finally {
  $cleanupErrors = [System.Collections.Generic.List[string]]::new()

  if ($null -ne $appProcess) {
    try {
      $appProcess.Refresh()
      if (-not $appProcess.HasExited) {
        Stop-ProcessTreeBestEffort -ProcessId $appProcess.Id
      }
    } catch {
      $cleanupErrors.Add("app process cleanup failed: $($_.Exception.Message)")
    }
  }

  try {
    foreach ($listener in @(Get-SmokePortListeners)) {
      Stop-ProcessTreeBestEffort -ProcessId ([int]$listener.OwningProcess)
    }
    Wait-SmokePortClosed -TimeoutSeconds 15 -Context "final cleanup"
  } catch {
    $cleanupErrors.Add($_.Exception.Message)
  }

  if ($installationOwned -and (Test-Path $installDir)) {
    try {
      if (Test-Path $uninstaller -PathType Leaf) {
        $cleanupStdout = Join-Path $evidenceRoot "uninstaller-cleanup.stdout.log"
        $cleanupStderr = Join-Path $evidenceRoot "uninstaller-cleanup.stderr.log"
        Invoke-SilentUninstall -StdoutPath $cleanupStdout -StderrPath $cleanupStderr
      } else {
        $cleanupErrors.Add("cannot clean failed installation because uninstaller is missing: $uninstaller")
      }
    } catch {
      $cleanupErrors.Add("fallback uninstall failed: $($_.Exception.Message)")
    }
  }

  try {
    Restore-SmokeEnvironment
  } catch {
    $cleanupErrors.Add("environment restore failed: $($_.Exception.Message)")
  }

  foreach ($root in $createdContractProbeRoots) {
    try {
      if (Test-Path $root) {
        Remove-Item $root -Recurse -Force
      }
    } catch {
      $cleanupErrors.Add("contract probe cleanup failed for ${root}: $($_.Exception.Message)")
    }
  }

  try {
    if (Test-Path $smokeRoot) {
      Remove-Item $smokeRoot -Recurse -Force
    }
    if (Test-Path $smokeRoot) {
      throw "smoke data directory still exists after cleanup: $smokeRoot"
    }
  } catch {
    $cleanupErrors.Add("smoke data cleanup failed: $($_.Exception.Message)")
  }

  if ($cleanupErrors.Count -gt 0) {
    try {
      $cleanupErrors |
        Set-Content (Join-Path $evidenceRoot "cleanup-errors.log") -Encoding utf8
    } catch {
      Write-Warning "failed to write cleanup-errors.log: $($_.Exception.Message)"
    }
    foreach ($message in $cleanupErrors) {
      Write-Warning $message
    }
    if ($null -eq $failure) {
      $failure = [System.InvalidOperationException]::new(
        "Windows installed smoke cleanup failed: $($cleanupErrors -join '; ')"
      )
    }
  }
}

if ($null -ne $failure) {
  if (-not (Test-Path (Join-Path $evidenceRoot "failure.json") -PathType Leaf)) {
    Write-FailureDiagnostics -Failure $failure
  }
  Write-Host "[windows-smoke] evidence retained at $evidenceRoot" -ForegroundColor Yellow
  throw $failure
}

if ($null -eq $result) {
  throw "Windows installed smoke completed without a result"
}
$result | ConvertTo-Json -Depth 6 |
  Set-Content (Join-Path $evidenceRoot "result.json") -Encoding utf8
Write-Host "[windows-smoke] PASS app=$($result.app_version) backend=$($result.backend_version) meeting=$($result.meeting_id) port=$Port"
Write-Host "[windows-smoke] evidence retained at $evidenceRoot"
