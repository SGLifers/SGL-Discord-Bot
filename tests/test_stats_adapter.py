import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from stats_adapter import forward


class StatsAdapter(unittest.TestCase):
    def test_forward_preserves_signature_body_and_base64_flag(self):
        event = {
            "rawPath": "/stats/interactions",
            "headers": {
                "x-signature-timestamp": "1000",
                "x-signature-ed25519": "a" * 128,
            },
            "body": "eyJ0eXBlIjoxfQ==",
            "isBase64Encoded": True,
        }
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            return {"Payload": io.BytesIO(b'{"statusCode":200,"body":"ok"}')}

        with patch.dict(
            os.environ,
            {
                "STATS_API_FUNCTION_ARN": "arn:aws:lambda:us-west-2:123456789012:function:stats"
            },
        ):
            result = forward(event, invoke, lambda: 1000)
        self.assertEqual(result["statusCode"], 200)
        sent = json.loads(calls[0]["Payload"])
        self.assertEqual(sent["body"], event["body"])
        self.assertEqual(sent["headers"], event["headers"])
        self.assertTrue(sent["isBase64Encoded"])
        self.assertEqual(sent["rawPath"], "/v1/stats/discord/interactions")

    def test_disabled_unsigned_and_stale_do_not_invoke(self):
        calls = []
        with patch.dict(os.environ, {"STATS_API_FUNCTION_ARN": ""}):
            self.assertEqual(forward({}, calls.append)["statusCode"], 503)
        with patch.dict(os.environ, {"STATS_API_FUNCTION_ARN": "configured"}):
            self.assertEqual(
                forward({"headers": {}, "body": ""}, calls.append)["statusCode"], 401
            )
        self.assertEqual(calls, [])
