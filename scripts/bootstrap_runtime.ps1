#requires -Version 7.4
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Plan')][ValidateSet('douyin','wechat','xhs')][string]$Platform,
    [Parameter(Mandatory = $true, ParameterSetName = 'Plan')][string]$ToolRoot,
    [Parameter(ParameterSetName = 'Plan')][string]$PythonPath,
    [Parameter(ParameterSetName = 'Plan')][string]$ChromePath,
    [Parameter(ParameterSetName = 'Plan')][string]$ArchivePath,
    [Parameter(ParameterSetName = 'Plan')][string]$RuntimeDirectory,
    [Parameter(ParameterSetName = 'Plan')][string]$OutputDirectory,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][switch]$Execute,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][string]$PlanPath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ApprovedPlanSha256
)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'
$script:BootstrapSource=$PSCommandPath
$script:BootstrapHelper=Join-Path $PSScriptRoot 'setup_environment.ps1'
$script:BootstrapInputs=[pscustomobject][ordered]@{
    platform=$Platform; tool_root=$ToolRoot; python_path=$PythonPath; chrome_path=$ChromePath
    archive_path=$ArchivePath; runtime_directory=$RuntimeDirectory
}
$script:BootstrapAttempted=$false
$script:BootstrapExecute=[bool]$Execute
$script:BootstrapPlanPath=$PlanPath
$script:BootstrapApprovedHash=$ApprovedPlanSha256
$script:BootstrapOutput=$OutputDirectory
. $script:BootstrapHelper -Platform douyin -ToolRoot $PSScriptRoot

function Get-BootstrapSource {
    [pscustomobject][ordered]@{
        version='3.13.17'; architecture='x64'; id='pythoncore-3.13-64'
        url='https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.zip'
        sha256='b26ca4b4ee51e157836049934d2b8eabd55939be296f3130254251b6df96ec2f'
        bytes=31162518; index_url='https://www.python.org/ftp/python/index-windows.json'
        index_sha256='80f82f87f0e42ed85097ed55879267b53d97e0f05ae6dbaf2ea40ee1bf7766b2'
    }
}

