param([switch]$UiSmoke)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Launcher-Language.ps1')
$app = Join-Path $PSScriptRoot '15_Control-Center\App\JuiAI.ControlCenter.exe'
if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
    throw (Get-JuiText 'Build the native client with Build-ControlCenter.ps1 or extract the Windows x64 release ZIP.' '请先运行 Build-ControlCenter.ps1 构建桌面客户端，或解压 Windows x64 发布包。')
}

& (Join-Path $PSScriptRoot 'Initialize-Terminal.ps1')

if (-not ('JuiAI.LauncherPackageIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace JuiAI
{
    public static class LauncherPackageIdentity
    {
        private const int ErrorInsufficientBuffer = 122;
        private const int AppModelErrorNoPackage = 15700;

        [DllImport("kernel32.dll", ExactSpelling = true)]
        private static extern int GetCurrentPackageFullName(ref uint length, IntPtr name);

        public static bool IsPackaged()
        {
            uint length = 0;
            int result = GetCurrentPackageFullName(ref length, IntPtr.Zero);
            if (result == ErrorInsufficientBuffer) return true;
            if (result == AppModelErrorNoPackage) return false;
            throw new Win32Exception(result);
        }
    }
}
'@
}

$directory = Split-Path -Parent $app
$arguments = if ($UiSmoke) { '--ui-smoke' } else { '' }
if ([JuiAI.LauncherPackageIdentity]::IsPackaged()) {
    # Launch from Explorer so the unpackaged WinUI app has its own resource context.
    $shell = New-Object -ComObject Shell.Application
    $desktopWindow = 8
    $needDispatch = 1
    $windowHandle = 0
    $desktop = $shell.Windows().FindWindowSW(0, 0, $desktopWindow, [ref]$windowHandle, $needDispatch)
    if ($null -eq $desktop) { throw (Get-JuiText 'Windows Explorer desktop is unavailable for launching JuiAI.' '无法通过 Windows 资源管理器启动 JuiAI Terminal。') }
    $desktop.Document.Application.ShellExecute($app, $arguments, $directory, 'open', 1)
    return
}

$start = @{ FilePath = $app; WorkingDirectory = $directory }
if ($arguments) { $start.ArgumentList = $arguments }
Start-Process @start
