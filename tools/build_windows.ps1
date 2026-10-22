param(
    [string]$Python = 'python',
    [switch]$OneDir
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Build on Windows to produce a Windows EXE.' }
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $repoRoot '.build-venv'
$buildPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    & $Python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
}
& $buildPython -m pip install -r (Join-Path $repoRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
$bundleMode = '--onefile'
if ($OneDir) { $bundleMode = '--onedir' }
$buildArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--noupx',
    $bundleMode, '--name', 'RailMail', '--paths', $repoRoot,
    '--distpath', (Join-Path $repoRoot 'dist'),
    '--workpath', (Join-Path $repoRoot 'build'),
    '--specpath', (Join-Path $repoRoot 'build'),
    '--version-file', (Join-Path $PSScriptRoot 'windows_version.txt'),
    (Join-Path $PSScriptRoot 'windows_entry.py')
)
& $buildPython @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
Write-Host ('Build complete: ' + (Join-Path $repoRoot 'dist'))
