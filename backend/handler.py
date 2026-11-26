"""AWS HTTP API entry point. Tokens, request bodies and upstream responses are never logged."""
import base64
import json
import os
import time
from service import Announcer, Config, Http, Problem


class DynamoRepository:
    def __init__(self, table):
        self.table = table

    def edit(self, host, callback):
        for _ in range(8):
            item = self.table.get_item(Key={"pk": "HOST#" + host}, ConsistentRead=True).get("Item")
            version = int(item["version"]) if item else 0
            state = json.loads(item["state"]) if item else {}
            result = callback(state)
            record = {"pk": "HOST#" + host, "version": version + 1, "state": json.dumps(state, separators=(",", ":")), "expiresAt": int(time.time()) + 172800}
            try:
                args = {"Item": record, "ConditionExpression": "attribute_not_exists(pk)"} if item is None else {"Item": record, "ConditionExpression": "version = :version", "ExpressionAttributeValues": {":version": version}}
                self.table.put_item(**args)
                return result
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        raise Problem(503, "Announcement state is busy.", 5)


_service = None


def service():
    global _service
    if _service is None:
        import boto3
        config = Config(os.environ["SGL_IDENTITY_URL"], os.environ["DISCORD_GUILD_ID"], os.environ["DISCORD_CHANNEL_ID"],
                        frozenset(x.strip() for x in os.environ.get("ALLOWED_HOST_IDS", "").split(",") if x.strip()),
                        os.environ.get("HOST_ROLE_ID", ""), os.environ.get("ANNOUNCEMENTS_ENABLED", "false") == "true")
        secrets = boto3.client("secretsmanager")
        def token():
            raw = secrets.get_secret_value(SecretId=os.environ["BOT_SECRET_ARN"])["SecretString"]
            return json.loads(raw).get("token", "")
        _service = Announcer(config, DynamoRepository(boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])), Http(), token)
    return _service


def response(status, body, retry=0):
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if retry:
        headers["Retry-After"] = str(retry)
        body["retryAfter"] = retry
    return {"statusCode": status, "headers": headers, "body": json.dumps(body)}


def handler(event, context):
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method", "")
        path = event.get("rawPath", "")
        if path == "/stats/interactions" and method == "POST":
            from stats_adapter import forward
            return forward(event)
        if path == "/health" and method == "GET":
            return response(200, {"service": "SGL Hosting", "enabled": os.environ.get("ANNOUNCEMENTS_ENABLED", "false") == "true", "lobbyActions": ["open", "close", "update"]})
        if path != "/announcements" or method != "POST":
            return response(404, {"error": "Not found."})
        raw = event.get("body", "")
        if not isinstance(raw, str) or len(raw) > 8192:
            raise Problem(413, "Request too large.")
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        if len(raw.encode()) > 4096:
            raise Problem(413, "Request too large.")
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        if not headers.get("content-type", "").lower().startswith("application/json"):
            raise Problem(415, "Use application/json.")
        result = service().announce(headers.get("authorization", ""), json.loads(raw))
        return response(200, result)
    except Problem as error:
        return response(error.status, {"error": str(error)}, error.retry_after)
    except (ValueError, UnicodeError, TypeError):
        return response(400, {"error": "Invalid request."})
    except Exception as error:
        print(json.dumps({"event": "announcement_failure", "type": type(error).__name__}))
        return response(503, {"error": "Announcement service unavailable."}, 15)
