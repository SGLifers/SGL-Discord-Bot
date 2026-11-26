param(
    [Parameter(Mandatory)][string]$Profile,
    [Parameter(Mandatory)][ValidatePattern('^\d{12}$')][string]$AccountId,
    [string]$Region='us-west-2',
    [Parameter(Mandatory)][ValidatePattern('^\d{17,20}$')][string]$GuildId,
    [Parameter(Mandatory)][ValidatePattern('^\d{17,20}$')][string]$ChannelId,
    [ValidatePattern('^(\d{17,20}(,\d{17,20})*)?$')][string]$HostRoleId='',
    [ValidatePattern('^(\d{17,20}(,\d{17,20})*)?$')][string]$AllowedHostIds='',
    [Parameter(Mandatory)][string]$AlarmEmail,
    [string]$StackName='sgl-hosting',
    [string]$StatsApiFunctionArn='',
    [switch]$EnableAnnouncements,
    [switch]$PlanOnly
)
$ErrorActionPreference='Stop'
if($EnableAnnouncements -and -not $HostRoleId -and -not $AllowedHostIds){throw 'Specify a host role or an explicit host allowlist before enabling announcements.'}
if($AlarmEmail -notmatch '^[^\s@]+@[^\s@]+\.[^\s@]+$'){throw 'Specify an alarm email address.'}
function Invoke-HostingAws([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments) {
    $result=& aws @Arguments --profile $Profile --region $Region --no-cli-pager
    if($LASTEXITCODE -ne 0){throw 'AWS operation failed; deployment stopped.'}
    return $result
}
$caller=Invoke-HostingAws @('sts','get-caller-identity','--output','json') | ConvertFrom-Json
if($caller.Account -ne $AccountId){throw 'AWS profile is for a different account.'}
$root=Split-Path $PSScriptRoot -Parent
$artifact=Join-Path $root 'artifacts'
New-Item -ItemType Directory -Force -Path $artifact | Out-Null
$zip=Join-Path $artifact 'hosting.zip'
Compress-Archive -Path (Join-Path $root 'backend/handler.py'),(Join-Path $root 'backend/service.py'),(Join-Path $root 'backend/stats_adapter.py') -DestinationPath $zip -Force
$hash=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$bucket="sgl-hosting-artifacts-$AccountId-$Region"
$key="hosting/$hash.zip"
$parameters=@(
    @{ParameterKey='CodeBucket';ParameterValue=$bucket}, @{ParameterKey='CodeKey';ParameterValue=$key},
    @{ParameterKey='GuildId';ParameterValue=$GuildId}, @{ParameterKey='ChannelId';ParameterValue=$ChannelId},
    @{ParameterKey='HostRoleId';ParameterValue=$HostRoleId}, @{ParameterKey='AllowedHostIds';ParameterValue=$AllowedHostIds},
    @{ParameterKey='AlarmEmail';ParameterValue=$AlarmEmail}, @{ParameterKey='AnnouncementsEnabled';ParameterValue=([bool]$EnableAnnouncements).ToString().ToLowerInvariant()}
)
# CloudFormation deploy preserves an existing parameter when no override is supplied.
if($PSBoundParameters.ContainsKey('StatsApiFunctionArn')){$parameters+=@{ParameterKey='StatsApiFunctionArn';ParameterValue=$StatsApiFunctionArn}}
$parameterFile=Join-Path $artifact 'parameters.json'
$parameters | ConvertTo-Json | Set-Content -LiteralPath $parameterFile -Encoding utf8
if($PlanOnly){Write-Output "Prepared package and parameters for $AccountId in $Region. No AWS changes were made.";return}
# Only create an absent bucket; never adopt a bucket belonging to another account.
$head=& aws s3api head-bucket --bucket $bucket --expected-bucket-owner $AccountId --profile $Profile --region $Region --no-cli-pager 2>&1
if($LASTEXITCODE -ne 0){
    if(($head -join ' ') -notmatch '404|Not Found|NoSuchBucket'){throw 'Artifact bucket ownership or permissions could not be verified.'}
    $arguments=@('s3api','create-bucket','--bucket',$bucket)
    if($Region -ne 'us-east-1'){$arguments+=@('--create-bucket-configuration',"LocationConstraint=$Region")}
    Invoke-HostingAws $arguments | Out-Null
}
Invoke-HostingAws @('s3api','put-public-access-block','--bucket',$bucket,'--public-access-block-configuration','BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true') | Out-Null
Invoke-HostingAws @('s3api','put-bucket-versioning','--bucket',$bucket,'--versioning-configuration','Status=Enabled') | Out-Null
Invoke-HostingAws @('s3api','put-object','--bucket',$bucket,'--key',$key,'--body',$zip,'--server-side-encryption','AES256') | Out-Null
# Every override is a separate native argument; no shell command is constructed.
$overrides=@($parameters | ForEach-Object { $_.ParameterKey+'='+$_.ParameterValue })
Invoke-HostingAws (@('cloudformation','deploy','--stack-name',$StackName,'--template-file',(Join-Path $PSScriptRoot 'template.yaml'),'--parameter-overrides')+$overrides+@('--capabilities','CAPABILITY_IAM','--no-fail-on-empty-changeset')) | Out-Null
Invoke-HostingAws @('cloudformation','describe-stacks','--stack-name',$StackName,'--query','Stacks[0].Outputs','--output','json') | Set-Content -LiteralPath (Join-Path $artifact 'outputs.json') -Encoding utf8
Write-Output 'Deployment complete. Configure the bot token locally, confirm the alarm subscription, then enable announcements and connect SGL to ApiUrl.'
