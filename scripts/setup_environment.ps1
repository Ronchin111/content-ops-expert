#requires -Version 7.0
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Plan')][ValidateSet('douyin', 'wechat', 'xhs')][string]$Platform,
    [Parameter(Mandatory = $true, ParameterSetName = 'Plan')][string]$ToolRoot,
    [Parameter(ParameterSetName = 'Plan')][string]$PythonPath,
    [Parameter(ParameterSetName = 'Plan')][string]$ChromePath,
    [Parameter(ParameterSetName = 'Plan')][string]$EnvironmentDirectory,
    [Parameter(ParameterSetName = 'Plan')][string]$BundleManifest,
    [Parameter(ParameterSetName = 'Plan')][string]$OutputDirectory,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][switch]$Execute,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][string]$PlanPath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Execute')][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ApprovedPlanSha256
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:SetupSource = $PSCommandPath
$script:CheckerSource = Join-Path $PSScriptRoot 'check_environment.ps1'
$script:SetupMayHaveRun = $false
# Dot-sourcing the checker binds its own parameter names in this scope.
# Preserve the caller's selection before loading its unchanged functions.
$script:RequestedInputs = [pscustomobject][ordered]@{
    platform = $Platform; tool_root = $ToolRoot; python_path = $PythonPath
    chrome_path = $ChromePath; environment_directory = $EnvironmentDirectory; bundle_manifest = $BundleManifest
}
. $script:CheckerSource -Platform douyin -ToolRoot $PSScriptRoot

