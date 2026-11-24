$ErrorActionPreference='Stop'
$global:configureMockJson=''
$global:configureMockExitCode=0
$global:configurePromptReached=$false
$global:configureAwsCalls=0
function global:aws {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $global:configureAwsCalls++
    if($Arguments[0] -ne 'cloudformation' -or $Arguments[1] -ne 'describe-stacks'){
        throw 'Unexpected AWS call: token setup tests must be read-only.'
    }
    $global:LASTEXITCODE=$global:configureMockExitCode
    return ($global:configureMockJson -split "`n")
}
function global:Read-Host {
    param([string]$Prompt,[switch]$AsSecureString)
    if(-not $AsSecureString){throw 'Token prompt is not private.'}
    $global:configurePromptReached=$true
    throw 'TEST_TOKEN_PROMPT_REACHED'
}
$script=Join-Path (Split-Path $PSScriptRoot -Parent) 'infra/configure-bot.ps1'
$fixtures=@(
    @{Json="[`n{`"OutputKey`":`"ApiUrl`",`"OutputValue`":`"https://example.invalid`"},`n{`"OutputKey`":`"BotSecretArn`",`"OutputValue`":`"arn:aws:secretsmanager:us-west-2:123456789012:secret:test`"}`n]";ExitCode=0;Expected='TEST_TOKEN_PROMPT_REACHED';Prompt=$true},
    @{Json='[{"OutputKey":"BotSecretArn","OutputValue":"arn:aws:secretsmanager:us-west-2:123456789012:secret:test"}]';ExitCode=0;Expected='TEST_TOKEN_PROMPT_REACHED';Prompt=$true},
    @{Json='[{"OutputKey":"ApiUrl","OutputValue":"https://example.invalid"}]';ExitCode=0;Expected='The stack has no bot secret output.';Prompt=$false},
    @{Json='[]';ExitCode=0;Expected='The stack has no bot secret output.';Prompt=$false},
    @{Json='';ExitCode=1;Expected='Could not read the hosting stack.';Prompt=$false}
)
try {
    foreach($fixture in $fixtures){
        $global:configureMockJson=$fixture.Json
        $global:configureMockExitCode=$fixture.ExitCode
        $global:configurePromptReached=$false
        $global:configureAwsCalls=0
        $failure=''
        try { & $script -Profile fixture } catch { $failure=$_.Exception.Message }
        if($failure -ne $fixture.Expected){throw "Unexpected setup result: $failure"}
        if($global:configurePromptReached -ne $fixture.Prompt){throw 'Unexpected token prompt state.'}
        if($global:configureAwsCalls -ne 1){throw 'Expected one read-only AWS call.'}
    }
    Write-Output 'Passed bot-configuration checks with mocked AWS and no token entry.'
}
finally {
    Remove-Item Function:\aws
    Remove-Item Function:\Read-Host
}
