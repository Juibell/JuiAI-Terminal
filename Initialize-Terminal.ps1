param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Launcher-Language.ps1')
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
    Write-Host (Get-JuiText 'Downloading uv for the private Python environment...' '正在下载 uv，准备独立的 Python 环境……')
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) { throw (Get-JuiText 'The uv download did not match the expected SHA-256 digest.' '下载的 uv 文件未通过 SHA-256 校验。') }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $entry = $archive.GetEntry('uv.exe')
            if (-not $entry) { throw (Get-JuiText 'The uv download is missing uv.exe.' '下载的压缩包中缺少 uv.exe。') }
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
catch [IO.IOException] { throw (Get-JuiText 'Another JuiAI Terminal setup is in progress. Open the app after it finishes.' '另一个 JuiAI Terminal 实例正在初始化。请等待完成后再打开应用。') }
try {
    $ready = (Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $marker) -and ((Get-Content -Raw -LiteralPath $marker).Trim() -eq $hash)
    if (-not $ready) {
        $uv = Resolve-Uv
        Write-Host (Get-JuiText 'Preparing Python 3.12 and the locked backend dependencies. First-time setup requires an internet connection.' '正在准备 Python 3.12 和后端依赖。首次安装需要联网。')
        $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
        try {
            $env:UV_PROJECT_ENVIRONMENT = $environment
            & $uv --no-config sync --project $router --locked --python 3.12 --no-dev
            if ($LASTEXITCODE -ne 0) { throw (Get-JuiText 'Dependency installation failed. Check the download output and run the launcher again.' '依赖安装失败。请查看下载日志，然后重新运行启动器。') }
        } finally { $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment }
        Set-Content -LiteralPath $marker -Value $hash -Encoding ascii
    }
    $profileMarker = Join-Path $runtime 'initialized-root.txt'
    $profilesReady = (Test-Path -LiteralPath (Join-Path $runtime 'secrets.json')) -and (Test-Path -LiteralPath $profileMarker) -and ((Get-Content -Raw -LiteralPath $profileMarker).Trim() -eq $PSScriptRoot)
    if (-not $profilesReady) {
        # Decode the configured language correctly through Windows PowerShell's native pipeline.
        $previousOutputEncoding = [Console]::OutputEncoding
        try {
            [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
            & $python -X utf8 (Join-Path $router 'configure.py')
        } finally { [Console]::OutputEncoding = $previousOutputEncoding }
        if ($LASTEXITCODE -ne 0) { throw (Get-JuiText 'Local profile initialization failed.' '本地配置初始化失败。') }
        [IO.File]::WriteAllText($profileMarker, $PSScriptRoot, [Text.UTF8Encoding]::new($true))
    }
} finally { $lock.Dispose() }