function Get-BootstrapRuntimeInfo([string]$Interpreter) {
    $code='import importlib.util,json,sys;print(json.dumps(dict(version=list(sys.version_info[:3]),venv=importlib.util.find_spec("venv") is not None,ensurepip=importlib.util.find_spec("ensurepip") is not None)))'
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $r=Invoke-SetupProcess $Interpreter @('-I','-B','-c',('import base64;exec(base64.b64decode('''+$encoded+'''))')) 10
    if ($r.state -ne 'finished' -or $r.exit_code -ne 0 -or -not $r.output) { throw 'runtime_probe_incomplete' }
    $info=$r.output | ConvertFrom-Json
    if (@($info.version).Count -ne 3 -or $info.venv -isnot [bool] -or $info.ensurepip -isnot [bool]) { throw 'runtime_probe_invalid' }
    foreach ($number in $info.version) { if (($number -isnot [int] -and $number -isnot [long]) -or $number -lt 0) { throw 'runtime_probe_invalid' } }
    return $info
}

function Get-BootstrapStandardChrome {
    # Mirror the existing launcher's supported paths, not arbitrary ChromePath.
    $candidates=@('C:\Program Files\Google\Chrome\Application\chrome.exe','C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')
    if ($env:LOCALAPPDATA) { $candidates+=Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe' }
    foreach ($candidate in $candidates) {
        try { $path=Get-SetupPath $candidate; if (Test-Path -LiteralPath $path -PathType Leaf) { $path } }
        catch { continue }
    }
}

function Test-BootstrapEntries($Zip) {
    $names=@{}; $files=@{}; $total=[long]0
    if ($Zip.Entries.Count -gt 10000 -or $Zip.Entries.Count -eq 0) { throw 'archive_entry_limit' }
    foreach ($entry in $Zip.Entries) {
        $name=$entry.FullName
        if ($name -match '[\\\x00-\x1f<>:"|?*]' -or $name.StartsWith('/')) { throw 'archive_path_invalid' }
        $directory=$name.EndsWith('/')
        $relative=$name.TrimEnd('/')
        if (-not $relative -or $names.ContainsKey($relative)) { throw 'archive_duplicate_or_empty_path' }
        $names[$relative]=$true
        $parts=@($relative.Split('/'))
        foreach ($part in $parts) {
            if (-not $part -or $part -in @('.','..') -or $part -match '[. ]$' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { throw 'archive_path_invalid' }
        }
        $unixType=($entry.ExternalAttributes -shr 16) -band 61440
        if ($unixType -eq 40960 -or ($entry.ExternalAttributes -band 1024)) { throw 'archive_links_not_supported' }
        if (-not $directory) { $files[$relative]=$true }
        $total+=$entry.Length
        if ($total -gt 536870912 -or $entry.Length -gt 134217728) { throw 'archive_expansion_limit' }
    }
    foreach ($name in $names.Keys) {
        $parts=@($name.Split('/'))
        for ($i=1; $i -lt $parts.Count; $i++) {
            if ($files.ContainsKey(($parts[0..($i-1)] -join '/'))) { throw 'archive_file_directory_collision' }
        }
    }
    foreach ($critical in @('python.exe','Lib/venv/__init__.py','Lib/ensurepip/__init__.py')) {
        if (-not $files.ContainsKey($critical)) { throw 'archive_runtime_files_missing' }
    }
    if (@($files.Keys | Where-Object { $_ -match '^Lib/ensurepip/_bundled/pip-[^/]+\.whl$' }).Count -ne 1) { throw 'archive_bundled_pip_missing' }
    [pscustomobject][ordered]@{ entries=$Zip.Entries.Count; expanded_bytes=$total; critical_files_present=$true }
}

function Read-BootstrapArchive([string]$Path, [string]$Destination) {
    $path=Get-SetupPath $Path
    $source=Get-BootstrapSource
    # A shared-read handle prevents replacement/write/delete while hashing and extracting.
    $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $zip=$null; $hasher=[Security.Cryptography.SHA256]::Create()
    try {
        if ($stream.Length -ne $source.bytes) { throw 'archive_size_mismatch' }
        $digest=([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-','').ToLowerInvariant()
        if ($digest -cne $source.sha256) { throw 'archive_official_hash_mismatch' }
        $stream.Position=0
        $zip=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read,$true)
        $info=Test-BootstrapEntries $zip
        if ($Destination) {
            $destination=Get-SetupPath $Destination
            if (-not (Test-Path -LiteralPath $destination -PathType Container)) { throw 'extraction_root_missing' }
            foreach ($entry in $zip.Entries) {
                $relative=$entry.FullName.TrimEnd('/').Replace('/','\')
                $target=Get-SetupPath (Join-Path $destination $relative)
                if (-not $target.StartsWith($destination+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'archive_path_escape' }
                $parent=if ($entry.FullName.EndsWith('/')) { $target } else { Split-Path -Parent $target }
                # Parent paths belong to this new target; path helper rejects reparse ancestors.
                $null=[IO.Directory]::CreateDirectory((Get-SetupPath $parent))
                if ($entry.FullName.EndsWith('/')) { continue }
                $reader=$entry.Open()
                try {
                    $writer=[IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
                    try { $reader.CopyTo($writer) } finally { $writer.Dispose() }
                } finally { $reader.Dispose() }
            }
        }
        return $info
    } finally {
        if ($zip) { $zip.Dispose() }
        $hasher.Dispose(); $stream.Dispose()
    }
}

function New-BootstrapPlan($Inputs) {
    if ($Inputs.platform -cnotin @('douyin','wechat','xhs')) { throw 'platform_invalid' }
    $root=Get-SetupPath $Inputs.tool_root
    $python=if ($Inputs.python_path) { Get-SetupPath $Inputs.python_path } else { $null }
    $chrome=if ($Inputs.chrome_path) { Get-SetupPath $Inputs.chrome_path } else { $null }
    $report=Invoke-EnvironmentCheck $Inputs.platform $root $python $chrome 10
    $blockers=New-Object 'Collections.Generic.List[string]'
    $pythonCheck=@($report.checks | Where-Object name -eq 'python')[0]
    $needRuntime=$false; $runtimeInfo=$null; $archiveInfo=$null; $target=$null; $archive=$null
    if ($pythonCheck.state -eq 'needs_confirmation') { $blockers.Add('confirm_existing_python_first') }
    elseif ($pythonCheck.state -eq 'missing') { $needRuntime=$true }
    else {
        if (-not $python) { throw 'explicit_python_required' }
        $runtimeInfo=Get-BootstrapRuntimeInfo $python
        $tooOld=$runtimeInfo.version[0] -lt 3 -or ($runtimeInfo.version[0] -eq 3 -and $runtimeInfo.version[1] -lt 10)
        $badPackages=@($report.checks | Where-Object { $_.name -in @('playwright','openpyxl','httpx') -and $_.state -ne 'ready' }).Count -gt 0
        $needRuntime=$tooOld -or ($badPackages -and (-not $runtimeInfo.venv -or -not $runtimeInfo.ensurepip))
        if ($pythonCheck.state -ne 'ready' -and -not $tooOld) { $blockers.Add('selected_python_check_incomplete') }
    }
    foreach ($row in @($report.checks | Where-Object { $_.name -in @('tool_files','maintenance') -and $_.state -ne 'ready' })) { $blockers.Add(($row.name+':'+$row.state)) }
    if ($needRuntime) {
        if (-not [Environment]::Is64BitOperatingSystem -or [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') { $blockers.Add('runtime_candidate_supports_windows_x64_only') }
        if (-not $Inputs.runtime_directory) { $blockers.Add('new_runtime_directory_required') }
        else {
            $target=Get-SetupPath $Inputs.runtime_directory
            if (Test-Path -LiteralPath $target) { $blockers.Add('target_already_exists') }
            if (-not (Test-Path -LiteralPath (Split-Path -Parent $target) -PathType Container)) { $blockers.Add('target_parent_missing') }
            $protected=@($root)
            if ($python -and $runtimeInfo) { $protected+=Split-Path -Parent $python }
            foreach ($folder in $protected) {
                $prefix=$folder.TrimEnd('\')+'\'
                if ($target -ieq $folder -or $target.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { $blockers.Add('target_inside_existing_tool_or_runtime') }
            }
        }
        if (-not $Inputs.archive_path) { $blockers.Add('official_archive_not_staged') }
        else { $archive=Get-SetupPath $Inputs.archive_path; $archiveInfo=Read-BootstrapArchive $archive $null }
    }
    $standardChrome=@(Get-BootstrapStandardChrome)
    $chromeReady=$standardChrome.Count -gt 0
    $state=if ($blockers.Count) { 'preparation_required' } elseif ($needRuntime) { 'awaiting_runtime_approval' } else { 'runtime_not_needed' }
    $actions=@()
    if ($state -eq 'awaiting_runtime_approval') { $actions=@('extract_official_archive_to_new_runtime','probe_new_runtime_standard_library') }
    [pscustomobject][ordered]@{
        schema_version=1; bootstrap_version='PAY-S3-05-candidate1'; state=$state; input_values=$Inputs
        inspection=$report; runtime_info=$runtimeInfo; runtime_needed=$needRuntime
        source=(Get-BootstrapSource); archive_path=$archive; archive_info=$archiveInfo; target=$target
        actions=$actions
        expected_python=$(if ($target) { Join-Path $target 'runtime\python.exe' } else { $python })
        bootstrap_sha256=(Get-SetupHash $script:BootstrapSource); helper_sha256=(Get-SetupHash $script:BootstrapHelper)
        checker_sha256=(Get-SetupHash $script:CheckerSource)
        existing_python_sha256=$(if ($python -and $runtimeInfo) { Get-SetupHash $python } else { $null })
        blockers=@($blockers.ToArray()); persistent_configuration_changes=@(); network_downloads=@()
        standard_chrome_paths=$standardChrome
        chrome_action=$(if ($chromeReady) { 'reuse_existing_standard_chrome' } else { 'separate_standard_chrome_plan_required_no_installation_here' })
        chrome_for_testing_supported_by_current_panel=$false
        installation_performed=$false; export_started=$false; panel_interpreter_verified=$false
    }
}

function Invoke-BootstrapExecution([string]$SavedPlan, [string]$ApprovedHash) {
    $path=Get-SetupPath $SavedPlan
    if ((Get-SetupHash $path) -cne $ApprovedHash.ToLowerInvariant()) { throw 'approval_hash_mismatch' }
    if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw 'plan_too_large' }
    $text=[IO.File]::ReadAllText($path,[Text.Encoding]::UTF8); $plan=$text | ConvertFrom-Json
    if ($plan.schema_version -ne 1 -or $plan.bootstrap_version -cne 'PAY-S3-05-candidate1') { throw 'plan_version_invalid' }
    $fresh=New-BootstrapPlan $plan.input_values
    if (($fresh | ConvertTo-Json -Depth 14) -cne $text) { throw 'plan_stale_or_modified' }
    if ($fresh.state -cne 'awaiting_runtime_approval') { throw 'runtime_install_not_planned' }
    $target=New-SetupDirectory $fresh.target
    $receipt=[pscustomobject][ordered]@{
        bootstrap_version='PAY-S3-05-candidate1'; state='runtime_install_incomplete'; stage='prepare'
        approved_plan_sha256=$ApprovedHash.ToLowerInvariant(); target=$target; expected_python=$fresh.expected_python
        installation_performed=$false; new_runtime_executed=$false; runtime_info=$null; archive_info=$null
        error_code=$null; export_started=$false; panel_interpreter_verified=$false; dependency_installation_performed=$false
    }
    try {
        Write-SetupNewFile (Join-Path $target 'approved-plan.json') $text
        $receipt.stage='extract_runtime'; $receipt.installation_performed=$true; $script:BootstrapAttempted=$true
        $runtime=New-SetupDirectory (Join-Path $target 'runtime')
        $receipt.archive_info=Read-BootstrapArchive $fresh.archive_path $runtime
        $receipt.stage='probe_new_runtime'; $receipt.new_runtime_executed=$true
        $receipt.runtime_info=Get-BootstrapRuntimeInfo $fresh.expected_python
        $v=$receipt.runtime_info.version
        if (($v -join '.') -cne $fresh.source.version -or -not $receipt.runtime_info.venv -or -not $receipt.runtime_info.ensurepip) { throw 'new_runtime_check_failed' }
        $receipt.state='runtime_ready_dependencies_pending'
    } catch {
        $receipt.error_code='runtime_stage_failed'
    } finally { Write-SetupNewFile (Join-Path $target 'receipt.json') ($receipt | ConvertTo-Json -Depth 14) }
    return $receipt
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($script:BootstrapExecute) {
            $r=Invoke-BootstrapExecution $script:BootstrapPlanPath $script:BootstrapApprovedHash
            $r | ConvertTo-Json -Depth 14
            if ($r.state -eq 'runtime_ready_dependencies_pending') { exit 0 }; exit 1
        }
        $script:BootstrapInputs.platform=$script:BootstrapInputs.platform.ToLowerInvariant()
        $r=New-BootstrapPlan $script:BootstrapInputs
        $json=$r | ConvertTo-Json -Depth 14
        if ($script:BootstrapOutput) { $out=New-SetupDirectory $script:BootstrapOutput; Write-SetupNewFile (Join-Path $out 'runtime-plan.json') $json }
        $json
        if ($r.state -eq 'runtime_not_needed') { exit 0 }; exit 1
    } catch {
        [pscustomobject]@{ state='runtime_preparation_incomplete'; error_code='runtime_input_approval_or_io_check_failed'; error_type=$_.Exception.GetType().Name; installation_performed=$script:BootstrapAttempted; export_started=$false; next_step='核对现场检查、官方ZIP及新目录、计划hash和权限；若目标已创建读receipt.json。保留失败文件，不自动重试。' } | ConvertTo-Json
        exit 2
    }
}
