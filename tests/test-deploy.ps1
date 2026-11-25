$ErrorActionPreference='Stop'
$global:hostingMockCalls=New-Object System.Collections.Generic.List[object]
function global:aws {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $global:hostingMockCalls.Add($Arguments)
    $global:LASTEXITCODE=0
    if($Arguments[0] -eq 'sts'){return '{"Account":"633398616560"}'}
    if($Arguments[0] -eq 'cloudformation' -and $Arguments[1] -eq 'describe-stacks'){return '[{"OutputKey":"ApiUrl","OutputValue":"https://example.execute-api.us-west-2.amazonaws.com"}]'}
    return '{}'
}
$script=Join-Path (Split-Path $PSScriptRoot -Parent) 'infra/deploy.ps1'
$parameters=@{Profile='fixture';AccountId='633398616560';GuildId='123456789012345678';ChannelId='223456789012345678';AllowedHostIds='323456789012345678,423456789012345678';AlarmEmail='alarms@example.com'}
try {
    & $script @parameters -PlanOnly
    if($global:hostingMockCalls.Count -ne 1 -or $global:hostingMockCalls[0][0] -ne 'sts'){throw 'PlanOnly performed an AWS mutation.'}
    $withoutHosts=$parameters.Clone();$withoutHosts.Remove('AllowedHostIds')
    & $script @withoutHosts -PlanOnly
    $rejected=$false
    try { & $script @withoutHosts -EnableAnnouncements -PlanOnly } catch { $rejected=$true }
    if(-not $rejected){throw 'Enabled announcements without an authorization policy.'}
    $global:hostingMockCalls.Clear()
    & $script @parameters
    $deploy=@($global:hostingMockCalls | Where-Object {$_[0] -eq 'cloudformation' -and $_[1] -eq 'deploy'})
    if($deploy.Count -ne 1){throw 'Deployment command missing.'}
    if($deploy[0] -notcontains 'AllowedHostIds=323456789012345678,423456789012345678' -or $deploy[0] -notcontains 'HostRoleId=' -or $deploy[0] -notcontains 'AnnouncementsEnabled=false'){throw 'Parameter arguments did not preserve allowlists, empty roles or safe defaults.'}
    $roleParameters=$withoutHosts.Clone();$roleParameters.HostRoleId='523456789012345678,623456789012345678,723456789012345678'
    $global:hostingMockCalls.Clear()
    & $script @roleParameters -EnableAnnouncements -PlanOnly
    $prepared=Get-Content (Join-Path (Split-Path $PSScriptRoot -Parent) 'artifacts/parameters.json') -Raw | ConvertFrom-Json
    if(($prepared | Where-Object ParameterKey -eq 'HostRoleId').ParameterValue -ne $roleParameters.HostRoleId){throw 'Multiple host role IDs were not retained.'}
    Write-Output 'Passed deployment-script checks with mocked AWS only.'
}
finally {Remove-Item Function:\aws}
