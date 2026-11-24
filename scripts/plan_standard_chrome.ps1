#requires -Version 7.4
[CmdletBinding()]
param([string]$InstallerPath,[string]$OutputDirectory)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'
$script:ChromeInstallerInput=$InstallerPath; $script:ChromeOutput=$OutputDirectory
. (Join-Path $PSScriptRoot 'setup_environment.ps1') -Platform douyin -ToolRoot $PSScriptRoot

function Get-StandardChromePaths {
    $paths=@('C:\Program Files\Google\Chrome\Application\chrome.exe','C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')
    if($env:LOCALAPPDATA) { $paths+=Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe' }
    foreach($path in $paths) { try { $local=Get-SetupPath $path; if(Test-Path -LiteralPath $local -PathType Leaf) { $local } } catch { continue } }
}
function Read-StandardChromeInstaller([string]$Path) {
    $path=Get-SetupPath $Path
    if((Get-Item -LiteralPath $path).Length -ne 13283768 -or (Get-SetupHash $path) -cne '020cb4676d0b770615002fc72f0d381542249c0c8a736da187dbfac4a9043180') { throw 'chrome_candidate_bytes_changed' }
    $signature=Get-AuthenticodeSignature -LiteralPath $path
    if($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)CN=Google LLC(?:,|$)') { throw 'chrome_signature_not_verified' }
    [pscustomobject][ordered]@{ path=$path; bytes=13283768; sha256=(Get-SetupHash $path); signature='Valid'; publisher='Google LLC'; signer_thumbprint=$signature.SignerCertificate.Thumbprint }
}
function New-StandardChromePlan([string]$Path) {
    $present=@(Get-StandardChromePaths)
    $installer=$null
    $state='chrome_reuse_standard_installation'
    if(-not $present.Count) {
        $state='chrome_installer_preparation_required'
        if($Path) { $installer=Read-StandardChromeInstaller $Path; $state='awaiting_separate_installation_and_persistent_change_approval' }
    }
    [pscustomobject][ordered]@{
        schema_version=1; planner_version='PAY-S3-06-candidate1'; state=$state; standard_chrome_paths=$present
        source_url='https://dl.google.com/chrome/install/latest/chrome_installer.exe'; installer=$installer
        installation_mode='interactive_official_online_installer'; arguments=@(); final_browser_download_required=$true
        possible_persistent_effects=@('Chrome installation registration','Google update components/settings','user or machine registry/installation paths')
        may_request_elevation=$true; user_only_installation_not_certified=$true; telemetry_defaults_not_certified=$true
        agent_may_execute_installer=$false; installation_performed=$false; configuration_changed=$false; export_started=$false
        next_step=$(if($present.Count) { '复用现有标准Chrome；原生环境及实际面板检查仍须通过，不下载或安装第二套。' } elseif($installer) { '已列具体安装器及影响，但本候选没有执行器。实际目标须确认安装位置/范围、网络和持久变更许可后，使用官方交互安装流程，不自动同意UAC/条款/默认浏览器/统计或登录。' } else { '先取得官方安装文件并核本候选字节/签名；动态文件更新时重新核源，不接受换文件沿用旧计划。' })
    }
}
if($MyInvocation.InvocationName -ne '.') {
    try {
        $r=New-StandardChromePlan $script:ChromeInstallerInput
        $json=$r | ConvertTo-Json -Depth 8
        if($script:ChromeOutput) { $out=New-SetupDirectory $script:ChromeOutput; Write-SetupNewFile (Join-Path $out 'chrome-plan.json') $json }
        $json
        if($r.state -eq 'chrome_reuse_standard_installation') { exit 0 }; exit 1
    } catch {
        [pscustomobject]@{state='chrome_plan_incomplete';error_code='chrome_source_signature_or_io_check_failed';error_type=$_.Exception.GetType().Name;installation_performed=$false;configuration_changed=$false;export_started=$false} | ConvertTo-Json
        exit 2
    }
}
