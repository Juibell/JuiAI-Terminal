param([ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start', [switch]$Native)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Launcher-Language.ps1')
$app = Join-Path $PSScriptRoot '05_Agent-Router'
$recordPath = Join-Path $app '.runtime\workbench-process.json'
$logs = Join-Path $PSScriptRoot '12_Logs\Workbench'
$settings = Get-Content -LiteralPath (Join-Path $app 'settings.json') -Raw | ConvertFrom-Json
$port = [int]$settings.port
if ($port -lt 1 -or $port -gt 65535) { throw (Get-JuiText 'settings.json must specify a valid port.' '请在 settings.json 中指定有效的端口。') }
$url = "http://127.0.0.1:$port/"

function Get-RecordedProcess {
    if (Test-Path -LiteralPath $recordPath) {
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        $item = Get-Process -Id $record.Pid -ErrorAction SilentlyContinue
        if ($item -and $item.Path -eq $record.Executable -and $item.StartTime.ToUniversalTime().Ticks -eq $record.StartTicks) { return $item }
    }
}

if ($Action -in @('Stop','Status')) {
    $item = Get-RecordedProcess
    if ($Action -eq 'Status') { [pscustomobject]@{ Running=[bool]$item; Url=$url; Pid=$item.Id }; return }
    if ($item) {
        $item.Kill()
        if (-not $item.WaitForExit(15000)) { throw (Get-JuiText 'The local backend did not stop.' '本地后端未能停止。') }
    }
    if (Test-Path -LiteralPath $recordPath) { Remove-Item -LiteralPath $recordPath }
    return
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    $item = Get-RecordedProcess
    if (-not $item) { throw (Get-JuiText "Port $port is in use. Set a free port in 05_Agent-Router/settings.json." "端口 $port 已被占用。请在 05_Agent-Router/settings.json 中指定其他可用端口。") }
    $health = Invoke-RestMethod ($url + 'api/health') -TimeoutSec 5
    if (-not $health.native -or $health.control_api -ne 1) { throw (Get-JuiText 'The service on the configured port does not expose the native control API.' '当前端口上的服务不支持桌面客户端所需的控制接口。') }
    Write-Output (Get-JuiText "JuiAI Terminal is already running at $url" "JuiAI Terminal 已在 $url 运行。")
    return
}
& (Join-Path $PSScriptRoot 'Initialize-Terminal.ps1')
[void][IO.Directory]::CreateDirectory($logs)
$venvPython = Join-Path $app '.venv\Scripts\python.exe'
# Launch the actual interpreter so the process record identifies the server on Windows.
$basePython = & $venvPython -c 'import sys; print(sys._base_executable)'
if ($LASTEXITCODE -ne 0) { throw (Get-JuiText 'Could not resolve the backend interpreter.' '无法找到后端使用的 Python 解释器。') }
$bootstrap = 'import site,runpy,sys; site.addsitedir(sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")'
$arguments = @('-s', '-c', ('"' + $bootstrap.Replace('"','\"') + '"'), ('"' + (Join-Path $app '.venv\Lib\site-packages') + '"'), ('"' + (Join-Path $app 'serve.py') + '"'))
$previousNative = $env:JUI_NATIVE
try {
    $env:JUI_NATIVE = '1'
    $process = Start-Process -FilePath $basePython.Trim() -ArgumentList $arguments -WorkingDirectory $app -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'workbench.stdout.log') -RedirectStandardError (Join-Path $logs 'workbench.stderr.log') -PassThru
} finally { $env:JUI_NATIVE = $previousNative }
@{Pid=$process.Id; Executable=$basePython.Trim(); StartTicks=$process.StartTime.ToUniversalTime().Ticks} | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if ($process.HasExited) { throw (Get-JuiText 'The backend exited. See 12_Logs\Workbench\workbench.stderr.log.' '后端已退出。请查看 12_Logs\Workbench\workbench.stderr.log。') }
    try {
        $health = Invoke-RestMethod ($url + 'api/health') -TimeoutSec 2
        if ($health.native -and $health.control_api -eq 1) { Write-Output (Get-JuiText "JuiAI Terminal is ready at $url" "JuiAI Terminal 已就绪，地址为 $url"); return }
    } catch { }
    Start-Sleep -Milliseconds 500
}
& $PSCommandPath -Action Stop
throw (Get-JuiText 'The local backend did not respond in time.' '本地后端响应超时。')
