<#
.SYNOPSIS
    One-click release build of autoRDP.exe.

.DESCRIPTION
    Creates the virtual environment if needed, installs dependencies, refreshes
    the icon when the source art has changed, builds with PyInstaller, and then
    actually launches the result to confirm it starts and opens its window.

    That last step is the point. A PyInstaller build can succeed and still
    produce an exe that dies on startup, because packages like aardwolf and
    unicrypto import things dynamically and static analysis cannot see them.
    "It compiled" is not evidence; "it opened a window" is.

.PARAMETER OneDir
    Build a folder instead of a single file. Measured ~1s to window against
    ~12s for the single file, which has to unpack its whole archive to a temp
    directory on every launch. Costs you a 66 MB folder instead of one 32 MB
    exe.

.PARAMETER SkipSmokeTest
    Build only. Use on a machine where launching a GUI is not possible.

.PARAMETER Clean
    Discard PyInstaller's caches and dist/ first. Worth doing after changing
    the spec or upgrading a dependency. It is not the slow option people expect:
    measured at ~40s either way, because PyInstaller re-runs its analysis every
    build and the cache saves little here.

.EXAMPLE
    .\build.ps1
.EXAMPLE
    .\build.ps1 -OneDir -Clean
#>
[CmdletBinding()]
param(
    [switch]$OneDir,
    [switch]$SkipSmokeTest,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

function Step($n, $text) { Write-Host "[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "    $text" -ForegroundColor Green }
function Note($text)     { Write-Host "    $text" -ForegroundColor DarkGray }
function Fail($text)     { Write-Host "" ; Write-Host "BUILD FAILED: $text" -ForegroundColor Red ; exit 1 }

Write-Host ""
Write-Host "autoRDP release build" -ForegroundColor White
Write-Host ("-" * 52)

# --- 1. interpreter ---------------------------------------------------------
Step 1 "Checking the virtual environment"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Note "No .venv yet, creating one"
    # 3.13 specifically: aardwolf's dependency arc4 has no 3.14 wheel, and
    # 0.2.13 is the newest aardwolf with Windows wheels for 3.9-3.13.
    $base = (Get-Command py -ErrorAction SilentlyContinue)
    if ($null -eq $base) { Fail "Python launcher 'py' not found. Install Python 3.13." }
    & py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create a 3.13 virtual environment." }
}
$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($version -eq "3.14") { Fail "Python 3.14 cannot install arc4 (no wheel). Use 3.13." }
Ok "Python $version"

# --- 2. dependencies --------------------------------------------------------
Step 2 "Installing dependencies"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed." }
& $python -m pip install --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "pip install pyinstaller failed." }
$pyi = & $python -m PyInstaller --version
Ok "PyInstaller $pyi"

# --- 3. icon ----------------------------------------------------------------
Step 3 "Icon"
$source = Join-Path $root "assets\icon-source.png"
$icon   = Join-Path $root "assets\autoRDP.ico"
if (-not (Test-Path $source)) {
    Note "assets\icon-source.png missing, keeping the existing autoRDP.ico"
} elseif ((-not (Test-Path $icon)) -or
          ((Get-Item $source).LastWriteTime -gt (Get-Item $icon).LastWriteTime)) {
    & $python make_icon.py
    if ($LASTEXITCODE -ne 0) { Fail "Icon generation failed." }
    Ok "assets\autoRDP.ico rebuilt from icon-source.png"
} else {
    Ok "assets\autoRDP.ico is up to date"
}

# --- 4. build ---------------------------------------------------------------
$spec = "autoRDP.spec"
if ($OneDir) {
    # The spec has a ONEFILE switch at the top; write a variant rather than
    # editing the tracked file.
    $spec = "autoRDP-onedir.spec"
    (Get-Content "autoRDP.spec" -Raw).Replace("ONEFILE = True", "ONEFILE = False") |
        Set-Content $spec -Encoding utf8
    Note "Building the one-folder variant"
}

Step 4 "Building"
if ($Clean) { Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path build | Out-Null
$outLog = Join-Path $root "build\pyinstaller-out.log"
$errLog = Join-Path $root "build\pyinstaller-err.log"

# Note: NOT $args -- that is a PowerShell automatic variable.
$pyiArgs = @("-m", "PyInstaller", $spec, "--noconfirm", "--log-level", "WARN")
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

$errors = @(Select-String -Path $errLog -Pattern "^\d+ ERROR:" -ErrorAction SilentlyContinue)
$warnings = @(Select-String -Path $errLog -Pattern "^\d+ WARNING:" -ErrorAction SilentlyContinue)
if ($run.ExitCode -ne 0) {
    Write-Host ""
    Get-Content $errLog -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkYellow }
    Fail "PyInstaller exited with $($run.ExitCode). Full log: $errLog"
}
foreach ($e in $errors) { Write-Host "    $($e.Line)" -ForegroundColor Yellow }
Note "$($warnings.Count) benign warning(s) from dependencies, logged to build\pyinstaller-err.log"

$exe = if ($OneDir) { Join-Path $root "dist\autoRDP\autoRDP.exe" }
       else          { Join-Path $root "dist\autoRDP.exe" }
if (-not (Test-Path $exe)) { Fail "Expected $exe but it is not there." }
$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Ok "$exe  ($mb MB)"
if ($OneDir) {
    $folder = [math]::Round(((Get-ChildItem "dist\autoRDP" -Recurse |
        Measure-Object Length -Sum).Sum / 1MB), 1)
    Note "folder total: $folder MB - ship the whole dist\autoRDP directory"
}

# --- 5. smoke test ----------------------------------------------------------
if ($SkipSmokeTest) {
    Step 5 "Smoke test skipped (-SkipSmokeTest)"
} else {
    Step 5 "Smoke test: does it actually open a window?"
    Get-Process autoRDP -ErrorAction SilentlyContinue | Stop-Process -Force
    $proc = Start-Process $exe -PassThru
    # One-file has to unpack ~33 MB to a temp directory before Python starts.
    $limit = if ($OneDir) { 25 } else { 60 }
    $title = $null
    for ($i = 0; $i -lt $limit; $i++) {
        Start-Sleep -Seconds 1
        $windowed = Get-Process autoRDP -ErrorAction SilentlyContinue |
                    Where-Object { $_.MainWindowTitle -ne "" }
        if ($windowed) { $title = $windowed[0].MainWindowTitle; break }
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
            Fail "The exe exited before opening a window. Build a console copy to see the traceback: change console=False to console=True in $spec."
        }
    }
    Get-Process autoRDP -ErrorAction SilentlyContinue | Stop-Process -Force
    if ($null -eq $title) {
        Fail "No window appeared within ${limit}s. The exe started but something is hanging."
    }
    Ok "window opened after ${i}s: `"$title`""
}

if ($OneDir) { Remove-Item "autoRDP-onedir.spec" -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "Release ready: $exe" -ForegroundColor Green
Write-Host ""
Note "The exe keeps its STOP file and screenshots\ next to itself,"
Note "so put it somewhere writable rather than C:\Program Files."
