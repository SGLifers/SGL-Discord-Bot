"""Verified SGL-host announcements. No Gateway connection or client-side bot secret."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class Problem(Exception):
    def __init__(self, status, message, retry_after=0):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def require(test, status, message):
    if not test:
        raise Problem(status, message)


def snowflake(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{17,20}", value) is not None


@dataclass(frozen=True)
class Config:
    identity_url: str
    guild: str
    channel: str
    allowed_hosts: frozenset
    host_role: str = ""
    enabled: bool = False
    cooldown: int = 60
    daily_limit: int = 30

    def validate(self):
        uri = urllib.parse.urlsplit(self.identity_url)
        require(uri.scheme == "https" and bool(uri.hostname) and not uri.username and not uri.password and not uri.query and not uri.fragment and uri.port in (None, 443), 503, "Invalid identity service configuration.")
        require(snowflake(self.guild) and snowflake(self.channel), 503, "Configure the Discord server and channel.")
        require(bool(self.allowed_hosts or self.host_role) and all(snowflake(x) for x in self.allowed_hosts) and (not self.host_role or snowflake(self.host_role)), 503, "Configure permitted hosts or a host role.")
        require(10 <= self.cooldown <= 3600 and 1 <= self.daily_limit <= 100, 503, "Invalid announcement limits.")


class UpstreamError(Exception):
    def __init__(self, status=0, retry_after=0):
        self.status, self.retry_after = status, retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect)

    def json(self, url, authorization, body=None):
        headers = {"Authorization": authorization, "User-Agent": "DiscordBot (https://github.com/SGLifers/SGL-Discord-Bot, 1.0)"}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise UpstreamError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise UpstreamError()
                return result
        except urllib.error.HTTPError as error:
            retry = 0
            if error.code == 429:
                try:
                    value = json.loads(error.read(4096))
                    retry = min(120, max(1, int(float(value.get("retry_after", 5))) + 1))
                except (ValueError, TypeError, AttributeError):
                    retry = 5
            raise UpstreamError(error.code, retry) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise UpstreamError() from None


class Announcer:
    def __init__(self, config, repo, http, token_provider, now=lambda: int(time.time())):
        self.config, self.repo, self.http, self.token_provider, self.now = config, repo, http, token_provider, now

    def announce(self, authorization, body):
        self.config.validate()
        require(self.config.enabled, 503, "Announcements are disabled.")
        require(isinstance(authorization, str) and re.fullmatch(r"Bearer [A-Za-z0-9_-]{32,512}", authorization), 401, "Sign in with Discord.")
        require(isinstance(body, dict) and set(body) == {"code", "requestId"}, 400, "Expected a lobby code and request identity.")
        code, request_id = body["code"], body["requestId"]
        require(isinstance(code, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,31}", code), 400, "Invalid lobby code.")
        require(isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id), 400, "Invalid request identity.")
        try:
            user = self.http.json(self.config.identity_url.rstrip("/") + "/me", authorization)
        except UpstreamError as error:
            raise Problem(401 if error.status == 401 else 503, "Sign in again." if error.status == 401 else "Identity verification unavailable.") from None
        host = user.get("discordId")
        require(snowflake(host), 401, "No verified Discord identity.")
        token = self.token_provider()
        require(isinstance(token, str) and 20 <= len(token) <= 256 and not any(c.isspace() for c in token), 503, "Configure the Discord bot token.")
        bot_auth = "Bot " + token
        try:
            member = self.http.json(f"https://discord.com/api/v10/guilds/{self.config.guild}/members/{host}", bot_auth)
            require(member.get("user", {}).get("id") == host and not member.get("pending"), 403, "Join the Discord server before announcing.")
            require(host in self.config.allowed_hosts or self.config.host_role and self.config.host_role in member.get("roles", []), 403, "This account is not a permitted host.")
            channel = self.http.json(f"https://discord.com/api/v10/channels/{self.config.channel}", bot_auth)
            require(channel.get("guild_id") == self.config.guild and channel.get("type") in (0, 5), 503, "Configure a text channel in the selected server.")
        except UpstreamError as error:
            raise Problem(403 if error.status == 404 else 503, "Server membership or bot configuration could not be verified.", error.retry_after) from None
        now = self.now()

        def claim(state):
            entries = {k: v for k, v in state.get("entries", {}).items() if v["created"] > now - 86400}
            same = entries.get(request_id)
            require(same is None or same["code"] == code, 409, "Request identity was used for another code.")
            canonical = request_id if same else next((k for k, v in entries.items() if v["code"] == code), request_id)
            previous = entries.get(canonical)
            if previous and previous["status"] == "sent":
                return {"status": "duplicate", "messageId": previous["messageId"]}
            if previous and previous["status"] == "pending":
                require(now - previous["attempted"] < 120 and previous["attempts"] < 3, 409, "Delivery is uncertain. Check the Discord channel before retrying.")
            if state.get("lockUntil", 0) > now:
                raise Problem(429, "An announcement is in progress.", min(120, state["lockUntil"] - now))
            if not previous or previous["status"] != "pending":
                if state.get("nextAllowed", 0) > now:
                    raise Problem(429, "Announcements are cooling down.", min(120, state["nextAllowed"] - now))
            day = now // 86400
            daily = state.get("daily", 0) if state.get("day") == day else 0
            if previous is None:
                require(daily < self.config.daily_limit, 429, "Daily announcement limit reached.")
                daily += 1
                previous = {"code": code, "created": now, "attempts": 0}
            previous.update(status="pending", attempted=now if previous.get("status") != "pending" else previous["attempted"], attempts=previous["attempts"] + 1)
            entries[canonical] = previous
            state.update(entries=entries, lockUntil=now + 15, nextAllowed=now + self.config.cooldown, day=day, daily=daily)
            return {"status": "claimed", "canonical": canonical}

        result = self.repo.edit(host, claim)
        if result["status"] == "duplicate":
            return result
        canonical = result["canonical"]
        nonce = hashlib.sha256((host + ":" + canonical).encode()).hexdigest()[:24]
        message = {"content": f"New match started by <@{host}>.\nLobby code: `{code}`", "allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}
        try:
            posted = self.http.json(f"https://discord.com/api/v10/channels/{self.config.channel}/messages", bot_auth, message)
            require(snowflake(posted.get("id")), 503, "Discord did not confirm delivery.")
        except UpstreamError as error:
            def failed(state):
                entry = state["entries"][canonical]
                if error.status == 429:
                    entry["status"] = "retryable"
                    state["nextAllowed"] = self.now() + (error.retry_after or 5)
                    state["lockUntil"] = 0
                elif error.status in (400, 401, 403, 404):
                    entry["status"] = "failed"
                    state["lockUntil"] = 0
                # A timeout/5xx may have delivered. Keep pending and retry only with
                # the same nonce within the bounded Discord nonce window.
                return None
            self.repo.edit(host, failed)
            raise Problem(429 if error.status == 429 else 503, "Discord announcement is unavailable.", error.retry_after or (15 if error.status == 0 or error.status >= 500 else 0)) from None

        def complete(state):
            state["entries"][canonical].update(status="sent", messageId=posted["id"])
            state["lockUntil"] = 0
            return {"status": "sent", "messageId": posted["id"]}
        return self.repo.edit(host, complete)
