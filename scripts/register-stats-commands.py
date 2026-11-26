"""Explicit operator command registration; never run by CI and sends no channel messages."""

import argparse
import json
import urllib.request


def command():
    def option(name, description, kind=3, required=True):
        return {
            "type": kind,
            "name": name,
            "description": description,
            "required": required,
        }

    def sub(name, description, options):
        return {"type": 1, "name": name, "description": description, "options": options}

    member = option("member", "Linked Discord member", 6)
    name = option("game-name", "Exact in-game name")
    reason = option("reason", "Audit reason", required=False)
    dates = [
        option(
            "effective-from", "Optional ISO timestamp interval start", required=False
        ),
        option("effective-to", "Optional ISO timestamp interval end", required=False),
    ]
    return {
        "name": "stats",
        "description": "Administer SGL player identity and statistics",
        "default_member_permissions": "0",
        "dm_permission": False,
        "options": [
            sub(
                "link",
                "Link a Discord account and backfill an exact game name",
                [member, name, reason, *dates],
            ),
            sub(
                "alias-add",
                "Add an alias and backfill matching history",
                [member, name, reason, *dates],
            ),
            sub(
                "alias-remove",
                "Retire an alias while preserving prior ownership",
                [member, name, reason, *dates],
            ),
            sub("aliases", "Privately list a member’s aliases", [member]),
            sub(
                "job",
                "Inspect a statistics indexing/admin job",
                [option("id", "Job receipt ID")],
            ),
            sub(
                "unresolved", "Inspect recent unresolved participant references", [name]
            ),
            sub(
                "resolve",
                "Resolve one ambiguous participant after review",
                [
                    option("match", "Match ID"),
                    option("participant", "Participant ID"),
                    member,
                    reason,
                ],
            ),
        ],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--application-id", required=True)
    p.add_argument("--guild-id", required=True)
    p.add_argument("--secret-arn", required=True)
    p.add_argument("--plan-only", action="store_true")
    a = p.parse_args()
    if not a.application_id.isdigit() or not a.guild_id.isdigit():
        raise SystemExit("Use numeric Discord application/guild IDs.")
    if a.plan_only:
        print(json.dumps(command(), indent=2))
        raise SystemExit(0)
    import boto3

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    if session.client("sts").get_caller_identity()["Account"] != a.account_id:
        raise SystemExit("AWS account mismatch.")
    token = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=a.secret_arn)[
            "SecretString"
        ]
    )["token"]
    # POST upserts just /stats, preserving unrelated application commands.
    request = urllib.request.Request(
        f"https://discord.com/api/v10/applications/{a.application_id}/guilds/{a.guild_id}/commands",
        data=json.dumps(command()).encode(),
        headers={"Authorization": "Bot " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    print("Registered guild-only /stats command", result["id"])
