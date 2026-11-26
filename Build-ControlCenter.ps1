param([switch]$Test, [switch]$UiSmoke, [string]$OutputDirectory, [string]$Version = '0.2.0')
$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot '15_Control-Center'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $project 'App' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
Push-Location -LiteralPath $project
try {
    if ($Test) {
        & dotnet run --project 'Tests\JuiAI.ClientTests.csproj'
        if ($LASTEXITCODE -ne 0) { throw 'Client checks failed.' }
        & uv --no-config run --project (Join-Path $PSScriptRoot '05_Agent-Router') --locked --directory (Join-Path $PSScriptRoot '05_Agent-Router') python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Backend checks failed.' }
    }
    & dotnet publish 'JuiAI.ControlCenter.csproj' -c Release -p:Platform=x64 -p:DebugType=None -p:DebugSymbols=false "-p:Version=$Version" -o $OutputDirectory --self-contained true
    if ($LASTEXITCODE -ne 0) { throw 'Native publish failed.' }
    foreach ($resource in @('JuiAI.ControlCenter.exe','App.xbf','MainWindow.xbf','JuiAI.ControlCenter.pri')) {
        if (-not (Test-Path -LiteralPath (Join-Path $OutputDirectory $resource) -PathType Leaf)) { throw "Native publish is missing $resource." }
    }
    if ($UiSmoke) {
        if ($OutputDirectory -ne (Join-Path $project 'App')) { throw 'UI smoke testing requires the default publish directory.' }
        & (Join-Path $PSScriptRoot 'Start-JuiAI.ps1') -UiSmoke
    }
    Write-Output "Native client published to $OutputDirectory"
} finally { Pop-Location }
