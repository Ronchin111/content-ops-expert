#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('douyin','wechat','xhs')][string]$Platform,
    [Parameter(Mandatory=$true)][string]$ToolRoot,
    [string]$PythonPath,
    [string]$PythonArchive,
    [string]$RuntimeDirectory,
    [string]$ChromeInstaller,
    [string]$ChromeReceiptDirectory,
    [string]$EnvironmentDirectory,
    [string]$BundleManifest,
    [string]$OutputDirectory
)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'
$script:S3Output=$OutputDirectory
# Each component loads in a child scope: existing dot-source parameter binding is isolated.
function New-S3Preparation($Inputs,[string]$PlanOutput) {
    $runtime=& {
        . (Join-Path $PSScriptRoot 'bootstrap_runtime.ps1') -Platform douyin -ToolRoot $PSScriptRoot
        New-BootstrapPlan ([pscustomobject][ordered]@{platform=$Inputs.platform;tool_root=$Inputs.tool_root;python_path=$Inputs.python_path;chrome_path=$null;archive_path=$Inputs.python_archive;runtime_directory=$Inputs.runtime_directory})
    }
    $chrome=& {
        . (Join-Path $PSScriptRoot 'setup_standard_chrome.ps1')
        New-ChromeMsiPlan ([pscustomobject][ordered]@{installer_path=$Inputs.chrome_installer;receipt_directory=$Inputs.chrome_receipt_directory})
    }
    & {
        . (Join-Path $PSScriptRoot 'setup_environment.ps1') -Platform douyin -ToolRoot $PSScriptRoot
        $blockers=[Collections.Generic.List[string]]::new()
        foreach($row in $runtime.blockers){$blockers.Add('runtime:'+ $row)}
        foreach($row in $chrome.blockers){$blockers.Add('chrome:'+ $row)}
        $required=@('playwright','openpyxl');if($Inputs.platform -eq 'wechat'){$required+='httpx'}
        $wheels=@();$bundle=$null;$environment=$null;$dependencyPlan=$null
        $packageChecks=@($runtime.inspection.checks|Where-Object {$_.name -in $required -and $_.state -ne 'ready'})
        $needDependencies=$runtime.runtime_needed -or $packageChecks.Count -gt 0
        if($needDependencies){
            if(-not $Inputs.bundle_manifest){$blockers.Add('locked_offline_bundle_required')}else{
                $bundle=Get-SetupPath $Inputs.bundle_manifest
                $wheels=@(Read-SetupBundle $bundle $required)
            }
            if(-not $Inputs.environment_directory){$blockers.Add('new_environment_directory_required')}else{
                $environment=Get-SetupPath $Inputs.environment_directory
                if(Test-Path -LiteralPath $environment){$blockers.Add('environment_target_exists')}
                if(-not(Test-Path -LiteralPath (Split-Path -Parent $environment) -PathType Container)){$blockers.Add('environment_parent_missing')}
                $protected=@((Get-SetupPath $Inputs.tool_root))
                if($Inputs.python_path){$protected+=Split-Path -Parent (Get-SetupPath $Inputs.python_path)}
                if($runtime.target){$protected+=$runtime.target}
                foreach($root in $protected){if($environment -ieq $root -or $environment.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){$blockers.Add('environment_inside_tool_or_runtime')}}
            }
            # The staged CPython-specific wheels cannot silently target another interpreter.
            if($wheels.Count -and -not $runtime.runtime_needed){
                $abi=Invoke-SetupProcess $Inputs.python_path @('-I','-B','-c','import json,sys,platform;print(json.dumps(dict(implementation=sys.implementation.name,major=sys.version_info.major,minor=sys.version_info.minor,machine=platform.machine())))') 10
                if($abi.state -ne 'finished' -or $abi.exit_code -ne 0 -or -not $abi.output){throw 'python_abi_probe_incomplete'}
                $info=$abi.output|ConvertFrom-Json
                foreach($wheel in $wheels){
                    if([IO.Path]::GetFileName($wheel.path) -match '-cp313-cp313-win_amd64\.whl$' -and ($info.implementation -cne 'cpython' -or $info.major -ne 3 -or $info.minor -ne 13 -or $info.machine -ine 'AMD64')){$blockers.Add('selected_python_incompatible_with_cp313_wheels');break}
                }
            }
        }
        if($runtime.state -eq 'runtime_not_needed' -and $chrome.state -eq 'reuse_standard_chrome'){
            $dependencyPlan=New-SetupPlan ([pscustomobject][ordered]@{platform=$Inputs.platform;tool_root=$Inputs.tool_root;python_path=$Inputs.python_path;chrome_path=$null;environment_directory=$Inputs.environment_directory;bundle_manifest=$Inputs.bundle_manifest})
            foreach($row in $dependencyPlan.blockers){$blockers.Add('dependencies:'+ $row)}
        }
        # Only distinct, non-nested write targets are usable within one reviewed scope.
        $targets=@(@($runtime.target,$chrome.target,$environment)|Where-Object {$_})
        if($PlanOutput){
            $outputPath=Get-SetupPath $PlanOutput
            foreach($targetPath in $targets){
                if($outputPath -ieq $targetPath -or $outputPath.StartsWith($targetPath+'\',[StringComparison]::OrdinalIgnoreCase) -or $targetPath.StartsWith($outputPath+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'plan_output_overlaps_installation_target'}
            }
        }
        for($i=0;$i -lt $targets.Count;$i++){for($j=$i+1;$j -lt $targets.Count;$j++){
            if($targets[$i] -ieq $targets[$j] -or $targets[$i].StartsWith($targets[$j]+'\',[StringComparison]::OrdinalIgnoreCase) -or $targets[$j].StartsWith($targets[$i]+'\',[StringComparison]::OrdinalIgnoreCase)){$blockers.Add('installation_targets_overlap')}
        }}
        $selected=if($needDependencies -and $environment){Join-Path $environment 'venv\Scripts\python.exe'}else{$Inputs.python_path}
        $needsInstallation=$runtime.runtime_needed -or $chrome.state -ne 'reuse_standard_chrome' -or $needDependencies
        [pscustomobject][ordered]@{
            schema_version=1;route_version='PAY-S3-08-candidate1'
            state=$(if($blockers.Count){'preparation_required'}elseif($needsInstallation){'installation_scope_ready_for_review'}else{'prerequisites_ready_panel_binding_pending'})
            input_values=$Inputs;runtime_plan=$runtime;chrome_plan=$chrome;dependency_plan=$dependencyPlan
            dependency_plan_generation_pending=($needDependencies -and $null -eq $dependencyPlan)
            locked_bundle_sha256=$(if($bundle){Get-SetupHash $bundle}else{$null});locked_wheels=$wheels
            expected_python=$selected;blockers=@($blockers.ToArray())
            ordered_stages=@('native_entry_if_powershell_missing','runtime_if_needed','standard_chrome_if_missing','fresh_dependency_plan_after_prerequisites','dependencies_if_needed','same_python_environment_recheck','panel_identity_check_only_after_separate_panel_start_authorization')
            execution_guide='references/s3-installation.md';automatic_batch_executor=$false;approval_granted=$false
            installation_performed=$false;panel_started=$false;panel_interpreter_verified=$false;export_started=$false
        }
    }
}
if($MyInvocation.InvocationName -ne '.'){
    try{
        $inputs=[pscustomobject][ordered]@{platform=$Platform.ToLowerInvariant();tool_root=$ToolRoot;python_path=$PythonPath;python_archive=$PythonArchive;runtime_directory=$RuntimeDirectory;chrome_installer=$ChromeInstaller;chrome_receipt_directory=$ChromeReceiptDirectory;environment_directory=$EnvironmentDirectory;bundle_manifest=$BundleManifest}
        $r=New-S3Preparation $inputs $script:S3Output
        if($script:S3Output){
            & {
                . (Join-Path $PSScriptRoot 'setup_environment.ps1') -Platform douyin -ToolRoot $PSScriptRoot
                $out=New-SetupDirectory $script:S3Output
                Write-SetupNewFile (Join-Path $out 's3-preparation.json') ($r|ConvertTo-Json -Depth 18)
                Write-SetupNewFile (Join-Path $out 'runtime-plan.json') ($r.runtime_plan|ConvertTo-Json -Depth 14)
                Write-SetupNewFile (Join-Path $out 'chrome-plan.json') ($r.chrome_plan|ConvertTo-Json -Depth 12)
                if($r.dependency_plan){Write-SetupNewFile (Join-Path $out 'dependency-plan.json') ($r.dependency_plan|ConvertTo-Json -Depth 12)}
            }
        }
        $r|ConvertTo-Json -Depth 18
        if($r.state -eq 'prerequisites_ready_panel_binding_pending'){exit 0};exit 1
    }catch{
        [pscustomobject]@{state='s3_preparation_incomplete';error_code='s3_component_input_or_io_check_failed';error_type=$_.Exception.GetType().Name;installation_performed=$false;panel_started=$false;export_started=$false}|ConvertTo-Json
        exit 2
    }
}
