"""Forward Discord interactions to the statistics service without changing signed bytes."""

import json
import os
import re
import time


def forward(event, invoke=None, clock=time.time):
    arn = os.environ.get("STATS_API_FUNCTION_ARN", "")

    def error(status, message):
        return {
            "statusCode": status,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
            "body": json.dumps({"error": message}),
        }

    if not arn:
        return error(503, "Statistics commands are not configured.")
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    stamp = headers.get("x-signature-timestamp", "")
    if (
        not re.fullmatch("[0-9]{1,16}", stamp)
        or abs(clock() - int(stamp)) > 300
        or not re.fullmatch("[a-fA-F0-9]{128}", headers.get("x-signature-ed25519", ""))
    ):
        return error(401, "Signed Discord interaction required.")
    body = event.get("body", "")
    if not isinstance(body, str) or len(body) > 350000:
        return error(413, "Interaction too large.")
    try:
        if invoke is None:
            import boto3
            from botocore.config import Config

            invoke = boto3.client(
                "lambda",
                config=Config(
                    connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 1}
                ),
            ).invoke
        forwarded = {**event, "rawPath": "/v1/stats/discord/interactions"}
        response = invoke(FunctionName=arn, Payload=json.dumps(forwarded).encode())
        if response.get("FunctionError"):
            return error(503, "Statistics commands unavailable.")
        result = json.load(response["Payload"])
        if not isinstance(result, dict) or type(result.get("statusCode")) is not int:
            return error(503, "Statistics commands unavailable.")
        return result
    except Exception:
        return error(503, "Statistics commands unavailable.")