function Get-SetupPath([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]') { throw 'absolute_local_path_required' }
    if ($Value -match '[\x00-\x1f"]') { throw 'unsupported_path_characters' }
    $path = Get-EnvironmentLocalPath $Value
    if ($path.EndsWith('\')) { throw 'directory_root_or_trailing_separator_not_supported' }
    return $path
}

function Get-SetupHash([string]$Path) {
    (Get-FileHash -LiteralPath (Get-SetupPath $Path) -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-SetupNewFile([string]$Path, [string]$Content) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}

function New-SetupDirectory([string]$Path) {
    $path = Get-SetupPath $Path
    if (Test-Path -LiteralPath $path) { throw 'target_already_exists' }
    $parent = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'target_parent_missing' }
    # No reuse, cleanup, or automatic retry of partial targets.
    New-Item -ItemType Directory -Path $path -ErrorAction Stop | Out-Null
    return $path
}

function Invoke-SetupProcess([string]$Executable, [string[]]$Arguments, [int]$Seconds) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Get-SetupPath $Executable
    # All arguments are generated here, never read as command text from a plan.
    foreach ($arg in $Arguments) {
        if ($arg -match '[\x00-\x1f"]' -or $arg.EndsWith('\')) { throw 'unsafe_process_argument' }
    }
    $start.Arguments = (($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    # Child-process environment only. No persistent variable or pip.ini is changed.
    foreach ($key in @($start.EnvironmentVariables.Keys)) {
        if ([string]$key -like 'PIP_*') { $start.EnvironmentVariables.Remove([string]$key) }
    }
    # pip's documented os.devnull switch disables global/user/site config files,
    # including when --isolated is used (Windows os.devnull is exactly 'nul').
    $start.EnvironmentVariables['PIP_CONFIG_FILE'] = 'nul'
    # Installation runs only trusted Python/package code after specific approval.
    # Avoid console-codepage dependencies; probes emit ASCII JSON.
    $start.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'process_start_failed' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Seconds * 1000)) {
            $process.Kill($true)
            $null = $process.WaitForExit(1000)
            return [pscustomobject]@{ state = 'timeout'; exit_code = $null; output = $null }
        }
        if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) { throw 'process_output_incomplete' }
        # Do not publish arbitrary dependency output or stderr; report stage + code.
        $output = $null
        if ($process.ExitCode -eq 0 -and $stdout.Result.Length -le 32768) { $output = $stdout.Result }
        return [pscustomobject]@{ state = 'finished'; exit_code = $process.ExitCode; output = $output }
    } finally { $process.Dispose() }
}

function Get-SetupCapabilities([string]$Interpreter) {
    $code = 'import importlib.util,json;print(json.dumps({n:importlib.util.find_spec(n) is not None for n in ("venv","ensurepip")}))'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $result = Invoke-SetupProcess $Interpreter @('-I', '-B', '-c', ('import base64;exec(base64.b64decode(''' + $encoded + '''))')) 10
    if ($result.state -ne 'finished' -or $result.exit_code -ne 0 -or -not $result.output) { throw 'capability_probe_failed' }
    $data = $result.output | ConvertFrom-Json
    if ($data.venv -isnot [bool] -or $data.ensurepip -isnot [bool]) { throw 'capability_response_invalid' }
    return $data
}

function Get-SetupReleaseKey([string]$Value) {
    # PEP 440 numeric release subset, not an implementation of all PEP 440 forms.
    if ($Value -cnotmatch '\A(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*\z') { throw 'bundle_version_invalid' }
    $parts = @($Value.Split('.'))
    while ($parts.Count -gt 1 -and $parts[-1] -eq '0') { $parts = @($parts[0..($parts.Count - 2)]) }
    return ($parts -join '.')
}

function Compare-SetupRelease([string]$Left, [string]$Right) {
    $leftParts = @( (Get-SetupReleaseKey $Left).Split('.') )
    $rightParts = @( (Get-SetupReleaseKey $Right).Split('.') )
    for ($index = 0; $index -lt [Math]::Max($leftParts.Count, $rightParts.Count); $index++) {
        $leftPart = if ($index -lt $leftParts.Count) { $leftParts[$index] } else { '0' }
        $rightPart = if ($index -lt $rightParts.Count) { $rightParts[$index] } else { '0' }
        if ($leftPart.Length -lt $rightPart.Length) { return -1 }
        if ($leftPart.Length -gt $rightPart.Length) { return 1 }
        $comparison = [string]::CompareOrdinal($leftPart, $rightPart)
        if ($comparison -lt 0) { return -1 }
        if ($comparison -gt 0) { return 1 }
    }
    return 0
}

function Read-SetupBundle([string]$Manifest, [string[]]$Required) {
    $path = Get-SetupPath $Manifest
    if ((Get-Item -LiteralPath $path).Length -gt 262144) { throw 'bundle_manifest_too_large' }
    $data = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($data.schema_version -ne 1 -or @($data.wheels).Count -lt 1 -or @($data.wheels).Count -gt 100) { throw 'bundle_manifest_invalid' }
    $seen = @{}
    $seenFiles = @{}
    $items = @()
    foreach ($wheel in $data.wheels) {
        $name = [regex]::Replace(([string]$wheel.name).ToLowerInvariant(), '[-_.]+', '-')
        if ($name -notmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$' -or $seen.ContainsKey($name)) { throw 'bundle_package_name_invalid_or_duplicate' }
        $seen[$name] = $true
        $releaseKey = Get-SetupReleaseKey ([string]$wheel.version)
        $file = [string]$wheel.file
        if ($file -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.+-]*\.whl$') { throw 'bundle_filename_invalid' }
        # PowerShell hashtables compare strings case-insensitively, matching Windows.
        if ($seenFiles.ContainsKey($file)) { throw 'bundle_filename_duplicate' }
        $seenFiles[$file] = $true
        $fileParts = @($file.Substring(0, $file.Length - 4).Split('-'))
        if ($fileParts.Count -notin @(5, 6)) { throw 'bundle_filename_invalid' }
        $fileName = [regex]::Replace($fileParts[0].ToLowerInvariant(), '[-_.]+', '-')
        if ($fileName -cne $name) { throw 'bundle_filename_name_mismatch' }
        if ((Get-SetupReleaseKey $fileParts[1]) -cne $releaseKey) { throw 'bundle_filename_version_mismatch' }
        if ($fileParts.Count -eq 6 -and $fileParts[2] -cnotmatch '^[0-9][a-zA-Z0-9_]*$') { throw 'bundle_filename_invalid' }
        if ([string]$wheel.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'bundle_hash_invalid' }
        # A public URL records provenance for human review; it is not fetched or certified here.
        $source = [uri]([string]$wheel.source)
        if (-not $source.IsAbsoluteUri -or $source.Scheme -ne 'https' -or $source.Host -ne 'files.pythonhosted.org' -or $source.UserInfo -or $source.Query -or $source.Fragment) { throw 'bundle_public_source_required' }
        $filePath = Get-SetupPath (Join-Path (Split-Path -Parent $path) $file)
        if ((Get-SetupHash $filePath) -ne ([string]$wheel.sha256).ToLowerInvariant()) { throw 'bundle_hash_mismatch' }
        $items += [pscustomobject][ordered]@{ name = $name; version = [string]$wheel.version; path = $filePath; sha256 = Get-SetupHash $filePath; source = $source.AbsoluteUri }
    }
    foreach ($name in $Required) {
        if (-not $seen.ContainsKey($name)) { throw 'bundle_platform_package_missing' }
    }
    foreach ($row in $items) {
        if ($row.name -eq 'playwright' -and (Compare-SetupRelease $row.version '1.40.0') -lt 0) { throw 'bundle_version_too_old' }
        if ($row.name -eq 'openpyxl' -and (Compare-SetupRelease $row.version '3.1.0') -lt 0) { throw 'bundle_version_too_old' }
    }
    return $items
}

function New-SetupPlan($InputValues) {
    if ($InputValues.platform -cnotin @('douyin', 'wechat', 'xhs')) { throw 'platform_invalid' }
    $root = Get-SetupPath $InputValues.tool_root
    $python = if ($InputValues.python_path) { Get-SetupPath $InputValues.python_path } else { $null }
    $chrome = if ($InputValues.chrome_path) { Get-SetupPath $InputValues.chrome_path } else { $null }
    $report = Invoke-EnvironmentCheck $InputValues.platform $root $python $chrome 10
    $blockers = New-Object 'Collections.Generic.List[string]'
    $actions = @()
    $wheels = @()
    $target = $null
    $caps = $null
    $required = @('playwright', 'openpyxl')
    if ($InputValues.platform -eq 'wechat') { $required += 'httpx' }
    $unready = @($report.checks | Where-Object { $_.state -ne 'ready' })
    foreach ($row in $unready) {
        if ($row.name -notin $required) { $blockers.Add(($row.name + ':' + $row.state)) }
    }
    $missingPackages = @($unready | Where-Object { $_.name -in $required })
    $pythonReady = @($report.checks | Where-Object { $_.name -eq 'python' -and $_.state -eq 'ready' }).Count -eq 1
    if ($missingPackages.Count -and $pythonReady -and $blockers.Count -eq 0) {
        $caps = Get-SetupCapabilities $python
        if (-not $caps.venv -or -not $caps.ensurepip) { $blockers.Add('full_python_required:venv_or_ensurepip_missing') }
        if (-not $InputValues.environment_directory) { $blockers.Add('new_environment_directory_required') }
        else {
            $target = Get-SetupPath $InputValues.environment_directory
            if (Test-Path -LiteralPath $target) { $blockers.Add('target_already_exists') }
            if (-not (Test-Path -LiteralPath (Split-Path -Parent $target) -PathType Container)) { $blockers.Add('target_parent_missing') }
            # Do not write an environment inside the existing tool or interpreter tree.
            foreach ($protected in @($root, (Split-Path -Parent $python))) {
                if ($target -ieq $protected -or $target.StartsWith($protected + '\', [StringComparison]::OrdinalIgnoreCase)) { $blockers.Add('target_inside_existing_runtime_or_tool') }
            }
        }
        if (-not $InputValues.bundle_manifest) { $blockers.Add('reviewed_offline_wheel_bundle_required') }
        else { $wheels = @(Read-SetupBundle $InputValues.bundle_manifest $required) }
        if ($blockers.Count -eq 0) { $actions = @('create_new_venv', 'install_locked_local_wheels', 'check_dependency_consistency', 'recheck_new_interpreter') }
    }
    $state = if ($report.state -eq 'prerequisites_ready') { 'no_install_needed' } elseif ($blockers.Count) { 'preparation_required' } else { 'awaiting_specific_approval' }
    $commands = @()
    if ($actions.Count) {
        $plannedVenv = Join-Path $target 'venv'
        $commands = @(
            [pscustomobject]@{ executable = $python; arguments = @('-I', '-B', '-m', 'venv', $plannedVenv); timeout_seconds = 120 },
            [pscustomobject]@{ executable = (Join-Path $plannedVenv 'Scripts\python.exe'); arguments = @('-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-input', 'install', '--no-deps', '--no-index', '--no-cache-dir', '--only-binary=:all:', '--require-hashes', '--find-links', (Join-Path $target 'wheels'), '-r', (Join-Path $target 'requirements.lock')); timeout_seconds = 120 },
            [pscustomobject]@{ executable = (Join-Path $plannedVenv 'Scripts\python.exe'); arguments = @('-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check'); timeout_seconds = 30 }
        )
    }
    [pscustomobject][ordered]@{
        schema_version = 1; installer_version = 'PAY-S3-04-revision3'; state = $state
        input_values = $InputValues; inspection = $report; capabilities = $caps
        setup_sha256 = Get-SetupHash $script:SetupSource; checker_sha256 = Get-SetupHash $script:CheckerSource
        python_sha256 = $(if ($pythonReady) { Get-SetupHash $python } else { $null })
        target = $target; required_in_new_environment = $required; wheels = $wheels
        actions = $actions; planned_commands = $commands; blockers = @($blockers.ToArray()); network_downloads = @()
        child_pip_environment = 'remove PIP_* then set PIP_CONFIG_FILE=nul; no persistent changes'
        installation_performed = $false; export_started = $false; panel_interpreter_verified = $false
    }
}

function Invoke-SetupExecution([string]$SavedPlan, [string]$ApprovedHash) {
    $path = Get-SetupPath $SavedPlan
    if ((Get-SetupHash $path) -ne $ApprovedHash.ToLowerInvariant()) { throw 'approval_hash_mismatch' }
    if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw 'plan_too_large' }
    $planText = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $plan = $planText | ConvertFrom-Json
    if ($plan.schema_version -ne 1 -or $plan.installer_version -ne 'PAY-S3-04-revision3') { throw 'plan_version_invalid' }
    # Recompute every executable action from live input, not commands from JSON.
    # Any changed checker, Python executable, bundle, or readiness requires a new plan.
    $fresh = New-SetupPlan $plan.input_values
    if (($fresh | ConvertTo-Json -Depth 12) -cne $planText) { throw 'plan_stale_or_modified' }
    if ($fresh.state -ne 'awaiting_specific_approval') { throw 'plan_has_no_executable_installation' }
    $target = New-SetupDirectory $fresh.target
    $receipt = [pscustomobject][ordered]@{
        installer_version = 'PAY-S3-04-revision3'; state = 'installation_incomplete'; stage = 'prepare'
        approved_plan_sha256 = $ApprovedHash.ToLowerInvariant(); target = $target
        installation_performed = $false; export_started = $false; panel_interpreter_verified = $false
        process_state = $null; exit_code = $null; inspection = $null; error_code = $null
    }
    try {
        Write-SetupNewFile (Join-Path $target 'approved-plan.json') $planText
        $wheelDir = New-SetupDirectory (Join-Path $target 'wheels')
        $requirements = @()
        foreach ($row in $fresh.wheels) {
            $source = Get-SetupPath $row.path
            $destination = Join-Path $wheelDir ([IO.Path]::GetFileName($source))
            $sourceStream = [IO.File]::OpenRead($source)
            try {
                $destinationStream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew)
                try { $sourceStream.CopyTo($destinationStream) } finally { $destinationStream.Dispose() }
            } finally { $sourceStream.Dispose() }
            if ((Get-SetupHash $destination) -ne $row.sha256) { throw 'staged_wheel_hash_mismatch' }
            $requirements += ($row.name + '==' + $row.version + ' --hash=sha256:' + $row.sha256)
        }
        $lock = Join-Path $target 'requirements.lock'
        Write-SetupNewFile $lock ($requirements -join "`n")
        $venv = Join-Path $target 'venv'
        $receipt.stage = 'create_venv'
        $receipt.installation_performed = $true
        $script:SetupMayHaveRun = $true
        $step = Invoke-SetupProcess $fresh.input_values.python_path @('-I', '-B', '-m', 'venv', $venv) 120
        $receipt.process_state = $step.state; $receipt.exit_code = $step.exit_code
        if ($step.state -ne 'finished' -or $step.exit_code -ne 0) { throw 'venv_creation_failed' }
        $interpreter = Get-SetupPath (Join-Path $venv 'Scripts\python.exe')
        $receipt.stage = 'install_wheels'
        $step = Invoke-SetupProcess $interpreter @('-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-input', 'install', '--no-deps', '--no-index', '--no-cache-dir', '--only-binary=:all:', '--require-hashes', '--find-links', $wheelDir, '-r', $lock) 120
        $receipt.process_state = $step.state; $receipt.exit_code = $step.exit_code
        if ($step.state -ne 'finished' -or $step.exit_code -ne 0) { throw 'wheel_installation_failed' }
        $receipt.stage = 'check_dependencies'
        $step = Invoke-SetupProcess $interpreter @('-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check') 30
        $receipt.process_state = $step.state; $receipt.exit_code = $step.exit_code
        if ($step.state -ne 'finished' -or $step.exit_code -ne 0) { throw 'dependency_consistency_failed' }
        $receipt.stage = 'recheck'
        $receipt.inspection = Invoke-EnvironmentCheck $fresh.input_values.platform $fresh.input_values.tool_root $interpreter $fresh.input_values.chrome_path 10
        $receipt.state = if ($receipt.inspection.state -eq 'prerequisites_ready') { 'environment_ready' } else { 'installed_but_recheck_failed' }
    } catch {
        # Controlled codes only; never echo raw subprocess output or arbitrary exceptions.
        $known = @('staged_wheel_hash_mismatch', 'venv_creation_failed', 'wheel_installation_failed', 'dependency_consistency_failed')
        $receipt.error_code = if ($_.Exception.Message -in $known) { $_.Exception.Message } else { 'setup_stage_failed' }
    } finally {
        Write-SetupNewFile (Join-Path $target 'receipt.json') ($receipt | ConvertTo-Json -Depth 12)
    }
    return $receipt
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($Execute) {
            $result = Invoke-SetupExecution $PlanPath $ApprovedPlanSha256
            $result | ConvertTo-Json -Depth 12
            if ($result.state -eq 'environment_ready') { exit 0 }
            exit 1
        }
        $script:RequestedInputs.platform = $script:RequestedInputs.platform.ToLowerInvariant()
        $result = New-SetupPlan $script:RequestedInputs
        $json = $result | ConvertTo-Json -Depth 12
        if ($OutputDirectory) {
            $output = New-SetupDirectory $OutputDirectory
            Write-SetupNewFile (Join-Path $output 'plan.json') $json
        }
        $json
        if ($result.state -eq 'no_install_needed') { exit 0 }
        exit 1
    } catch {
        [pscustomobject]@{ schema_version = 1; state = 'setup_incomplete'; error_code = 'input_approval_or_io_check_failed'; error_type = $_.Exception.GetType().Name; installation_performed = $script:SetupMayHaveRun; export_started = $false; next_step = '核对参数、计划指纹、路径及权限；有bundle时另核包名/数字版本、wheel文件名唯一及名称版本对应、SHA256和来源。保留已有文件，新计划另存。执行若已创建目标，请读取目标内receipt.json；不自动重试或清理。' } | ConvertTo-Json
        exit 2
    }
}
