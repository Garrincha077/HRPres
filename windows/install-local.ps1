param([string]$Destination = 'D:\HRPres')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$targetRoot = [System.IO.Path]::GetFullPath($Destination)
if ($targetRoot -eq [System.IO.Path]::GetPathRoot($targetRoot)) {
    throw 'Odaberi zasebnu mapu za HRPres, ne korijen diska.'
}
$appRoot = Join-Path $targetRoot 'app'
New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
foreach ($item in @('src','windows','pyproject.toml','README.md','LICENSE')) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $item) -Destination $appRoot -Recurse -Force
}
$envRoot = Join-Path $targetRoot '.venv'
$pythonPath = Join-Path $envRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & py -3.12 -m venv $envRoot
    if ($LASTEXITCODE -ne 0) { throw 'Za pripremu je potreban Python 3.12.' }
}
& $pythonPath -X utf8 -m pip install -e $appRoot
if ($LASTEXITCODE -ne 0) { throw 'Instalacija ovisnosti nije uspjela.' }
foreach ($launcher in @('POKRENI-HRPRES.cmd','POKRENI-PILOT-25.cmd','NASTAVI-PREUZIMANJE.cmd','PAUZIRAJ-HRPRES.cmd','STANJE-HRPRES.cmd')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $launcher) -Destination $targetRoot -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'UPUTE.txt') -Destination $targetRoot -Force
Write-Output "HRPres je pripremljen u $targetRoot. Otvori POKRENI-HRPRES.cmd. Podaci se spremaju u data\."
