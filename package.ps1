$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}

& $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --windowed `
        --name "ChatGPTQuestionIndexer" `
        --paths (Join-Path $ProjectRoot "src") `
        --add-data "$ProjectRoot\config.json;." `
        --hidden-import comtypes.stream `
        (Join-Path $ProjectRoot "run.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "config.json") `
        -Destination (Join-Path $ProjectRoot "dist\ChatGPTQuestionIndexer\config.json") `
        -Force
    Write-Output "生成的可执行程序位置：$(Join-Path $ProjectRoot 'dist\ChatGPTQuestionIndexer\ChatGPTQuestionIndexer.exe')"
}
finally {
    Pop-Location
}
