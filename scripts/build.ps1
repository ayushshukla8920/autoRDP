<#
.SYNOPSIS
    Build autordp.exe locally, and prove it is not hollow.

.DESCRIPTION
    Creates the virtual environment if needed, installs dependencies, builds
    with PyInstaller, and then runs the result.

    That last step is the point. A PyInstaller build can succeed and still
    produce a binary that is missing half of what it needs, because aardwolf
    loads its keyboard layouts and unicrypto its crypto backend through
    importlib, and static analysis cannot see either. "It compiled" is not
    evidence. `autordp doctor` loads both and says so.

    CI does this on five platforms; this script is the Windows one, for when
    you want to test a change without pushing a tag.

.PARAMETER Clean
    Discard PyInstaller's caches and dist\ first. Worth doing after changing
    the spec or upgrading a dependency.

.PARAMETER Npm
    Also assemble the npm packages around the binary, into dist\npm. Needs
    node on PATH. The result is publishable with `npm publish dist\npm\<pkg>`,
    though a real release should go through .github/workflows/release.yml so
    that every platform is built from the same commit.

.PARAMETER SkipSmokeTest
    Build only. There is rarely a good reason: the smoke test is the part that
    catches the failure mode this build actually has.

.EXAMPLE
    .\scripts\build.ps1
.EXAMPLE
    .\scripts\build.ps1 -Clean -Npm
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Npm,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($n, $text) { Write-Host "[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "    $text" -ForegroundColor Green }
function Note($text)     { Write-Host "    $text" -ForegroundColor DarkGray }
function Fail($text)     { Write-Host ""; Write-Host "BUILD FAILED: $text" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "autordp build" -ForegroundColor White
Write-Host ("-" * 52)

# --- 1. interpreter ---------------------------------------------------------
Step 1 "Checking the virtual environment"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    # 3.13 specifically: aardwolf's dependency arc4 has no 3.14 wheel, and
    # aardwolf itself is pinned to 0.2.13, the last release with prebuilt
    # wheels for 3.9-3.13. See requirements.txt.
    Note "No .venv yet, creating one with py -3.13"
    & py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create a 3.13 virtual environment." }
}
$pyVersion = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Ok "python $pyVersion"

# --- 2. dependencies --------------------------------------------------------
Step 2 "Installing dependencies"
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r requirements.txt --quiet
& $python -m pip install "pyinstaller>=6.3" --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
$pyi = & $python -m PyInstaller --version
Ok "PyInstaller $pyi"

# --- 3. build ---------------------------------------------------------------
Step 3 "Building"
if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path build | Out-Null
$outLog = Join-Path $root "build\pyinstaller-out.log"
$errLog = Join-Path $root "build\pyinstaller-err.log"

# Note: NOT $args -- that is a PowerShell automatic variable.
$pyiArgs = @("-m", "PyInstaller", "autordp.spec", "--noconfirm", "--log-level", "WARN")
if ($Clean) { $pyiArgs += "--clean" }

# PyInstaller prints a wall of third-party noise: Cryptodome's self-test suite
# emits UserWarnings just from being imported during analysis, arc4 is a single
# module rather than a package, and pycparser's parser tables are generated at
# runtime. None of it is actionable, so it goes to a log and only the summary
# reaches the screen. Start-Process is used rather than a pipeline because
# Windows PowerShell 5.1 wraps a native command's stderr in ErrorRecords and
# then reports a non-zero result even on success.
$before = $env:PYTHONWARNINGS
$env:PYTHONWARNINGS = "ignore"
try {
    $run = Start-Process -FilePath $python -ArgumentList $pyiArgs -NoNewWindow -Wait `
                         -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
} finally {
    $env:PYTHONWARNINGS = $before
}

if ($run.ExitCode -ne 0) {
    Write-Host ""
    Get-Content $errLog -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkYellow }
    Fail "PyInstaller exited with $($run.ExitCode). Full log: $errLog"
}
$warnings = @(Select-String -Path $errLog -Pattern "^\d+ WARNING:" -ErrorAction SilentlyContinue)
Note "$($warnings.Count) benign warning(s) from dependencies, logged to build\pyinstaller-err.log"

$exe = Join-Path $root "dist\autordp.exe"
if (-not (Test-Path $exe)) { Fail "Expected $exe but it is not there." }
$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Ok "$exe  ($mb MB)"

# --- 4. smoke test ----------------------------------------------------------
if ($SkipSmokeTest) {
    Step 4 "Smoke test skipped (-SkipSmokeTest)"
} else {
    Step 4 "Smoke test: is the bundled RDP stack complete?"

    $version = & $exe --version 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "$exe --version exited $LASTEXITCODE`: $version" }
    Ok "$version"

    $reportPath = Join-Path $root "build\doctor.json"
    & $exe doctor --json --no-network --no-input 2>$null |
        Set-Content $reportPath -Encoding utf8
    if (-not (Test-Path $reportPath)) { Fail "doctor produced no output." }

    $report = Get-Content $reportPath -Raw | ConvertFrom-Json
    # Only these two say anything about the build. The rest of doctor is about
    # this machine's configuration, which has nothing to do with whether the
    # binary is whole.
    foreach ($name in @("rdp stack", "keyboard layout")) {
        $check = $report.checks | Where-Object { $_.name -eq $name }
        if ($null -eq $check) { Fail "doctor did not report a '$name' check." }
        if ($check.status -ne "ok") { Fail "$name is broken in this build: $($check.detail)" }
        Ok "$name`: $($check.detail)"
    }
}

# --- 5. npm packages --------------------------------------------------------
if ($Npm) {
    Step 5 "Assembling the npm packages"
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Fail "node is not on PATH, so -Npm cannot run scripts\build-npm.mjs."
    }
    $stage = Join-Path $root "dist\artifacts\autordp-win32-x64"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item $exe (Join-Path $stage "autordp.exe") -Force
    & node scripts\build-npm.mjs --artifacts dist\artifacts --out dist\npm
    if ($LASTEXITCODE -ne 0) { Fail "build-npm.mjs exited $LASTEXITCODE." }
    Note "Only win32-x64 was built here; the other platforms are skipped."
    Note "A real release runs .github\workflows\release.yml, which builds all five."
}

Write-Host ""
Write-Host "Ready: $exe" -ForegroundColor Green
Write-Host ""
Note "STOP and screenshots\ are written to whatever directory you run it from,"
Note "not next to the binary. RDP_STOP_FILE and RDP_SCREENSHOT_DIR override that."
