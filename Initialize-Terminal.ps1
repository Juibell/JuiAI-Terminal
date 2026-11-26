param()
$ErrorActionPreference = 'Stop'
$router = Join-Path $PSScriptRoot '05_Agent-Router'
$runtime = Join-Path $router '.runtime'
$environment = Join-Path $router '.venv'
$python = Join-Path $environment 'Scripts\python.exe'
$marker = Join-Path $environment 'jui-dependency-hashes.txt'
$hash = ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $router 'uv.lock'), (Join-Path $router 'pyproject.toml')).Hash) -join '/'

function Resolve-Uv {
    $installed = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installed) { return $installed.Source }
    # Bootstrap a private copy when the user does not already have uv.
    $toolsDir = Join-Path $runtime 'tools'
    $exe = Join-Path $toolsDir 'uv-0.13.1.exe'
    if (Test-Path -LiteralPath $exe -PathType Leaf) { return $exe }
    [void][IO.Directory]::CreateDirectory($toolsDir)
    $zip = Join-Path $toolsDir 'uv-0.13.1.zip'
    $url = 'https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip'
    $expected = '3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672'
    Write-Host 'Downloading uv for the private Python environment...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) { throw 'The uv download did not match the expected SHA-256 digest.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $entry = $archive.GetEntry('uv.exe')
            if (-not $entry) { throw 'The uv download is missing uv.exe.' }
            $tmp = "$exe.partial"
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $tmp, $true)
            Move-Item -LiteralPath $tmp -Destination $exe -Force
        } finally { $archive.Dispose() }
    } finally { if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip } }
    return $exe
}

[void][IO.Directory]::CreateDirectory($runtime)
# Another launcher may be preparing the same installation.
$lockPath = Join-Path $runtime 'setup.lock'
try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch [IO.IOException] { throw 'Another JuiAI Terminal setup is in progress. Open the app after it finishes.' }
try {
    $ready = (Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $marker) -and ((Get-Content -Raw -LiteralPath $marker).Trim() -eq $hash)
    if (-not $ready) {
        $uv = Resolve-Uv
        Write-Host 'Preparing Python 3.12 and the locked backend dependencies. First-time setup requires an internet connection.'
        $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
        try {
            $env:UV_PROJECT_ENVIRONMENT = $environment
            & $uv --no-config sync --project $router --locked --python 3.12 --no-dev
            if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the download output and run the launcher again.' }
        } finally { $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment }
        Set-Content -LiteralPath $marker -Value $hash -Encoding ascii
    }
    $profileMarker = Join-Path $runtime 'initialized-root.txt'
    $profilesReady = (Test-Path -LiteralPath (Join-Path $runtime 'secrets.json')) -and (Test-Path -LiteralPath $profileMarker) -and ((Get-Content -Raw -LiteralPath $profileMarker).Trim() -eq $PSScriptRoot)
    if (-not $profilesReady) {
        & $python (Join-Path $router 'configure.py')
        if ($LASTEXITCODE -ne 0) { throw 'Local profile initialization failed.' }
        [IO.File]::WriteAllText($profileMarker, $PSScriptRoot, [Text.UTF8Encoding]::new($true))
    }
} finally { $lock.Dispose() }
