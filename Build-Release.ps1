param([ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = '0.1.0', [string]$OutputDirectory = (Join-Path $PSScriptRoot 'releases'))
$ErrorActionPreference = 'Stop'
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
[void][IO.Directory]::CreateDirectory($OutputDirectory)
$tmpRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$scratch = Join-Path $tmpRoot ('jui-package-' + [guid]::NewGuid().ToString('N'))
$bundle = Join-Path $scratch 'JuiAI-Terminal'
$project = Join-Path $PSScriptRoot '15_Control-Center'
$client = Join-Path $bundle '15_Control-Center\App'
$router = Join-Path $bundle '05_Agent-Router'
$asset = Join-Path $OutputDirectory "JuiAI-Terminal-v$Version-windows-x64.zip"
$partial = "$asset.$([guid]::NewGuid().ToString('N')).partial"
[void][IO.Directory]::CreateDirectory($router)
try {
    & (Join-Path $PSScriptRoot 'Build-ControlCenter.ps1') -OutputDirectory $client -Version $Version
    # Build a branded entry point with the .NET Framework compiler included in x64 Windows.
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    $icon = Join-Path $project 'Assets\JuiAI-Terminal.ico'
    $launcher = Join-Path $bundle 'JuiAI-Terminal.exe'
    & $compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll "/win32icon:$icon" "/out:$launcher" (Join-Path $PSScriptRoot 'launcher\Program.cs')
    if ($LASTEXITCODE -ne 0) { throw 'The Windows launcher could not be built.' }
    foreach ($name in @('JuiAI-Terminal.cmd','Initialize-Terminal.ps1','Start-Jui.ps1','Start-JuiAI.ps1','README.md')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $bundle
    }
    $art = Join-Path $bundle '15_Control-Center\Assets'
    [void][IO.Directory]::CreateDirectory($art)
    Copy-Item -LiteralPath (Join-Path $project 'Assets\JuiAI-Terminal.png') -Destination $art
    # Package only executable router inputs. User state and developer environments live alongside them.
    $source = Join-Path $PSScriptRoot '05_Agent-Router'
    foreach ($name in @('settings.json','pyproject.toml','uv.lock','configure.py','serve.py','control_mcp.py','mcp_server.py','update_tools.py')) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination $router
    }
    foreach ($entry in @(@('jui','*.py'), @('plugins','*.mjs'))) {
        $from = Join-Path $source $entry[0]
        $to = Join-Path $router $entry[0]
        foreach ($file in Get-ChildItem -LiteralPath $from -File -Recurse -Filter $entry[1]) {
            $destination = Join-Path $to $file.FullName.Substring($from.Length + 1)
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            Copy-Item -LiteralPath $file.FullName -Destination $destination
        }
    }
    # Preserve notices shipped by NuGet dependencies of this particular runtime build.
    $notices = Join-Path $bundle 'licenses'
    [void][IO.Directory]::CreateDirectory($notices)
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses\Markdig.txt') -Destination $notices
    $deps = Get-Content -Raw -LiteralPath (Join-Path $client 'JuiAI.ControlCenter.deps.json') | ConvertFrom-Json
    $assets = Get-Content -Raw -LiteralPath (Join-Path $project 'obj\project.assets.json') | ConvertFrom-Json
    foreach ($item in $deps.libraries.PSObject.Properties.Name) {
        $package = $item -replace '^runtimepack\.', ''
        foreach ($cache in $assets.packageFolders.PSObject.Properties.Name) {
            $path = Join-Path $cache $package
            if (-not (Test-Path -LiteralPath $path -PathType Container)) { continue }
            foreach ($file in Get-ChildItem -LiteralPath $path -File) {
                if ($file.Name -match '^(license|licence|notice|third[-_ ]?party[-_ ]?notices?)(\.txt|\.md)?$') {
                    $label = ($package -replace '/','-') + '-' + $file.Name
                    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $notices $label)
                }
            }
            break
        }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($bundle, $partial, [IO.Compression.CompressionLevel]::Optimal, $true)
    Move-Item -LiteralPath $partial -Destination $asset -Force
    Get-FileHash -Algorithm SHA256 -LiteralPath $asset
} finally {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial }
    $resolved = (Resolve-Path -LiteralPath $scratch).ProviderPath
    if ([IO.Path]::GetDirectoryName($resolved).TrimEnd('\') -eq $tmpRoot.TrimEnd('\') -and [IO.Path]::GetFileName($resolved) -match '^jui-package-[0-9a-f]{32}$') { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
