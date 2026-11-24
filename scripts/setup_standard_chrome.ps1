#requires -Version 7.4
[CmdletBinding(DefaultParameterSetName='Plan')]
param(
    [Parameter(ParameterSetName='Plan')][string]$InstallerPath,
    [Parameter(ParameterSetName='Plan')][string]$ReceiptDirectory,
    [Parameter(ParameterSetName='Plan')][string]$OutputDirectory,
    [Parameter(Mandatory=$true,ParameterSetName='Execute')][switch]$Execute,
    [Parameter(Mandatory=$true,ParameterSetName='Execute')][string]$PlanPath,
    [Parameter(Mandatory=$true,ParameterSetName='Execute')][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ApprovedPlanSha256
)
Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'
$script:ChromeSource=$PSCommandPath
$script:ChromeInputs=[pscustomobject][ordered]@{installer_path=$InstallerPath;receipt_directory=$ReceiptDirectory}
$script:ChromeExecute=[bool]$Execute; $script:ChromePlan=$PlanPath; $script:ChromeApproval=$ApprovedPlanSha256; $script:ChromeOutput=$OutputDirectory
$script:ChromeAttempted=$false
. (Join-Path $PSScriptRoot 'setup_environment.ps1') -Platform douyin -ToolRoot $PSScriptRoot

function Get-ChromeMachine {
    $os=Get-CimInstance Win32_OperatingSystem -Property ProductType,BuildNumber -ErrorAction Stop
    [pscustomobject][ordered]@{windows_client=($os.ProductType -eq 1);build=[int]$os.BuildNumber;architecture=[Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString();process_x64=[Environment]::Is64BitProcess}
}
function Test-ChromeAdministrator {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try { return [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
}
function Get-ChromeStandardPaths {
    $paths=@('C:\Program Files\Google\Chrome\Application\chrome.exe','C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')
    if($env:LOCALAPPDATA){$paths+=Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'}
    foreach($path in $paths){$path=Get-SetupPath $path;if(Test-Path -LiteralPath $path -PathType Leaf){$path}}
}
function Get-ChromeResidualRoots {
    $paths=@('C:\Program Files\Google','C:\Program Files (x86)\Google')
    if($env:LOCALAPPDATA){$paths+=Join-Path $env:LOCALAPPDATA 'Google'}
    foreach($path in $paths){$path=Get-SetupPath $path;if(Test-Path -LiteralPath $path){$path}}
}
function Read-ChromeMsi([string]$Path) {
    $path=Get-SetupPath $Path
    if((Get-Item -LiteralPath $path).Length -ne 171970560 -or (Get-SetupHash $path) -cne '55edacd348b4c1c9b291dab19e4665929c7a127c013d9625970be924f893273f'){throw 'chrome_msi_bytes_changed'}
    $signature=Get-AuthenticodeSignature -LiteralPath $path
    if($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)CN=Google LLC(?:,|$)'){throw 'chrome_msi_signature_unverified'}
    [pscustomobject][ordered]@{path=$path;bytes=171970560;sha256=(Get-SetupHash $path);signature='Valid';publisher='Google LLC';version='157.0.8103.39';signer_thumbprint=$signature.SignerCertificate.Thumbprint}
}
function New-ChromeMsiPlan($Inputs) {
    $machine=Get-ChromeMachine
    $blockers=[Collections.Generic.List[string]]::new()
    if(-not $machine.windows_client -or $machine.build -lt 22000 -or $machine.architecture -cne 'X64' -or -not $machine.process_x64){$blockers.Add('windows11_x64_required')}
    $present=@(Get-ChromeStandardPaths)
    $msi=$null; $target=$null; $residual=@(); $admin=Test-ChromeAdministrator
    $engine=Get-SetupPath (Join-Path $env:SystemRoot 'System32\msiexec.exe')
    if(-not $present.Count){
        $residual=@(Get-ChromeResidualRoots)
        if($residual.Count){$blockers.Add('existing_google_tree_requires_manual_resolution')}
        if(-not $admin){$blockers.Add('existing_admin_token_required_no_auto_elevation')}
        if(-not $Inputs.installer_path){$blockers.Add('fixed_offline_msi_required')}else{$msi=Read-ChromeMsi $Inputs.installer_path}
        if(-not $Inputs.receipt_directory){$blockers.Add('new_receipt_directory_required')}else{
            $target=Get-SetupPath $Inputs.receipt_directory
            if(Test-Path -LiteralPath $target){$blockers.Add('receipt_target_exists')}
            if(-not(Test-Path -LiteralPath (Split-Path -Parent $target) -PathType Container)){$blockers.Add('receipt_parent_missing')}
            # Installer output must never be created inside a Chrome installation tree.
            foreach($root in @('C:\Program Files\Google','C:\Program Files (x86)\Google',(Join-Path $env:LOCALAPPDATA 'Google'))){
                if($target -ieq $root -or $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){$blockers.Add('receipt_inside_google_tree')}
            }
        }
    }
    $state=if($blockers.Count){'preparation_required'}elseif($present.Count){'reuse_standard_chrome'}else{'awaiting_machine_installation_approval'}
    [pscustomobject][ordered]@{
        schema_version=1;installer_version='PAY-S3-07-candidate1';state=$state;input_values=$Inputs;machine=$machine
        standard_paths=$present;residual_google_roots=$residual;existing_admin_token=$admin;installer=$msi;target=$target
        executor_sha256=(Get-SetupHash $script:ChromeSource);helper_sha256=(Get-SetupHash $script:SetupSource);checker_sha256=(Get-SetupHash $script:CheckerSource)
        msiexec_path=$engine;msiexec_sha256=(Get-SetupHash $engine)
        arguments=@(if($state -eq 'awaiting_machine_installation_approval'){'/i';$msi.path;'/qn';'/norestart';'REBOOT=ReallySuppress';'/L*V';(Join-Path $target 'chrome-msi.log')})
        possible_persistent_effects=@('machine-wide Chrome files and installation registration','Google update components and registry/settings')
        no_automatic_elevation=$true;no_automatic_restart=$true;network_downloads=@();installer_network_isolation_enforced=$false
        blockers=@($blockers.ToArray());installation_performed=$false;export_started=$false;panel_interpreter_verified=$false
    }
}
function Invoke-ChromeMsiProcess([string]$Engine,[string[]]$Arguments) {
    $start=[Diagnostics.ProcessStartInfo]::new()
    $start.FileName=$Engine;$start.UseShellExecute=$false;$start.CreateNoWindow=$true
    foreach($arg in $Arguments){$start.ArgumentList.Add($arg)}
    $process=[Diagnostics.Process]::new();$process.StartInfo=$start
    try{
        if(-not $process.Start()){throw 'chrome_process_start_failed'}
        if(-not $process.WaitForExit(240000)){return [pscustomobject]@{state='timeout';exit_code=$null;process_id=$process.Id;child_may_still_run=$true}}
        [pscustomobject]@{state='finished';exit_code=$process.ExitCode;process_id=$process.Id;child_may_still_run=$false}
    }finally{$process.Dispose()}
}
function Read-InstalledChrome {
    $path=Get-SetupPath 'C:\Program Files\Google\Chrome\Application\chrome.exe'
    $sig=Get-AuthenticodeSignature -LiteralPath $path
    $version=[Diagnostics.FileVersionInfo]::GetVersionInfo($path).ProductVersion
    if($sig.Status -ne 'Valid' -or -not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notmatch '(?:^|,\s*)CN=Google LLC(?:,|$)' -or $version -cne '157.0.8103.39'){throw 'installed_chrome_unverified'}
    [pscustomobject]@{path=$path;version=$version;signature='Valid';sha256=(Get-SetupHash $path)}
}
function Invoke-ChromeMsiExecution([string]$SavedPlan,[string]$ApprovedHash) {
    $path=Get-SetupPath $SavedPlan
    if((Get-Item -LiteralPath $path).Length -gt 1048576 -or (Get-SetupHash $path) -cne $ApprovedHash.ToLowerInvariant()){throw 'chrome_approval_mismatch'}
    $text=[IO.File]::ReadAllText($path,[Text.Encoding]::UTF8);$plan=$text|ConvertFrom-Json
    if($plan.schema_version -ne 1 -or $plan.installer_version -cne 'PAY-S3-07-candidate1'){throw 'chrome_plan_version_invalid'}
    $fresh=New-ChromeMsiPlan $plan.input_values
    if(($fresh|ConvertTo-Json -Depth 12) -cne $text){throw 'chrome_plan_stale_or_modified'}
    if($fresh.state -cne 'awaiting_machine_installation_approval'){throw 'chrome_installation_not_planned'}
    $target=New-SetupDirectory $fresh.target
    $receipt=[pscustomobject][ordered]@{installer_version='PAY-S3-07-candidate1';state='chrome_installation_incomplete';stage='prepare';approved_plan_sha256=$ApprovedHash.ToLowerInvariant();installation_performed=$false;process=$null;installed=$null;reboot_required=$false;error_code=$null;export_started=$false;panel_interpreter_verified=$false}
    $lock=$null
    try{
        Write-SetupNewFile (Join-Path $target 'approved-plan.json') $text
        # Hold the reviewed MSI read-only against replacement for the entire call.
        $lock=[IO.File]::Open($fresh.installer.path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        $null=Read-ChromeMsi $fresh.installer.path
        $receipt.stage='msiexec';$receipt.installation_performed=$true;$script:ChromeAttempted=$true
        $receipt.process=Invoke-ChromeMsiProcess $fresh.msiexec_path $fresh.arguments
        if($receipt.process.child_may_still_run){$receipt.state='chrome_installation_pending_no_retry';return $receipt}
        if($receipt.process.exit_code -in @(3010,1641)){$receipt.reboot_required=$true;$receipt.state='chrome_restart_reported_no_automatic_restart';return $receipt}
        if($receipt.process.exit_code -ne 0){throw 'chrome_msi_nonzero_exit'}
        $receipt.stage='verify_installed';$receipt.installed=Read-InstalledChrome
        $receipt.state='standard_chrome_ready'
    }catch{
        $receipt.error_code=if($_.Exception.Message -in @('chrome_msi_bytes_changed','chrome_msi_signature_unverified','chrome_process_start_failed','chrome_msi_nonzero_exit','installed_chrome_unverified')){$_.Exception.Message}else{'chrome_installation_stage_failed'}
    }finally{
        if($lock){$lock.Dispose()}
        Write-SetupNewFile (Join-Path $target 'receipt.json') ($receipt|ConvertTo-Json -Depth 12)
    }
    return $receipt
}
if($MyInvocation.InvocationName -ne '.'){
    try{
        if($script:ChromeExecute){$r=Invoke-ChromeMsiExecution $script:ChromePlan $script:ChromeApproval}else{
            $r=New-ChromeMsiPlan $script:ChromeInputs
            if($script:ChromeOutput){
                $outputPath=Get-SetupPath $script:ChromeOutput
                if($r.target -and ($outputPath -ieq $r.target -or $outputPath.StartsWith($r.target+'\',[StringComparison]::OrdinalIgnoreCase) -or $r.target.StartsWith($outputPath+'\',[StringComparison]::OrdinalIgnoreCase))){throw 'plan_output_overlaps_receipt_target'}
                $out=New-SetupDirectory $outputPath;Write-SetupNewFile (Join-Path $out 'chrome-plan.json') ($r|ConvertTo-Json -Depth 12)
            }
        }
        $r|ConvertTo-Json -Depth 12
        if($r.state -in @('reuse_standard_chrome','standard_chrome_ready')){exit 0};exit 1
    }catch{
        [pscustomobject]@{state='chrome_setup_incomplete';error_code='chrome_input_approval_or_io_check_failed';error_type=$_.Exception.GetType().Name;installation_performed=$script:ChromeAttempted;export_started=$false;retry=$false}|ConvertTo-Json
        exit 2
    }
}
