import copy
import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from service import Announcer, Config, Problem, UpstreamError
from handler import DynamoRepository, handler

HOST = "123456789012345678"
GUILD = "223456789012345678"
CHANNEL = "323456789012345678"
TOKEN = "session-token-" + "x" * 32
BOT = "bot-token-" + "y" * 32
CONFIG = Config("https://identity.example", GUILD, CHANNEL, frozenset({HOST}), enabled=True)


class Memory:
    def __init__(self):
        self.state = {}
        self.lock = threading.Lock()

    def edit(self, host, callback):
        with self.lock:
            current = copy.deepcopy(self.state.get(host, {}))
            result = callback(current)
            self.state[host] = current
            return result


class FakeHttp:
    def __init__(self):
        self.calls, self.messages = [], {}
        self.user = {"discordId": HOST, "displayName": "untrusted @everyone"}
        self.member = {"user": {"id": HOST}, "roles": []}
        self.channel = {"guild_id": GUILD, "type": 0}
        self.fail = None
        self.timeout_after_post = False

    def json(self, url, authorization, body=None):
        self.calls.append((url, authorization, copy.deepcopy(body)))
        if url.endswith("/me"):
            if self.fail == "identity": raise UpstreamError(401)
            return self.user
        if "/members/" in url: return self.member
        if url.endswith("/messages"):
            if self.fail == "429": raise UpstreamError(429, 20)
            if self.fail == "403": raise UpstreamError(403)
            nonce = body["nonce"]
            self.messages.setdefault(nonce, {"id": "423456789012345678"})
            if self.timeout_after_post:
                self.timeout_after_post = False
                raise UpstreamError()
            return self.messages[nonce]
        return self.channel


