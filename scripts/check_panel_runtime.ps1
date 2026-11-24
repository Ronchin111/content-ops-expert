#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('douyin','wechat','xhs')][string]$Platform,
    [Parameter(Mandatory=$true)][string]$ToolRoot,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [string]$ChromePath,
    [ValidateRange(1,65535)][int]$Port=8766,
    [ValidateRange(1,10)][int]$TimeoutSeconds=5
)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'
$script:PanelInputs=[pscustomobject]@{ platform=$Platform; tool_root=$ToolRoot; python_path=$PythonPath; chrome_path=$ChromePath; port=$Port; seconds=$TimeoutSeconds }
. (Join-Path $PSScriptRoot 'setup_environment.ps1') -Platform douyin -ToolRoot $PSScriptRoot

function Get-PanelRuntimeOwner([int]$SelectedPort) {
    $endpoints=@([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object Port -eq $SelectedPort)
    if (-not $endpoints.Count) { return [pscustomobject]@{ state='not_running'; owner=$null } }
    if (@($endpoints | Where-Object { $_.Address.ToString() -ne '127.0.0.1' }).Count) { return [pscustomobject]@{ state='listener_not_ipv4_loopback_only'; owner=$null } }
    $rows=@(Get-NetTCPConnection -LocalPort $SelectedPort -State Listen -ErrorAction Stop)
    $owners=@($rows.OwningProcess | Sort-Object -Unique)
    if ($owners.Count -ne 1 -or @($rows | Where-Object LocalAddress -ne '127.0.0.1').Count) { throw 'listener_owner_not_unique' }
    $processes=@(Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId='+[uint32]$owners[0]) -Property ProcessId,CreationDate,ExecutablePath -ErrorAction Stop)
    if ($processes.Count -ne 1 -or -not $processes[0].ExecutablePath -or -not $processes[0].CreationDate) { throw 'process_identity_unavailable' }
    # Never request/output command lines, environment, browser profiles or credentials.
    [pscustomobject]@{ state='identified'; owner=[pscustomobject][ordered]@{
        process_id=[uint32]$owners[0]; created_utc=$processes[0].CreationDate.ToUniversalTime().ToString('o')
        executable_path=(Get-SetupPath $processes[0].ExecutablePath)
    } }
}

function Read-PanelRuntimeHttp([int]$SelectedPort,[string]$Path,[int]$Seconds,[int]$Limit) {
    if ($Path -cnotin @('/','/api/env')) { throw 'unsupported_readonly_endpoint' }
    $handler=[Net.Http.HttpClientHandler]::new()
    $handler.UseProxy=$false; $handler.AllowAutoRedirect=$false; $handler.UseCookies=$false
    $client=[Net.Http.HttpClient]::new($handler)
    $cancel=[Threading.CancellationTokenSource]::new([TimeSpan]::FromSeconds($Seconds))
    $response=$null; $stream=$null; $buffer=[IO.MemoryStream]::new()
    try {
        $url='http://127.0.0.1:'+$SelectedPort+$Path
        $response=$client.GetAsync($url,[Net.Http.HttpCompletionOption]::ResponseHeadersRead,$cancel.Token).GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) { throw 'panel_http_status_unexpected' }
        if ($response.Content.Headers.ContentLength -and $response.Content.Headers.ContentLength -gt $Limit) { throw 'panel_http_response_limit' }
        $stream=$response.Content.ReadAsStreamAsync($cancel.Token).GetAwaiter().GetResult()
        $chunk=[byte[]]::new(8192)
        while ($true) {
            $count=$stream.ReadAsync($chunk,0,$chunk.Length,$cancel.Token).GetAwaiter().GetResult()
            if (-not $count) { break }
            if ($buffer.Length+$count -gt $Limit) { throw 'panel_http_response_limit' }
            $buffer.Write($chunk,0,$count)
        }
        return (New-Object Text.UTF8Encoding($false,$true)).GetString($buffer.ToArray())
    } finally {
        if ($stream) { $stream.Dispose() }; if ($response) { $response.Dispose() }
        $buffer.Dispose(); $cancel.Dispose(); $client.Dispose()
    }
}

function Invoke-PanelRuntimeCheck($Inputs) {
    if ($Inputs.platform -cnotin @('douyin','wechat','xhs')) { throw 'platform_invalid' }
    $root=Get-SetupPath $Inputs.tool_root; $python=Get-SetupPath $Inputs.python_path
    $chrome=if ($Inputs.chrome_path) { Get-SetupPath $Inputs.chrome_path } else { $null }
    $result=[pscustomobject][ordered]@{
        schema_version=1; checker_version='PAY-S3-05-candidate1'; state='panel_check_incomplete'
        port=$Inputs.port; selected_python=$python; owner=$null; panel_matches=$false
        interpreter_matches=$false; selected_interpreter_inspection=$null; panel_capabilities=$null
        service_started=$false; configuration_changed=$false; export_started=$false
        limitations=@('只核监听进程的exe路径及创建时间、页面和环境摘要；不是进程代码或模块搜索路径认证。','不证明登录、业务数据或导出可用；不启动或切换服务。')
    }
    $before=Get-PanelRuntimeOwner $Inputs.port
    if ($before.state -ne 'identified') {
        $result.state=if ($before.state -eq 'not_running') { 'panel_not_running' } else { 'panel_listener_unsupported' }
        return $result
    }
    $result.owner=$before.owner
    if ($before.owner.executable_path -ine $python) { $result.state='panel_interpreter_mismatch'; return $result }
    $htmlPath=Get-SetupPath (Join-Path $root 'panel.html')
    if ((Get-Item -LiteralPath $htmlPath).Length -gt 1048576) { throw 'local_panel_page_limit' }
    $local=[IO.File]::ReadAllText($htmlPath,[Text.Encoding]::UTF8).Replace("`r`n","`n")
    $remote=(Read-PanelRuntimeHttp $Inputs.port '/' $Inputs.seconds 1048576).Replace("`r`n","`n")
    if ($remote -cne $local) { $result.state='panel_page_mismatch'; return $result }
    $envResult=(Read-PanelRuntimeHttp $Inputs.port '/api/env' $Inputs.seconds 262144) | ConvertFrom-Json
    # Only use capability fields; no task/status/business endpoints are requested.
    foreach ($key in @('python','playwright','chrome','output')) {
        if (-not $envResult.PSObject.Properties[$key]) { throw 'panel_environment_invalid' }
    }
    if ($envResult.python -isnot [string]) { throw 'panel_environment_invalid' }
    foreach ($key in @('playwright','chrome','output')) { if ($envResult.$key -isnot [bool]) { throw 'panel_environment_invalid' } }
    $result.selected_interpreter_inspection=Invoke-EnvironmentCheck $Inputs.platform $root $python $chrome 10
    $after=Get-PanelRuntimeOwner $Inputs.port
    if ($after.state -ne 'identified' -or ($before.owner | ConvertTo-Json -Compress) -cne ($after.owner | ConvertTo-Json -Compress)) { $result.state='panel_owner_changed'; return $result }
    $result.panel_matches=$true; $result.interpreter_matches=$true
    $result.panel_capabilities=[pscustomobject]@{ python=$envResult.python; playwright=$envResult.playwright; chrome=$envResult.chrome; output=$envResult.output }
    $ready=$result.selected_interpreter_inspection.state -eq 'prerequisites_ready' -and $envResult.python -and $envResult.playwright -and $envResult.chrome -and $envResult.output
    $result.state=if ($ready) { 'panel_interpreter_and_prerequisites_match' } else { 'panel_interpreter_matches_environment_needs_attention' }
    return $result
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $script:PanelInputs.platform=$script:PanelInputs.platform.ToLowerInvariant()
        $r=Invoke-PanelRuntimeCheck $script:PanelInputs
        $r | ConvertTo-Json -Depth 14
        if ($r.state -eq 'panel_interpreter_and_prerequisites_match') { exit 0 }; exit 1
    } catch {
        [pscustomobject]@{ state='panel_check_incomplete'; error_code='panel_identity_http_or_access_check_failed'; error_type=$_.Exception.GetType().Name; service_started=$false; configuration_changed=$false; export_started=$false; next_step='核对本机监听、进程元数据权限及明确解释器；不查看命令行、不提权、不启动或重试导出。' } | ConvertTo-Json
        exit 2
    }
}
