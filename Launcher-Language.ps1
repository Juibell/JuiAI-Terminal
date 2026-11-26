# This preference is shared with the desktop client's Settings page.
$useChinese = $false
$preference = Join-Path $PSScriptRoot '05_Agent-Router\.runtime\ui-language.txt'
try {
    if ([IO.File]::Exists($preference)) { $useChinese = [IO.File]::ReadAllText($preference).Trim() -ceq 'zh-CN' }
} catch [IO.IOException] { } catch [UnauthorizedAccessException] { }
if ($env:JUI_LAUNCHER_UTF8 -eq '1') { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) }

function Get-JuiText([string]$English, [string]$Chinese) {
    if ($useChinese) { return $Chinese }
    return $English
}