class Announcements(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000
        self.repo, self.http = Memory(), FakeHttp()
        self.service = Announcer(CONFIG, self.repo, self.http, lambda: BOT, lambda: self.now)

    def send(self, code="ABC123", request="a" * 32, name=""):
        return self.service.announce("Bearer " + TOKEN, {"code": code, "requestId": request, "lobbyName": name})

    def problem(self, status, fn):
        with self.assertRaises(Problem) as result: fn()
        self.assertEqual(result.exception.status, status)
        return result.exception

    def test_verified_identity_and_fixed_destination(self):
        self.assertEqual(self.send()["status"], "sent")
        url, auth, body = self.http.calls[-1]
        self.assertEqual(url, f"https://discord.com/api/v10/channels/{CHANNEL}/messages")
        self.assertEqual(auth, "Bot " + BOT)
        self.assertEqual(body["content"], f"New match started by <@{HOST}>.\nLobby code: `ABC123`")
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertTrue(body["enforce_nonce"])
        self.assertNotIn(TOKEN, json.dumps(self.repo.state))
        self.assertNotIn(BOT, json.dumps(self.repo.state))
        self.assertNotIn("@everyone", body["content"])

    def test_replay_is_deduplicated(self):
        self.send()
        for request in ["a" * 32, "b" * 32]: self.assertEqual(self.send(request=request)["status"], "duplicate")
        self.assertEqual(len([c for c in self.http.calls if c[0].endswith("/messages")]), 1)

    def test_destination_change_does_not_reuse_test_receipts(self):
        self.send(name="Test lobby")
        self.service.config = replace(CONFIG, channel="623456789012345678")
        self.assertEqual(self.send(name="Real lobby")["status"], "sent")
        posts = [c for c in self.http.calls if c[0].endswith("/messages")]
        self.assertNotEqual(posts[0][0], posts[1][0])
        self.assertNotEqual(posts[0][2]["nonce"], posts[1][2]["nonce"])
        self.assertEqual(len(self.http.messages), 2)

    def test_request_identity_is_bound_to_code(self):
        self.send()
        self.problem(409, lambda: self.send("OTHER1"))

    def test_no_client_identity_or_channel_override(self):
        for field in ["discordId", "channelId", "guildId", "message", "displayName"]:
            self.problem(400, lambda: self.service.announce("Bearer " + TOKEN, {"code": "ABC123", "requestId": "a" * 32, field: HOST}))
        self.assertFalse(self.http.calls)

    def test_invalid_codes(self):
        for code in ["abc123", "a", "ABCDE", "ABCDEFG", "ABC-12", "@everyone", "AB\n123", "ABC`123", "https://example.com", "X" * 33]:
            self.problem(400, lambda: self.send(code))

    def test_lobby_name_is_included_as_escaped_plain_text(self):
        self.send(name=" Friday **games** <@everyone> ")
        message = self.http.calls[-1][2]
        self.assertIn("Lobby: Friday \\*\\*games\\*\\* \\<@everyone\\>", message["content"])
        self.assertEqual(message["allowed_mentions"], {"parse": []})

    def test_invalid_lobby_names(self):
        for name in [None, 42, "x" * 81, "one\ntwo", "hidden\u202ename", "one\u2028two"]:
            self.problem(400, lambda: self.send(name=name))

    def test_name_is_bound_to_request_identity(self):
        self.send(name="Original name")
        self.problem(409, lambda: self.send(name="Changed name"))
        self.assertEqual(self.send(request="b" * 32, name="Changed name")["status"], "duplicate")
        self.assertEqual(len(self.http.messages), 1)

    def test_pending_name_changes_keep_original_delivery_payload(self):
        self.http.timeout_after_post = True
        self.problem(503, lambda: self.send(name="Original name"))
        self.now += 16
        self.send(request="b" * 32, name="Changed name")
        posts = [c[2] for c in self.http.calls if c[0].endswith("/messages")]
        self.assertEqual(posts[0], posts[1])
        self.assertIn("Original name", posts[1]["content"])

    def test_missing_expired_or_forged_identity(self):
        self.problem(401, lambda: self.service.announce("Bearer short", {}))
        self.http.fail = "identity"
        self.problem(401, self.send)
        self.http.fail = None; self.http.user = {"id": HOST}
        self.problem(401, self.send)
        self.assertFalse(self.http.messages)

    def test_host_allowlist_and_role(self):
        self.service.config = replace(CONFIG, allowed_hosts=frozenset(), host_role="523456789012345678")
        self.problem(403, self.send)
        self.http.member["roles"] = ["523456789012345678"]
        self.assertEqual(self.send()["status"], "sent")

    def test_membership_and_fixed_server(self):
        self.http.member["pending"] = True
        self.problem(403, self.send)
        self.http.member["pending"] = False; self.http.channel["guild_id"] = HOST
        self.problem(503, self.send)
        self.http.channel["guild_id"] = GUILD; self.http.channel["type"] = 1
        self.problem(503, self.send)

    def test_configuration_disabled_and_fail_closed(self):
        for config in [replace(CONFIG, enabled=False), replace(CONFIG, allowed_hosts=frozenset()), replace(CONFIG, identity_url="http://identity.example")]:
            self.service.config = config; self.problem(503, self.send)
        self.assertFalse(self.http.calls)

    def test_host_cooldown_and_daily_limit(self):
        self.send()
        self.assertGreater(self.problem(429, lambda: self.send("OTHER1", "b" * 32)).retry_after, 0)
        self.now += 61
        self.service.config = replace(CONFIG, daily_limit=1)
        self.problem(429, lambda: self.send("OTHER1", "b" * 32))
        self.now += 86401
        self.assertEqual(self.send("OTHER1", "b" * 32)["status"], "sent")

    def test_concurrent_submissions_create_one_message(self):
        with ThreadPoolExecutor(8) as executor:
            def attempt(_):
                try: return self.send()["status"]
                except Problem as error: return error.status
            results = list(executor.map(attempt, range(8)))
        self.assertEqual(results.count("sent"), 1)
        self.assertEqual(len(self.http.messages), 1)

    def test_timeout_after_delivery_uses_same_nonce(self):
        self.http.timeout_after_post = True
        self.problem(503, self.send)
        self.now += 16
        self.assertEqual(self.send()["status"], "sent")
        self.assertEqual(len(self.http.messages), 1)
        posts = [c[2] for c in self.http.calls if c[0].endswith("/messages")]
        self.assertEqual(posts[0]["nonce"], posts[1]["nonce"])

    def test_uncertain_delivery_is_not_blindly_retried_later(self):
        self.http.timeout_after_post = True
        self.problem(503, self.send)
        self.now += 121
        self.problem(409, self.send)
        self.problem(409, lambda: self.send(request="b" * 32))
        self.assertEqual(len(self.http.messages), 1)

    def test_discord_rate_limit_can_retry(self):
        self.http.fail = "429"
        self.assertEqual(self.problem(429, self.send).retry_after, 20)
        self.http.fail = None; self.now += 21
        self.assertEqual(self.send()["status"], "sent")

    def test_durable_completion_failure_retries_nonce(self):
        real = self.repo.edit
        count = 0
        def fail_once(host, callback):
            nonlocal count
            count += 1
            if count == 2: raise RuntimeError("injected database failure")
            return real(host, callback)
        self.repo.edit = fail_once
        with self.assertRaises(RuntimeError): self.send()
        self.now += 16
        self.assertEqual(self.send()["status"], "sent")
        self.assertEqual(len(self.http.messages), 1)


class EntryPoint(unittest.TestCase):
    def test_health_and_rejected_inputs(self):
        self.assertEqual(handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}}, None)["statusCode"], 200)
        self.assertEqual(handler({}, None)["statusCode"], 404)
        event = {"rawPath": "/announcements", "requestContext": {"http": {"method": "POST"}}, "headers": {"content-type": "application/json"}, "body": "{"}
        with patch("handler.service"):
            self.assertEqual(handler(event, None)["statusCode"], 400)
            event["body"] = "x" * 9000
            self.assertEqual(handler(event, None)["statusCode"], 413)
            event["body"] = "{}"; event["headers"] = {}
            self.assertEqual(handler(event, None)["statusCode"], 415)

    def test_unexpected_errors_do_not_expose_secrets(self):
        event = {"rawPath": "/announcements", "requestContext": {"http": {"method": "POST"}}, "headers": {"content-type": "application/json"}, "body": "{}"}
        with patch("handler.service", side_effect=RuntimeError(BOT)), patch("builtins.print") as log:
            result = handler(event, None)
            self.assertEqual(result["statusCode"], 503)
            self.assertNotIn(BOT, result["body"])
            self.assertNotIn(BOT, repr(log.call_args))

    def test_dynamo_compare_and_swap(self):
        class Conditional(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        class Table:
            item = None
            once = True
            def get_item(self, **kwargs): return {"Item": copy.deepcopy(self.item)} if self.item else {}
            def put_item(self, **kwargs):
                if self.once:
                    self.once = False
                    self.item = {"pk": "HOST#" + HOST, "version": 1, "state": '{"count":1}'}
                    raise Conditional()
                self.item = kwargs["Item"]
        table = Table(); repo = DynamoRepository(table)
        def update(state):
            state["count"] = state.get("count", 0) + 1
            return state["count"]
        self.assertEqual(repo.edit(HOST, update), 2)
        self.assertEqual(table.item["version"], 2)


if __name__ == "__main__": unittest.main()
