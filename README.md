# SGL Discord Bot

Posts a verified host's Splitgate lobby code to one configured Discord server channel. SGL's Hosting tab sends a code when the host clicks **Open Lobby**. **Close Lobby** edits that same announcement to mark it closed. Typing never sends a message. The service posts one compact **green Lobby open embed**, with **Name**, **Code**, **Host**, optional **Gamemode**, and optional **Map**, on consecutive lines without blank lines. Closing updates that same card to **red Lobby closed**; it does not add a second message. Existing plain-text announcements are converted to a red embed when closed. Status titles accompany the colors so they remain readable without color alone.

This is an on-demand HTTP service using Discord's REST API, deployed as AWS Lambda + HTTP API + DynamoDB. It needs no always-running bot process, WebSocket/Gateway connection, EC2 instance or frontend. The desktop spinner works offline and is independent of this service.

## Status

Ready for deployment; no cloud resources or bot token are provisioned by cloning this repository. Announcements default to **disabled**. The SGL desktop endpoint is intentionally empty until deployment and connection are completed. Tests use fake HTTP/AWS and never post to Discord.

## Discord setup

1. Use the existing SGL application in the [Discord Developer Portal](https://discord.com/developers/applications), or create a separate bot application.
2. In **Bot**, generate/reset its **bot token**. This is different from the OAuth client secret used for SGL sign-in. Keep it local and use the secret-configuration script below; never paste it into chat, GitHub or SGL.
3. Under **OAuth2 → URL Generator**, select the **bot** scope and **View Channels / Send Messages / Embed Links** permissions. Install it in the community server. Do not grant Administrator.
4. Enable Discord Developer Mode and copy the server ID, destination text-channel ID, and either a host role ID or the Discord IDs of permitted hosts. The bot must be able to view/send/embed in the destination channel, including channel-specific overrides.
5. Each host signs in through SGL's existing Discord login. The service verifies that session through the configured SGL identity service, checks server membership and checks the allowed-host list/role. Client-supplied host identities, user IDs and destination channels are rejected. An optional lobby name is treated as plain text.

No Gateway events are consumed, so Message Content intent is not used. The service checks individual members through the REST API. It does not read channel messages or automatically crosspost to announcement-channel followers.

## Deploy later

Requirements: AWS CLI v2, PowerShell, a refreshed AWS profile, and permissions for CloudFormation, IAM, Lambda, API Gateway, DynamoDB, Secrets Manager, S3, CloudWatch and SNS. The script verifies the AWS account before changing resources. The default region is us-west-2. A monitored alarm email is required. Disabled initial deployments can omit the host policy; either allowed hosts or a host role must be configured before enabling announcements.

```powershell
./infra/deploy.ps1 -Profile YOUR_PROFILE -AccountId YOUR_ACCOUNT_ID -GuildId YOUR_SERVER_ID -ChannelId YOUR_CHANNEL_ID -HostRoleId YOUR_HOST_ROLE_ID -AlarmEmail YOU@example.com -PlanOnly
./infra/deploy.ps1 -Profile YOUR_PROFILE -AccountId YOUR_ACCOUNT_ID -GuildId YOUR_SERVER_ID -ChannelId YOUR_CHANNEL_ID -HostRoleId YOUR_HOST_ROLE_ID -AlarmEmail YOU@example.com
./infra/configure-bot.ps1 -Profile YOUR_PROFILE
```

Use `-HostRoleId 'FIRST_ROLE_ID,SECOND_ROLE_ID'` to allow members with any listed role. Use `-AllowedHostIds 'FIRST_DISCORD_ID,SECOND_DISCORD_ID'` for individual users instead, or supply both (either grants permission). The example IDs are placeholders. The deployment package and outputs are written beneath ignored `artifacts/`. No token is passed on a command line or through CloudFormation parameters. `configure-bot.ps1` privately prompts for it, checks that it belongs to a bot, then stores it in the stack's Secrets Manager secret through a temporary file restricted to the current Windows user. It sends no messages.

Confirm the SNS alarm subscription email. Re-run the same deploy command with **`-EnableAnnouncements`** after configuring the token/channel/hosts. Omitting that switch disables announcements on redeployment. To pause announcements, redeploy without it; deployment and in-flight requests take time to finish. If the identity service differs from SGL's standard sharing service, update the `SglIdentityUrl` stack parameter deliberately; never take an identity URL from an announcement request.

The stack uses shared Lambda concurrency (no reserved slots), encrypted on-demand DynamoDB with recovery enabled, retained bot/state resources, fourteen-day logs, API throttling at one request/second with burst two, and Lambda-error/API-server-error alarms. The default limits are one new lobby per host per minute and thirty per day. Throttling is not a hard cost cap. This stack is separate from SGL sharing, and the sharing service's budget cutoff does **not** pause it. Use your AWS account budget alerts and the explicit disable control. Retained table/secret/artifact-bucket resources can continue to incur costs after stack deletion.

## Connect SGL after deployment

Use the stack's `ApiUrl` output in the [SGL repository](https://github.com/SGLifers/SGL):

```powershell
./scripts/configure-hosting.ps1 -ApiUrl https://YOUR_API.execute-api.us-west-2.amazonaws.com
```

The script checks the service's health identity and embeds only its public endpoint. Rebuild and publish SGL to connect hosts. No bot token, AWS credentials or per-user shared API key belongs in the app. Optionally enter a lobby name, enter a six-character code in Hosting, then choose **Open Lobby**. Choose **Close Lobby** to mark the original announcement closed; no second message is posted. These actions affect Discord announcements, not the in-game lobby. SGL does not replay codes at startup, on sign-in or after an account switch.

## API contract

`GET /health` returns the service identity, enabled state and `lobbyActions: ["open", "close", "update"]`. It is not a delivery test.

`POST /announcements` requires `Authorization: Bearer <SGL session>` and `Content-Type: application/json`:

```json
{"code":"ABC123","lobbyName":"Friday games","action":"open","requestId":"32-lowercase-hex-characters"}
```

The request ID must be a real 32-character lowercase hexadecimal identity generated by SGL. Codes are normalized by the app; the service accepts exactly six uppercase letters or numbers. The optional `lobbyName` is limited to 80 UTF-16 characters on one line, is escaped for Discord formatting and cannot enable mention notifications. The response is `{"status":"sent","messageId":"..."}` or `{"status":"duplicate","messageId":"..."}`. Use `action: "close"` with the same code and a fresh request ID to close your own announcement. Omitting `action` retains the legacy open behavior. Close uses the stored message ID and original name, never a client-supplied message ID. Open responses also return an `announcementId` bound to that receipt. Optional `map` and `gamemode` are plain-text single-line names up to 120 UTF-16 characters; omitting it leaves the Gamemode line out of the embed. `action: "update"` requires that `announcementId` and a fresh `requestId`, and patches the open card with the supplied gamemode and optional map; an empty value removes its line. Omitting map from an update preserves the currently confirmed map for compatibility with older clients. Map is part of request-identity binding, and closing keeps the last confirmed map. Updates return `status: "updated"` (or `"duplicate"` on a completed retry). Closing keeps the last confirmed gamemode. Old announcement identities cannot update a closed or reopened lobby, even when its code is reused. Gamemode updates do not consume the new-lobby allowance and are limited to 256 distinct update requests per host/destination within the receipt-retention period. Successful responses also include `lobbyStatus: "open"` or `"closed"`; close returns `status: "closed"`. A missing recent announcement returns 404. No arbitrary message, host identity, destination or URL is accepted. HTTP errors return a fixed `error`, with bounded `retryAfter` seconds where retry is safe. Statuses include 400 malformed input, 401 expired/missing session, 403 unauthorized host, 409 uncertain/conflicting delivery, 429 cooldown/daily limit, and 503 disabled/unavailable service.

Durable state is scoped to the server, channel and verified host, with optimistic DynamoDB writes serializing concurrent requests. Moving from a test destination to a real one uses separate receipts and nonces. An open host/code in one destination is deduplicated for twenty-four hours, even when the lobby name changes. Closing clears any previous plain-text content and replaces the original bot message with a red closed embed using PATCH, suppressing mentions, and does not consume the daily opening limit or require waiting for the opening cooldown. A deleted announcement is treated as already closed. Reopening a closed code requires a new open request ID and respects the normal opening limits. Close IDs are durably bound to their original announcement, so an old close retry cannot close a newer reopening within the receipt-retention period. Close retries repeat the same idempotent PATCH; no extra message is created. The first delivery keeps its original name; name changes apply to the next lobby. Embed generation is deterministic so retries keep the same card and nonce. Daily counts and cooldowns apply to new codes. Discord requests include a stable nonce with `enforce_nonce`, and all mention notifications are suppressed. A timeout or database failure after Discord accepted the message can be retried with the same nonce only within a bounded two-minute window. After that it requires channel review rather than blindly creating another message. This reduces duplicates; it does not claim globally exactly-once delivery across all Discord/network failures. SGL sends only on button clicks and retries only when the host clicks again. Signing out cancels pending client work but cannot retract a message already received by the service. After restarting SGL, entering the code and choosing Close Lobby can close a recent announcement belonging to the signed-in host.

Tokens, lobby request bodies and upstream response bodies are never logged. Persistent state contains the verified Discord host ID, code, lobby name, delivery status, nonce/request identity, message receipt and rate-limit timestamps; DynamoDB TTL expires inactive host records after two days. Delivered code receipts are kept for one day. API access logs include only request identity and status. Sign-out affects future verification through SGL's identity endpoint.

## Tests

```powershell
$env:PYTHONPATH='backend'
python -m unittest discover -s tests -v
./tests/test-deploy.ps1
cfn-lint -r us-west-2 -t infra/template.yaml
```

Python runtime dependencies are supplied by Lambda (boto3); the service otherwise uses the standard library. `cfn-lint>=1.40,<2` is a development dependency. GitHub Actions runs service tests, mocked deployment checks and template validation. Deploying and an explicitly authorized real-channel pilot remain separate operator steps.

References: [Discord Edit Message](https://docs.discord.com/developers/resources/message#edit-message), [Discord Create Message and nonce behavior](https://docs.discord.com/developers/resources/message#create-message), [allowed mentions](https://docs.discord.com/developers/resources/message#allowed-mentions-object), [individual guild members](https://docs.discord.com/developers/resources/guild#get-guild-member), [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits).

## Statistics commands

The optional `StatsApiFunctionArn` deployment parameter connects `POST /stats/interactions` to the SplitStatsInfra API Lambda. The adapter forwards the exact original signed body/headers; SplitStatsInfra verifies Discord's Ed25519 signature, guild, administrator roles and replay identity. This leaves `/announcements` and its request contract unchanged. Do not configure a second competing interaction URL for the same Discord application.

After deploying both services on the operator machine, configure the application's interaction URL as this bot API's `/stats/interactions`, and configure the application's public key and authorized admin roles in SplitStatsInfra. Register just the guild `/stats` command with `scripts/register-stats-commands.py` using the existing bot secret ARN, profile and verified account. `--plan-only` prints its definition without credentials/network. Command registration preserves unrelated commands and sends no channel messages. In Discord server Integrations, permit the configured stats-admin roles to use the command; commands default to disabled for ordinary members, and backend authorization still applies.

Subcommands are `link`, `alias-add`, `alias-remove`, `aliases`, `unresolved`, `resolve` and `job`. Link/alias additions backfill matching unambiguous history by default. Results are ephemeral; indexing runs as a durable backend job. No token belongs in the desktop, frontend, arguments or source. This code has not been deployed by its implementation workflow.
