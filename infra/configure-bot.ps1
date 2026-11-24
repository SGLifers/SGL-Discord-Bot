param([Parameter(Mandatory)][string]$Profile,[string]$Region='us-west-2',[string]$StackName='sgl-hosting')
$ErrorActionPreference='Stop'
$output=& aws cloudformation describe-stacks --stack-name $StackName --profile $Profile --region $Region --query 'Stacks[0].Outputs' --output json --no-cli-pager
if($LASTEXITCODE -ne 0){throw 'Could not read the hosting stack.'}
# Windows PowerShell 5.1 emits a JSON array as one pipeline item. Parse it
# separately so the next pipeline enumerates the individual stack outputs.
$stackOutputs=$output | ConvertFrom-Json
$arn=($stackOutputs | Where-Object OutputKey -eq 'BotSecretArn').OutputValue
if(-not $arn){throw 'The stack has no bot secret output.'}
$secure=Read-Host 'Discord Bot token (not the OAuth client secret)' -AsSecureString
$pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$temp=Join-Path ([IO.Path]::GetTempPath()) ('SGL-bot-secret-'+[guid]::NewGuid().ToString('N')+'.json')
try {
    $token=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
    try {$bot=Invoke-RestMethod -Uri 'https://discord.com/api/v10/users/@me' -Headers @{Authorization=('Bot '+$token)}} catch {throw 'Discord rejected the bot token. No secret was stored.'}
    if(-not $bot.bot){throw 'This credential is not a Discord bot token.'}
    $acl=New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true,$false)
    $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($sid)
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')))
    New-Item -ItemType File -Path $temp | Out-Null
    Set-Acl -LiteralPath $temp -AclObject $acl
    [IO.File]::WriteAllText($temp,(@{token=$token}|ConvertTo-Json -Compress),(New-Object Text.UTF8Encoding($false)))
    & aws secretsmanager put-secret-value --secret-id $arn --secret-string ('file://'+$temp) --profile $Profile --region $Region --query VersionId --output text --no-cli-pager | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Could not store the bot token.'}
    Write-Output 'Bot token verified and stored. No Discord message was sent.'
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $token=$null
    if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force}
}
