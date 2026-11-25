"""Verified SGL-host announcements. No Gateway connection or client-side bot secret."""
import hashlib
import json
import re
import time
import unicodedata
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

    def json(self, url, authorization, body=None, method=None):
        headers = {"Authorization": authorization, "User-Agent": "DiscordBot (https://github.com/SGLifers/SGL-Discord-Bot, 1.0)"}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def lobby_message(host, code, lobby_name, closed=False, gamemode="", map_name=""):
    """One compact card; clearing content also converts legacy text announcements on close."""
    def plain(text):
        return re.sub(r"([\\`*_~|<>\[\]])", r"\\\1", text)
    lines = [f"Name: {plain(lobby_name) or 'Unnamed lobby'}", f"Code: `{code}`", f"Host: <@{host}>"]
    if gamemode:
        lines.append(f"Gamemode: {plain(gamemode)}")
    if map_name:
        lines.append(f"Map: {plain(map_name)}")
    embed = {"title": "Lobby closed" if closed else "Lobby open", "color": 0xED4245 if closed else 0x57F287, "description": "\n\n".join(lines)}
    return {"content": "", "embeds": [embed], "allowed_mentions": {"parse": []}}


class Announcer:
    def __init__(self, config, repo, http, token_provider, now=lambda: int(time.time())):
        self.config, self.repo, self.http, self.token_provider, self.now = config, repo, http, token_provider, now

    def announce(self, authorization, body):
        self.config.validate()
        require(self.config.enabled, 503, "Announcements are disabled.")
        require(isinstance(authorization, str) and re.fullmatch(r"Bearer [A-Za-z0-9_-]{32,512}", authorization), 401, "Sign in with Discord.")
        require(isinstance(body, dict) and {"code", "requestId"} <= set(body) <= {"code", "requestId", "lobbyName", "action", "gamemode", "announcementId", "map"}, 400, "Expected a lobby code, optional name, action and request identity.")
        action = body.get("action", "open")
        require(action in ("open", "close", "update"), 400, "Choose open, close or update.")
        code, request_id = body["code"], body["requestId"]
        require(isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{6}", code), 400, "Lobby codes must contain six letters or numbers.")
        lobby_name = body.get("lobbyName", "")
        require(isinstance(lobby_name, str), 400, "Invalid lobby name.")
        lobby_name = lobby_name.strip()
        require(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in lobby_name), 400, "Lobby names must be on one line.")
        require(len(lobby_name.encode("utf-16-le")) <= 160, 400, "Lobby names may contain up to 80 characters.")
        gamemode = body.get("gamemode", "")
        require(isinstance(gamemode, str), 400, "Invalid gamemode.")
        gamemode = gamemode.strip()
        require(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in gamemode), 400, "Gamemodes must be on one line.")
        require(len(gamemode.encode("utf-16-le")) <= 240, 400, "Gamemodes may contain up to 120 characters.")
        map_name = body.get("map")
        require("map" not in body or isinstance(map_name, str), 400, "Invalid map name.")
        if map_name is not None:
            map_name = map_name.strip()
            require(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in map_name), 400, "Map names must be on one line.")
            require(len(map_name.encode("utf-16-le")) <= 240, 400, "Map names may contain up to 120 characters.")
        announcement_id = body.get("announcementId")
        require(action != "update" or isinstance(announcement_id, str) and re.fullmatch(r"[a-f0-9]{32}", announcement_id), 400, "An update requires its original announcement identity.")
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
        subject = f"{self.config.guild}:{self.config.channel}:{host}"
        if action == "update":
            return self.update(subject, host, bot_auth, code, request_id, announcement_id, gamemode, now, map_name)
        if action == "close":
            return self.close(subject, host, bot_auth, code, request_id, now)

        def claim(state):
            entries = {k: v for k, v in state.get("entries", {}).items() if v["created"] > now - 86400}
            require(not any(request_id in v.get("closeRequestIds", []) or request_id in v.get("updates", {}) for v in entries.values()), 409, "Request identity was used to close a lobby.")
            same = entries.get(request_id)
            require(same is None or same["code"] == code and same.get("lobbyName", "") == lobby_name and same.get("initialGamemode", "") == gamemode and same.get("initialMap", "") == (map_name or ""), 409, "Request identity was used for another announcement.")
            canonical = request_id if same else next((k for k, v in entries.items() if v["code"] == code and not v.get("closed")), request_id)
            previous = entries.get(canonical)
            if previous and previous["status"] == "sent":
                if previous.get("closing"):
                    raise Problem(429, "This lobby is being closed. Retry Close Lobby.", 15)
                return {"status": "duplicate", "messageId": previous["messageId"], "announcementId": canonical, "lobbyStatus": "closed" if previous.get("closed") else "open"}
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
                previous = {"code": code, "lobbyName": lobby_name, "gamemode": gamemode, "initialGamemode": gamemode, "map": map_name or "", "initialMap": map_name or "", "created": now, "attempts": 0}
            previous.update(status="pending", attempted=now if previous.get("status") != "pending" else previous["attempted"], attempts=previous["attempts"] + 1)
            entries[canonical] = previous
            state.update(entries=entries, lockUntil=now + 15, nextAllowed=now + self.config.cooldown, day=day, daily=daily)
            return {"status": "claimed", "canonical": canonical, "lobbyName": previous.get("lobbyName", ""), "gamemode": previous.get("gamemode", ""), "map": previous.get("map", "")}

        result = self.repo.edit(subject, claim)
        if result["status"] == "duplicate":
            return result
        canonical = result["canonical"]
        nonce = hashlib.sha256((subject + ":" + canonical).encode()).hexdigest()[:24]
        # A pending retry keeps its original name, just as it keeps its nonce.
        message = lobby_message(host, code, result["lobbyName"], gamemode=result["gamemode"], map_name=result["map"])
        message.update(nonce=nonce, enforce_nonce=True)
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
            self.repo.edit(subject, failed)
            raise Problem(429 if error.status == 429 else 503, "Discord announcement is unavailable.", error.retry_after or (15 if error.status == 0 or error.status >= 500 else 0)) from None

        def complete(state):
            state["entries"][canonical].update(status="sent", messageId=posted["id"])
            state["lockUntil"] = 0
            return {"status": "sent", "messageId": posted["id"], "announcementId": canonical, "lobbyStatus": "open"}
        return self.repo.edit(subject, complete)

    def close(self, subject, host, bot_auth, code, request_id, now):
        def claim(state):
            entries = {k: v for k, v in state.get("entries", {}).items() if v["created"] > now - 86400}
            require(request_id not in entries and not any(request_id in v.get("updates", {}) for v in entries.values()), 409, "Request identity was already used for another action.")
            # Bind close retries to the original lobby, including after the same
            # code is reopened. An old retry must never close a newer lobby.
            canonical = next((k for k, v in entries.items() if request_id in v.get("closeRequestIds", [])), None)
            if canonical:
                require(entries[canonical]["code"] == code, 409, "Request identity was used for another lobby.")
            else:
                matches = [(k, v) for k, v in entries.items() if v["code"] == code]
                canonical = max(matches, key=lambda pair: (pair[1]["created"], pair[0]))[0] if matches else None
            require(canonical is not None, 404, "No recent announcement for this lobby was found for your account.")
            entry = entries[canonical]
            require(entry["status"] == "sent", 409, "Confirm the open request before closing this lobby.")
            identities = entry.setdefault("closeRequestIds", [])
            if request_id not in identities:
                require(len(identities) < 64, 429, "Too many close requests for this lobby.")
                identities.append(request_id)
            state["entries"] = entries
            if entry.get("closed"):
                return {"status": "closed", "messageId": entry["messageId"], "lobbyStatus": "closed"}
            if state.get("lockUntil", 0) > now:
                raise Problem(429, "A lobby request is in progress.", min(120, state["lockUntil"] - now))
            entry["closing"] = True
            state.update(entries=entries, lockUntil=now + 15)
            return {"canonical": canonical, "messageId": entry["messageId"], "lobbyName": entry.get("lobbyName", ""), "gamemode": entry.get("gamemode", ""), "map": entry.get("map", "")}

        result = self.repo.edit(subject, claim)
        if result.get("status") == "closed":
            return result
        canonical = result["canonical"]
        message = lobby_message(host, code, result["lobbyName"], closed=True, gamemode=result["gamemode"], map_name=result["map"])
        try:
            edited = self.http.json(f"https://discord.com/api/v10/channels/{self.config.channel}/messages/{result['messageId']}", bot_auth, message, method="PATCH")
            require(edited.get("id") == result["messageId"], 503, "Discord did not confirm the lobby update.")
        except UpstreamError as error:
            # A removed announcement is already unavailable to players. Other
            # failures may be retried: PATCH writes the same closed content.
            if error.status != 404:
                def failed(state):
                    state["lockUntil"] = 0
                self.repo.edit(subject, failed)
                raise Problem(429 if error.status == 429 else 503, "The lobby close could not be confirmed.", error.retry_after or 15) from None

        def complete(state):
            state["entries"][canonical].update(closed=True, closing=False)
            state["lockUntil"] = 0
            return {"status": "closed", "messageId": result["messageId"], "lobbyStatus": "closed"}
        return self.repo.edit(subject, complete)

    def update(self, subject, host, bot_auth, code, request_id, announcement_id, gamemode, now, map_name=None):
        fingerprint = hashlib.sha256(json.dumps([gamemode, map_name], ensure_ascii=False).encode()).hexdigest()
        legacy_fingerprint = hashlib.sha256(gamemode.encode()).hexdigest()
        def claim(state):
            entries = {k: v for k, v in state.get("entries", {}).items() if v["created"] > now - 86400}
            entry = entries.get(announcement_id)
            require(entry is not None and entry["code"] == code and not entry.get("closed"), 404, "No open announcement was found for this lobby.")
            require(entry["status"] == "sent" and not entry.get("closing"), 409, "Confirm the open or close request before updating this lobby.")
            require(request_id not in entries and not any(request_id in v.get("closeRequestIds", []) or request_id in v.get("updates", {}) and k != announcement_id for k, v in entries.items()), 409, "Request identity was already used for another action.")
            updates = entry.setdefault("updates", {})
            previous = updates.get(request_id)
            require(previous is None or previous.get("detailsHash") == fingerprint or "detailsHash" not in previous and map_name is None and previous.get("gamemodeHash", hashlib.sha256(previous.get("gamemode", "").encode()).hexdigest()) == legacy_fingerprint, 409, "Request identity was used for another gamemode.")
            if previous and previous["status"] == "sent":
                return {"status": "duplicate", "messageId": entry["messageId"], "announcementId": announcement_id, "lobbyStatus": "open"}
            require(previous is None or entry.get("latestUpdateId") == request_id, 409, "A newer update superseded this request.")
            if state.get("lockUntil", 0) > now:
                raise Problem(429, "A lobby request is in progress.", min(120, state["lockUntil"] - now))
            if previous is None:
                require(sum(len(v.get("updates", {})) for v in entries.values()) < 256, 429, "Daily gamemode update limit reached.")
                updates[request_id] = {"detailsHash": fingerprint, "status": "pending"}
            entry["latestUpdateId"] = request_id
            state.update(entries=entries, lockUntil=now + 45)
            return {"messageId": entry["messageId"], "lobbyName": entry.get("lobbyName", ""), "map": entry.get("map", "") if map_name is None else map_name}

        result = self.repo.edit(subject, claim)
        if result.get("status") == "duplicate":
            return result
        message = lobby_message(host, code, result["lobbyName"], gamemode=gamemode, map_name=result["map"])
        try:
            edited = self.http.json(f"https://discord.com/api/v10/channels/{self.config.channel}/messages/{result['messageId']}", bot_auth, message, method="PATCH")
            require(edited.get("id") == result["messageId"], 503, "Discord did not confirm the gamemode update.")
        except UpstreamError as error:
            def failed(state):
                state["lockUntil"] = 0
                if error.status == 404:
                    state["entries"][announcement_id]["closed"] = True
            self.repo.edit(subject, failed)
            raise Problem(404 if error.status == 404 else 429 if error.status == 429 else 503, "The gamemode update could not be confirmed.", error.retry_after or 15) from None

        def complete(state):
            entry = state["entries"][announcement_id]
            require(entry.get("latestUpdateId") == request_id and not entry.get("closed"), 409, "The lobby changed before this update completed.")
            entry["gamemode"] = gamemode
            entry["map"] = result["map"]
            entry["updates"][request_id]["status"] = "sent"
            state["lockUntil"] = 0
            return {"status": "updated", "messageId": entry["messageId"], "announcementId": announcement_id, "lobbyStatus": "open"}
        return self.repo.edit(subject, complete)
