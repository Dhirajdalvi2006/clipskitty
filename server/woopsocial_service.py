"""Shared state for WoopSocial publishing: settings, the API key, the project.

A sibling of server/uploadpost_service.py, kept separate for the same reason
that one is kept separate from the YouTube service: the two providers share
no configuration, and a change to one must not be able to break the other.
A creator uses whichever they already have an account with, or both.

The API key goes through core/secrets.py, not into the settings blob.
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import secrets
from server import posts

SETTINGS_KEY = "woopsocial"
ACCOUNTS_KEY = "woopsocial_accounts"  # named API keys, kept in app_state
KEY_SECRET_NAME = "woopsocial_key"  # legacy key, and the prefix for named keys
KEY_SECRET = KEY_SECRET_NAME

# Older installs have one key and one project. It becomes this profile, so an
# upgrade needs no reconnect and every old call keeps working with no account
# argument. New keys get their own id and their own encrypted secret file.
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_ACCOUNT_NAME = "My WoopSocial"
_ACCOUNT_ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A WoopSocial "project" is what their UI calls a Business Profile: the thing
# connected social accounts belong to. Created on demand under this name so a
# creator is never asked to invent one.
DEFAULT_PROJECT_NAME = "Clips Kitty"

# The approved WoopSocial referral URL, shipped to everyone.
#
# A value saved in one install's settings only affects that machine, so the
# link users actually see has to live here, in the code that ships. Setting
# it turns on the affiliate CTA and, with it, the disclosure that has to sit
# beside it — the two are never shown apart.
AFFILIATE_URL = "https://woopsocial.com/?via=clipskitty"

DEFAULTS = {
    "enabled": False,
    # Their project id, resolved once and remembered. Kept for old installs;
    # new ones keep it on the named profile instead.
    "project_id": "",
    "platforms": [],
    "common_description": "",
    # Overrides AFFILIATE_URL above for this install only, for trying a link
    # before committing it.
    "affiliate_url": "",
    # Which named key the Settings card manages with the old /key routes.
    "selected_account_id": DEFAULT_ACCOUNT_ID,
}


def load_settings(db) -> dict:
    try:
        stored = json.loads(db.get_flag(SETTINGS_KEY, "") or "{}")
    except (ValueError, TypeError):
        stored = {}
    return {**DEFAULTS, **(stored if isinstance(stored, dict) else {})}


def save_settings(db, patch: dict) -> dict:
    merged = {**load_settings(db), **{k: v for k, v in patch.items() if k in DEFAULTS}}
    db.set_flag(SETTINGS_KEY, json.dumps(merged))
    return merged


def is_enabled(db) -> bool:
    return bool(load_settings(db).get("enabled"))


# ---- named WoopSocial keys -------------------------------------------------
#
# A profile is one API key and the WoopSocial project behind it. A creator can
# give each one a name and publish to several of them; the id, never the key,
# travels through the API and is written beside each post so a later status
# check asks the right account about it.


def _clean_account_id(value: str) -> str:
    value = str(value or "").strip()
    if not _ACCOUNT_ID_SHAPE.fullmatch(value):
        return ""
    return value


def _clean_profile(raw) -> dict | None:
    """Only the fields a profile may carry, with safe defaults."""
    if not isinstance(raw, dict):
        return None
    account_id = _clean_account_id(raw.get("id"))
    if not account_id:
        return None
    name = str(raw.get("name") or "").strip()[:80] or DEFAULT_ACCOUNT_NAME
    platforms = raw.get("platforms")
    return {
        "id": account_id,
        "name": name,
        "project_id": str(raw.get("project_id") or ""),
        "platforms": [str(p) for p in platforms] if isinstance(platforms, list) else [],
    }


def _default_profile(db) -> dict:
    """The single key from older installs, presented as a named profile.

    Its secret keeps the original file name, so upgrading never has to copy a
    DPAPI blob or ask anyone to reconnect. The old project and platform choice
    move across on this first read as well.
    """
    settings = load_settings(db)
    return {
        "id": DEFAULT_ACCOUNT_ID,
        "name": DEFAULT_ACCOUNT_NAME,
        "project_id": str(settings.get("project_id") or ""),
        "platforms": list(settings.get("platforms") or []),
    }


def profiles(db) -> list[dict]:
    try:
        stored = json.loads(db.get_flag(ACCOUNTS_KEY, "") or "[]")
    except (ValueError, TypeError):
        stored = []
    except AttributeError:
        # A test double without the accounts flag: fall back to the default
        # profile, which reads settings (load_settings) instead.
        return [_default_profile(db)]
    found = [p for raw in (stored if isinstance(stored, list) else [])
             if (p := _clean_profile(raw)) is not None]
    if not found:
        found = [_default_profile(db)]
        try:
            db.set_flag(ACCOUNTS_KEY, json.dumps(found))
        except AttributeError:
            pass
    return found


def save_profiles(db, values: list[dict]) -> list[dict]:
    clean = [p for raw in values if (p := _clean_profile(raw)) is not None]
    if not clean:
        clean = [_default_profile(db)]
    try:
        db.set_flag(ACCOUNTS_KEY, json.dumps(clean))
    except AttributeError:
        pass
    return clean


def _selected_id(db) -> str:
    chosen = _clean_account_id(load_settings(db).get("selected_account_id"))
    return chosen or DEFAULT_ACCOUNT_ID


def selected_account_id(db) -> str:
    """The key the Settings card currently names, for callers with no picker."""
    return _selected_id(db)


def refresh_account_ids(db) -> list[str]:
    """Every row identity whose posts need following.

    Rows written before keys were named carry an empty account id; requests
    that name the default profile carry "default". Both used the original
    single-key secret, but they are different database rows, so each needs its
    own pass to keep a refresh from writing one post's status onto another.
    """
    return ["", DEFAULT_ACCOUNT_ID,
            *[p["id"] for p in profiles(db) if p["id"] != DEFAULT_ACCOUNT_ID]]


def get_profile(db, account_id: str | None = None) -> dict:
    """Resolve an account id; old no-id callers get the selected profile.

    An unknown or malformed id falls back rather than raising: a watch saved
    before a profile was deleted must still find somewhere sensible to land.
    """
    all_profiles = profiles(db)
    chosen = _clean_account_id(account_id) if account_id else _selected_id(db)
    for profile in all_profiles:
        if profile["id"] == chosen:
            return profile
    return all_profiles[0]


def find_profile(db, account_id: str) -> dict | None:
    chosen = _clean_account_id(account_id)
    return next((p for p in profiles(db) if p["id"] == chosen), None)


def select_profile(db, account_id: str) -> dict:
    chosen = find_profile(db, account_id)
    if chosen is None:
        raise ValueError("There is no WoopSocial key with that name.")
    save_settings(db, {"selected_account_id": chosen["id"]})
    return chosen


def update_profile(db, account_id: str, patch: dict) -> dict:
    """Change one profile's own settings, leaving the others alone."""
    chosen = find_profile(db, account_id)
    if chosen is None:
        raise ValueError("There is no WoopSocial key with that name.")
    updated = [{**p, **patch} if p["id"] == chosen["id"] else p for p in profiles(db)]
    save_profiles(db, updated)
    return get_profile(db, chosen["id"])


def create_profile(db, data_dir: Path, name: str, api_key: str = "") -> dict:
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("Give this key a name.")
    profile = {
        "id": "acc_" + uuid.uuid4().hex[:12],
        "name": name,
        "project_id": "",
        "platforms": [],
    }
    save_profiles(db, [*profiles(db), profile])
    select_profile(db, profile["id"])
    if api_key:
        save_key(data_dir, api_key, profile["id"])
    return profile


def rename_profile(db, account_id: str, name: str) -> dict:
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("Give this key a name.")
    return update_profile(db, account_id, {"name": name})


def delete_profile(db, data_dir: Path, account_id: str) -> dict:
    profile = find_profile(db, account_id)
    if profile is None:
        raise ValueError("There is no WoopSocial key with that name.")
    if profile["id"] == DEFAULT_ACCOUNT_ID:
        # The original key has the un-suffixed secret file and is what old
        # installs and one-argument callers use. Removing the profile would
        # silently leave those posts with no key to check them.
        raise ValueError("The original WoopSocial key cannot be removed.")
    remaining = [p for p in profiles(db) if p["id"] != profile["id"]]
    if not remaining:
        raise ValueError("Keep at least one WoopSocial key.")
    save_profiles(db, remaining)
    wipe_key(data_dir, profile["id"])
    if _selected_id(db) == profile["id"]:
        save_settings(db, {"selected_account_id": remaining[0]["id"]})
    return profile


def _key_state(data_dir: Path, account_id: str) -> tuple[bool, str]:
    """Key facts for one profile, without breaking one-argument test doubles.

    The original key lives under the un-suffixed secret name, so the default
    profile asks the original one-argument helpers. That keeps old installs and
    callers working; named profiles get their own id.
    """
    if account_id == DEFAULT_ACCOUNT_ID:
        return has_key(data_dir), key_tail(data_dir)
    return has_key(data_dir, account_id), key_tail(data_dir, account_id)


def profile_payload(db, data_dir: Path, profile: dict) -> dict:
    """What the renderer may know about a named key. Never the key itself."""
    has, tail = _key_state(data_dir, profile["id"])
    return {
        "id": profile["id"],
        "name": profile["name"],
        "project_id": profile["project_id"],
        "has_key": has,
        "key_tail": tail,
        "platforms": profile["platforms"],
    }


# ---- the API key -----------------------------------------------------------


def _key_secret_name(account_id: str | None) -> str:
    """The original blob stays where it was; new keys get their own file."""
    account_id = _clean_account_id(account_id) or DEFAULT_ACCOUNT_ID
    if account_id == DEFAULT_ACCOUNT_ID:
        return KEY_SECRET_NAME
    return f"{KEY_SECRET_NAME}_{account_id}"


def save_key(data_dir: Path, api_key: str, account_id: str | None = None) -> None:
    secrets.save(
        Path(data_dir), _key_secret_name(account_id), {"api_key": (api_key or "").strip()}
    )


def load_key(data_dir: Path, account_id: str | None = None) -> str:
    got = secrets.load(Path(data_dir), _key_secret_name(account_id)) or {}
    return str(got.get("api_key") or "")


def has_key(data_dir: Path, account_id: str | None = None) -> bool:
    if is_legacy_account(account_id):
        return bool(load_key(data_dir))
    return bool(load_key(data_dir, account_id))


def wipe_key(data_dir: Path, account_id: str | None = None) -> bool:
    return secrets.wipe(Path(data_dir), _key_secret_name(account_id))


def key_tail(data_dir: Path, account_id: str | None = None) -> str:
    if is_legacy_account(account_id):
        key = load_key(data_dir)
    else:
        key = load_key(data_dir, account_id)
    return key[-4:] if len(key) > 8 else ""


def is_legacy_account(account_id: str | None) -> bool:
    """Whether a row belongs to the original single-key secret."""
    return not account_id or account_id == DEFAULT_ACCOUNT_ID


def has_key_for(data_dir: Path, account_id: str | None = None) -> bool:
    """Check a profile's key without making old one-argument callers two-arg."""
    if is_legacy_account(account_id):
        return has_key(data_dir)
    return has_key(data_dir, account_id)


def make_client(data_dir: Path, account_id: str | None = None):
    from publish.woopsocial import WoopSocialClient

    return WoopSocialClient(load_key(data_dir, account_id))


def resolve_project(db, client, account_id: str | None = None) -> str:
    """The project id, created on first use, on the chosen named key.

    Their accounts hang off a project, and a creator should never be asked to
    make one — so if none is remembered, an existing one is adopted or a new
    one is created under a fixed name.
    """
    # A caller with no account argument is an old single-key caller: even if
    # Settings now points a picker at another profile, it must keep resolving
    # the original key's project.
    profile = get_profile(db, DEFAULT_ACCOUNT_ID if is_legacy_account(account_id) else account_id)
    if profile["project_id"]:
        return profile["project_id"]
    projects = client.projects()
    if projects:
        found = str(projects[0].get("id") or "")
    else:
        found = str((client.create_project(DEFAULT_PROJECT_NAME) or {}).get("id") or "")
    if found:
        update_profile(db, profile["id"], {"project_id": found})
        if is_legacy_account(account_id):
            # Older installs and one-argument readers expect it in settings too.
            save_settings(db, {"project_id": found})
    return found


def tags_of(clip) -> list[str]:
    """A clip's hashtags, stored as a JSON list in one column."""
    raw = clip["hashtags"] if "hashtags" in clip.keys() else ""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    # One tag per word. Clips made before analysis/metadata.py split them can
    # carry "#creatorname#drama#apology" as a single entry, which would go
    # into a caption as one unreadable hashtag.
    out: list[str] = []
    for entry in parsed:
        for part in re.split(r"[#\s]+", str(entry)):
            if part.strip() and part not in out:
                out.append(part.strip())
    return out


def _account_fields(db, account_id: str, account_name: str = "") -> dict:
    """Row identity for one post, resolved once for sends and refreshes.

    An empty id is an older caller or a pre-upgrade row and keeps the old
    behaviour: one post per clip and platform, with blank account fields.
    """
    if not account_id:
        return {"account_id": "", "account_name": ""}
    if not account_name:
        profile = find_profile(db, account_id)
        account_name = profile["name"] if profile else ""
    return {"account_id": account_id, "account_name": account_name}


def _publisher_for(db, data_dir: Path, account_id: str = ""):
    """A publisher holding the right key and project for one named profile."""
    from publish.woopsocial import WoopSocialPublisher

    if not is_legacy_account(account_id):
        client = make_client(data_dir, account_id)
        return WoopSocialPublisher(client, resolve_project(db, client, account_id))
    # The original call shape: kept for legacy rows and the single-key tests.
    client = make_client(data_dir)
    return WoopSocialPublisher(client, resolve_project(db, client))


def record_outcomes(db, clip_id: int, clip, result, *, scheduled_for: str = "",
                    caption="", visibility="", account_id: str = "",
                    account_name: str = "") -> None:
    """Write one row per platform for a fan-out that was just sent.

    Publish time only: this is what the post IS. The identity (video_id and
    the window) is written only while the clip row exists, never blanked.
    `scheduled_for` is when the post is actually due ('' for now). It is kept
    because a run spread over days is otherwise invisible: the app sent the
    times and then forgot them, so the only way to see the plan was the
    provider's own site. `caption` is the text that went out and `visibility`
    the one asked for; either may be one value or {platform: value}.

    `account_id` is the named key that sent it. It is part of the row's
    identity, so two keys posting the same clip to YouTube stay two posts with
    two independent states and links.

    A later status check goes through record_refresh, which learns states and
    links and leaves all of this alone. This is a send, so the row's clock
    starts again (created_at): a clip sent again to a platform is due from
    this send, not the first one.
    """
    sent = posts.sent_stamp()
    identity = _account_fields(db, account_id, account_name)
    for outcome in result.outcomes:
        fields = {
            "provider": "woopsocial",
            **identity,
            "created_at": sent,
            "state": outcome.state,
            "post_id": outcome.post_id,
            "post_url": outcome.post_url,
            "error": outcome.error,
            "request_id": result.request_id,
            "scheduled_for": scheduled_for or "",
            "caption": posts.for_platform(caption, outcome.platform),
            "visibility": posts.for_platform(visibility, outcome.platform),
        }
        if clip is not None:
            fields["video_id"] = clip["video_id"] or ""
            fields["start_s"] = clip["start_s"] or 0
            fields["end_s"] = clip["end_s"] or 0
        db.record_clip_publish(clip_id, outcome.platform, fields)


def record_refresh(db, clip_id: int, result, *, known=None, account_id: str = "",
                   account_name: str = "") -> int:
    """Write what a status check learned: state, ids, link and error only.

    The row's clip_id may by now be a re-rendered clip with a different
    window (StateDB.follow_clip_id), so a refresh must never write the post's
    identity, caption or time: those describe the post that went out.

    With `known`, the request's rows as stored, an outcome that only repeats
    its row is not written, nor one for a platform sent again since under a
    newer request (server/posts.py news). Returns how many were. The named key
    that made the post must be passed too, or the update would land on a
    different row from the send it describes.
    """
    provider = "woopsocial"
    if known and not account_id:
        first = known[0]
        try:
            account_id = str(first["account_id"] or "")
            account_name = str(first["account_name"] or "")
            provider = str(first["provider"] or "woopsocial")
        except (KeyError, TypeError, IndexError):
            # A test's stand-in row need not carry every column.
            pass
    identity = _account_fields(db, account_id, account_name)
    outcomes = result.outcomes if known is None else posts.news(known, result)
    for outcome in outcomes:
        db.record_clip_publish(clip_id, outcome.platform, {
            "provider": provider,
            **identity,
            "state": outcome.state,
            "post_id": outcome.post_id,
            "post_url": outcome.post_url,
            "error": outcome.error,
            "request_id": result.request_id,
        })
    return len(outcomes)


def committed_times(db, account_id: str = "") -> list[str]:
    """Every slot already spoken for, so a new batch queues behind them.

    A daily budget is per day, not per batch: without this, publishing a
    second video while the first is still going out puts two runs on the same
    days and the platform rejects the overflow. That is how 32 posts were
    lost the first time.

    Counted: anything still queued or processing with a time on it, and
    anything published today, which has already spent part of today's
    allowance. Not counted: failures, which hold nothing, and other providers'
    posts, which spend their own allowance rather than WoopSocial's. With an
    `account_id`, only that named key's posts count against its own allowance.
    """
    rows = db.conn.execute(
        "SELECT clip_id, state, scheduled_for, updated_at FROM clip_publishes "
        "WHERE state IN ('queued', 'processing', 'published', 'sending') "
        "AND provider = 'woopsocial' AND (? = '' OR account_id = ?)",
        (account_id, account_id),
    ).fetchall()
    today = datetime.now(timezone.utc).date().isoformat()
    out: list[str] = []
    seen: set[tuple[int, str]] = set()
    for r in rows:
        when = r["scheduled_for"] or ""
        if r["state"] == "published":
            # Only today's posts matter; older ones spent an allowance that
            # has since reset.
            stamp = when or (r["updated_at"] or "")
            if not stamp.startswith(today):
                continue
            when = stamp
        # A clip fanned out to YouTube and TikTok is one scheduling slot.
        # Separate clips at the same instant must still count separately.
        identity = (r["clip_id"], when)
        if when and identity not in seen:
            seen.add(identity)
            out.append(when)
    return out


# The start of the description analysis/metadata.py wrote when the model could
# not, until it stopped. Existing clips still carry it.
FALLBACK_PREFIX = "Clip from: "

# The daily budget used when a request asks to publish but says nothing about
# spacing: from the chat box, an MCP client, or a job that was queued with
# "publish when done". WoopSocial rations YouTube to five posts a day to protect
# the Google Cloud quota it shares between all its users, and 37 clips sent at
# once lost 32 of them. Five a day is the shape that fits.
DEFAULT_PER_DAY = 5


def first_slot_at(day_start: str, *, now: datetime | None = None) -> datetime:
    """The next moment, local time, that is `day_start` ("HH:MM") and far
    enough ahead for the scheduler's lead time. A day's run is anchored there,
    so "first post at 09:00" means 09:00 every day, not 09:00 once."""
    from datetime import timedelta

    from publish.schedule import MIN_LEAD_SECONDS

    hours, minutes = (int(part) for part in day_start.split(":", 1))
    earliest = ((now or datetime.now().astimezone()) + timedelta(seconds=MIN_LEAD_SECONDS + 60)).astimezone()
    # astimezone() returns the offset for that instant, not a timezone with
    # future DST rules. Choose the local calendar date before converting it.
    first = earliest.replace(hour=hours, minute=minutes, second=0, microsecond=0, tzinfo=None)
    if first.astimezone() < earliest:
        first += timedelta(days=1)
    return first.astimezone(timezone.utc)


def schedule_times(db, count: int, *, per_day: int, gap_hours: float = 1,
                   start_at: str = "", day_start: str = "",
                   account_id: str = "") -> list[str]:
    """When each of `count` posts goes out on a daily budget.

    One copy for the publish and for the plan shown before it, so the plan a
    person agrees to is the schedule that is sent. `day_start` ("HH:MM", local)
    puts each day's first post at that time; empty starts as soon as allowed.
    """
    from datetime import timedelta

    from publish.schedule import MIN_LEAD_SECONDS, daily_after, to_rfc3339

    if not count or not per_day:
        return []
    first = datetime.now(timezone.utc) + timedelta(seconds=MIN_LEAD_SECONDS + 60)
    if day_start and not start_at:
        first = first_slot_at(day_start)
    if start_at:
        try:
            first = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError:
            start_at = ""
    # "Start now" cannot mean this instant: every generated time goes through
    # the same validator a hand-picked one does, and that refuses anything
    # inside the lead time, so it begins at the first moment it would accept.
    # Without a start it also queues behind whatever is already scheduled
    # rather than on top of it; an explicit start_at is an instruction, so it
    # still wins.
    return daily_after(
        [] if start_at else committed_times(db, account_id),
        int(count),
        int(per_day),
        float(gap_hours or 1),
        to_rfc3339(first),
        local_start=bool(day_start and not start_at),
    )


# States in which a clip counts as sent, or on its way, to a platform.
ALIVE = ("queued", "processing", "published", "sending")

# How long a post may sit at "sending" before it is treated as interrupted.
# Longer than the upload and create timeouts together can run, so a slow send
# that is still in progress is never mistaken for one that died.
STALE_SENDING_SECONDS = 20 * 60


def already_sent(db, clip, platforms: list[str], account_id: str = "") -> set[str]:
    """The platforms this clip already went to, or is on its way to.

    Matched on the clip's lasting identity as well as its id, because a
    re-render gives the same clip a new id. A failure or a skip holds nothing:
    those are exactly what a retry is for. With `account_id`, only that named
    key counts: the same clip may still be sent through a different key."""
    marks = ",".join("?" * len(ALIVE))
    rows = db.conn.execute(
        f"SELECT platform FROM clip_publishes WHERE state IN ({marks}) AND "
        "(clip_id = ? OR (video_id != '' AND video_id = ? AND start_s = ? AND end_s = ?)) "
        "AND (? = '' OR account_id = ?)",
        (*ALIVE, clip["id"], clip["video_id"] or "", clip["start_s"], clip["end_s"],
         account_id, account_id),
    ).fetchall()
    return {r["platform"] for r in rows} & set(platforms)


def publish_clips(
    db,
    data_dir: Path,
    *,
    clip_ids: list[int],
    platforms: list[str],
    hashtags: list[str] | None = None,
    every_hours: float = 0,
    start_at: str = "",
    overrides: dict | None = None,
    exclude: dict | None = None,
    per_day: int = 0,
    gap_hours: float = 1,
    footer: str = "",
    once: bool = False,
    remember: bool = True,
    lead_hashtags: list[str] | None = None,
    ai_hashtags: bool = True,
    day_start: str = "",
    discord_destination_id: int | None = None,
    account_id: str = "",
    social_account_ids: list[str] | None = None,
) -> dict:
    """Publish a set of clips, optionally spaced out over time.

    Lives here rather than in the route because several callers need it: the
    /woopsocial/batch endpoint, the worker finishing a job that was asked to
    publish when it was queued (server/jobs.py), and watched channels
    (server/automation.py). A second copy of this loop would drift from the
    first the moment either changed.

    One post per clip, because each clip is different media. The spacing uses
    WoopSocial's own scheduler rather than a timer here, so a run stretching
    over days keeps going with Clips Kitty closed.

    `once` is for callers nobody is watching, which may run this again for the
    same clips after a restart or to retry failures. WoopSocial has no
    idempotency key, so the guard is here: a clip already sent or on its way to
    a platform is left alone, and each send is recorded as "sending" before any
    request goes out, then tagged with its media the moment the upload lands,
    so reconcile_sending can settle it if Clips Kitty stops halfway. Without
    `once` nothing changes: a person pressing Publish again means it.

    `footer` goes under the standing text. `remember` keeps a background run
    from changing the platforms the publish dialog offers next time.

    `lead_hashtags` are a creator's own, set once ("#creatorname #twitch"):
    they open the hashtag line, so a platform that trims the end never loses
    them. `ai_hashtags=False` leaves out the ones the model chose. `day_start`
    anchors each day's posts at a local "HH:MM". All three default to exactly
    what every other caller has always had.

    `account_id` names the WoopSocial key/profile to send through. Every row
    carries it, so two keys can post the same clip to the same platform without
    overwriting each other, and a later refresh asks the key that sent it.
    `social_account_ids` narrows a project to particular connected channels.
    """
    from datetime import datetime, timedelta
    from datetime import timezone as tz
    from pathlib import Path as _Path

    from publish.errors import PublishError
    from publish.woopsocial import WoopSocialPublisher

    # An empty id deliberately means the original single key. Callers with a
    # picker (the HTTP routes, watched channels, jobs) pass the chosen profile
    # explicitly; that keeps old one-argument callers and their test doubles
    # working without a settings lookup here.
    account_name = ""
    if account_id and not is_legacy_account(account_id):
        # A named profile: its own encrypted key and its own project memory.
        profile = get_profile(db, account_id)
        account_id = profile["id"]
        account_name = profile["name"]
        client = make_client(data_dir, account_id)
        project = resolve_project(db, client, account_id)
    else:
        # The original call shape: the default/legacy key uses the un-suffixed
        # secret and one-argument helpers, so old installs and one-argument
        # test doubles keep working unchanged.
        account_id = DEFAULT_ACCOUNT_ID
        client = make_client(data_dir)
        project = resolve_project(db, client)
    publisher = WoopSocialPublisher(client, project)
    identity = _account_fields(db, account_id, account_name)
    standing = (load_settings(db).get("common_description") or "").strip()

    start = datetime.now(tz.utc)
    if start_at:
        try:
            start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError:
            start = datetime.now(tz.utc)

    # A daily budget, when one was asked for. Posting limits are daily, so
    # "five a day, an hour apart" is the shape that works, and a flat interval
    # cannot express it.
    slot_times = schedule_times(
        db, len(clip_ids), per_day=per_day, gap_hours=gap_hours, start_at=start_at,
        day_start=day_start, account_id=account_id,
    )

    # Exceptions, keyed by clip id. JSON turns integer keys into strings on
    # the way in, so both are accepted rather than one silently missing.
    skip_map: dict[int, set[str]] = {}
    for key, values in (exclude or {}).items():
        try:
            skip_map[int(key)] = {str(v) for v in values}
        except (TypeError, ValueError):
            continue

    started: list[dict] = []
    skipped: list[dict] = []
    slot = 0
    for index, clip_id in enumerate(clip_ids):
        clip = db.get_clip(clip_id)
        if clip is None:
            skipped.append({"clip_id": clip_id, "reason": "no such clip"})
            continue

        # Some platforms are stricter than others, so a clip that is fine on
        # one is held back from another rather than dropped everywhere.
        wanted = [p for p in platforms if p not in skip_map.get(clip_id, set())]
        if not wanted:
            skipped.append({"clip_id": clip_id, "reason": "excluded from every platform"})
            continue
        if not (clip["path"] and _Path(clip["path"]).exists()):
            skipped.append({"clip_id": clip_id, "reason": "no rendered file yet"})
            continue
        if once:
            sent = already_sent(db, clip, wanted, account_id)
            wanted = [p for p in wanted if p not in sent]
            if not wanted:
                # Not a slot: nothing is being sent.
                skipped.append({"clip_id": clip_id, "reason": "already sent"})
                continue

        # The title leads. Only YouTube has a title field, so everywhere else
        # it used to be discarded and the caption opened with the description:
        # on TikTok the hook, which is the whole reason the line was written,
        # never reached the post at all. Skipped when the description already
        # opens with it, so nothing reads twice.
        headline = (clip["title"] or clip["hook"] or "").strip()
        body = (clip["description"] or "").strip()
        # What analysis/metadata.py used to write when the model failed. A
        # caption announcing it is a clip of someone else's video got TikTok
        # posts flagged as unoriginal, so an old one is never sent, and the
        # generic #clips that came with it goes too.
        fallback_caption = body.startswith(FALLBACK_PREFIX)
        if fallback_caption:
            body = ""
        if headline and not body.casefold().startswith(headline.casefold()):
            text = (headline + "\n\n" + body).strip()
        else:
            text = body or headline
        if standing:
            text = (text + "\n\n" + standing).strip()
        if footer.strip():
            text = (text + "\n\n" + footer.strip()).strip()
        # The clip's own tags first, then anything asked for across the whole
        # run, with duplicates dropped so a tag the clip already had is not
        # repeated.
        tags = tags_of(clip) if ai_hashtags else []
        if fallback_caption:
            tags = [t for t in tags if t.lower() != "clips"]
        for extra in hashtags or []:
            cleaned = str(extra).lstrip("#").strip()
            if cleaned and cleaned.lower() not in {t.lower() for t in tags}:
                tags.append(cleaned)
        # A creator's own set-ahead tags lead, ahead of everything else.
        lead = []
        for extra in lead_hashtags or []:
            cleaned = str(extra).lstrip("#").strip()
            if cleaned and cleaned.lower() not in {t.lower() for t in lead}:
                lead.append(cleaned)
        if lead:
            chosen = {t.lower() for t in lead}
            tags = lead + [t for t in tags if t.lower() not in chosen]
        if tags:
            text = (text + "\n\n" + " ".join("#" + t for t in tags)).strip()

        when = ""
        if slot_times:
            # `slot` counts clips actually being sent, not the loop index: a
            # clip skipped for being excluded or unrendered must not burn one
            # of the day's five slots.
            when = slot_times[slot] if slot < len(slot_times) else slot_times[-1]
        elif every_hours or start_at:
            at = start + timedelta(hours=every_hours * slot)
            # Their scheduler wants UTC and refuses a past time; the first slot
            # of a "starting now" run would otherwise be a second or two behind
            # by the time it arrives.
            if at <= datetime.now(tz.utc):
                at = datetime.now(tz.utc) + timedelta(minutes=2)
            when = at.isoformat()

        visibility = posts.visibilities("woopsocial", wanted, overrides)
        if discord_destination_id is not None:
            from campaigns import post_links

            post_links.prepare(db, destination_id=discord_destination_id, clip_id=clip_id,
                               platforms=wanted, provider="woopsocial")
        uploaded: list[str] = []
        on_media = None
        if once:
            for platform in wanted:
                db.record_clip_publish(clip_id, platform, {
                    # A send: the row's clock starts again (record_outcomes).
                    "provider": "woopsocial", **identity,
                    "created_at": posts.sent_stamp(),
                    "video_id": clip["video_id"] or "",
                    "start_s": clip["start_s"] or 0, "end_s": clip["end_s"] or 0,
                    "state": "sending", "post_id": "", "post_url": "", "error": "",
                    "request_id": "", "media_id": "", "scheduled_for": when,
                    # Kept from the start, so a post reconcile_sending adopts
                    # later still says what it said.
                    "caption": text, "visibility": visibility[platform],
                })

            def on_media(media_id, clip_id=clip_id, targets=tuple(wanted), uploaded=uploaded,
                         who=identity):
                uploaded.append(media_id)
                for platform in targets:
                    db.record_clip_publish(clip_id, platform, {
                        "provider": "woopsocial", **who, "media_id": media_id,
                    })

        # Only a background run asks to hear about the media; a person's
        # publish calls start() exactly as it always has. The same goes for an
        # explicit social-account target: an old one-argument publisher keeps
        # seeing only the keywords it always did.
        extra = {"on_media": on_media} if on_media is not None else {}
        if social_account_ids:
            extra["social_account_ids"] = social_account_ids
        try:
            result = publisher.start(
                _Path(clip["path"]),
                platforms=wanted,
                title=(clip["title"] or clip["hook"] or f"Clip {clip_id}").strip(),
                text=text,
                scheduled_for=when,
                overrides=overrides or {},
                **extra,
            )
        except PublishError as e:
            # One clip's problem must not cost the rest of the batch.
            skipped.append({"clip_id": clip_id, "reason": e.message})
            if once and not (uploaded and e.retryable):
                # Refused outright, or stopped before the upload landed: no
                # post exists, so it is safe to send again.
                for platform in wanted:
                    db.record_clip_publish(clip_id, platform, {
                        "provider": "woopsocial", **identity,
                        "state": "failed", "error": e.message[:300],
                    })
            # A timeout after the upload may or may not have created the post.
            # That row stays "sending" for reconcile_sending to settle.
            continue

        record_outcomes(db, clip_id, clip, result, scheduled_for=when, caption=text,
                        visibility=visibility, account_id=account_id,
                        account_name=account_name)
        if discord_destination_id is not None:
            post_links.recover(db, completed_clip=clip_id, provider="woopsocial")
        started.append(
            {"clip_id": clip_id, "request_id": result.request_id, "scheduled_for": when}
        )
        slot += 1

    if remember:
        if account_id:
            update_profile(db, account_id, {"platforms": platforms})
        else:
            save_settings(db, {"platforms": platforms})
    return {"started": started, "skipped": skipped}


def reconcile_sending(db, data_dir: Path, *, older_than: float = STALE_SENDING_SECONDS) -> dict:
    """Settle posts Clips Kitty stopped in the middle of sending.

    A row still at "sending" long after any send could have finished means the
    app stopped between writing it and learning the result. Either the post
    was never created, and the clip can safely be sent again, or it was, and
    sending again would post it twice. The uploaded media tells which: no media
    means no post could exist, and a post carrying the media is adopted with
    its id, so the ordinary status refresh follows it from there.
    """
    from datetime import timedelta

    from publish.errors import PublishError
    from publish.woopsocial import WoopSocialPublisher

    cutoff = (datetime.now() - timedelta(seconds=older_than)).isoformat(timespec="seconds")
    rows = db.conn.execute(
        "SELECT * FROM clip_publishes WHERE state = 'sending' AND provider = 'woopsocial' "
        "AND updated_at <= ? ORDER BY clip_id",
        (cutoff,),
    ).fetchall()
    if not rows:
        return {"adopted": 0, "failed": 0}

    groups: dict[tuple[int, str, str], list] = {}
    for row in rows:
        key = (int(row["clip_id"]), row["media_id"] or "", str(row["account_id"] or ""))
        groups.setdefault(key, []).append(row)

    publishers: dict[str, object] = {}
    adopted = failed = 0
    never_sent = "Clips Kitty stopped before this was sent. It is safe to send again."
    for (clip_id, media_id, account_id), group in groups.items():
        platforms = [r["platform"] for r in group]
        identity = _account_fields(db, account_id, str(group[0]["account_name"] or ""))
        found = None
        if media_id:
            try:
                if account_id not in publishers:
                    publishers[account_id] = _publisher_for(db, data_dir, account_id)
                found = publishers[account_id].find_by_media(media_id, platforms)
            except PublishError:
                continue  # unreachable now; a later refresh tries again
        if found is not None and found.outcomes:
            # The "sending" row already holds the post's identity, caption and
            # time; adopting it only learns the post's id and state.
            record_refresh(db, clip_id, found, account_id=account_id)
            adopted += 1
            platforms = [p for p in platforms if p not in {o.platform for o in found.outcomes}]
        for platform in platforms:
            db.record_clip_publish(clip_id, platform, {
                "provider": "woopsocial", **identity,
                "state": "failed", "error": never_sent,
            })
            failed += 1
    return {"adopted": adopted, "failed": failed}


def in_flight(db, provider: str = "woopsocial", account_id: str | None = None) -> list[str]:
    """Request ids with at least one destination still unfinished, newest first.

    One id per fan-out, not per row: a 37-clip batch is 37 rows but they were
    accepted as separate posts, so this returns each distinct request once and
    the caller asks about each once.

    Only this provider's: Upload-Post rows carry a request_id too, and asking
    WoopSocial about one of those can only fail. Filtered in SQL. A named
    account narrows it further, so the worker can use that key to check them.
    """
    rows = db.conn.execute(
        "SELECT DISTINCT request_id FROM clip_publishes "
        "WHERE request_id != '' AND state IN ('queued', 'processing') AND provider = ? "
        "AND (? IS NULL OR account_id = ?) ORDER BY created_at DESC",
        (provider, account_id, account_id),
    ).fetchall()
    return [r["request_id"] for r in rows]


#: How far ahead of its time a post is already worth asking about.
DUE_AHEAD_SECONDS = 120


def due_in_flight(db, *, now=None, provider: str = "woopsocial",
                  account_id: str | None = None) -> list[str]:
    """The in-flight requests that are due, oldest-due first.

    A watch spreads its clips over days, so the newest requests are the ones
    due last: checking those first left a day-one post unchecked for a week.
    A request is due at the earliest due time of its rows (server/posts.py);
    one due more than two minutes from now is left out, since there is
    nothing to learn about a post that has not gone out yet, and so is one
    due more than 14 days ago, as Upload-Post's are (posts.FOLLOW_DAYS).
    """
    current = posts.as_utc(now)
    rows = db.conn.execute(
        "SELECT request_id, created_at, scheduled_for FROM clip_publishes "
        "WHERE request_id != '' AND state IN ('queued', 'processing') AND provider = ? "
        "AND (? IS NULL OR account_id = ?)",
        (provider, account_id, account_id),
    ).fetchall()
    earliest: dict = {}
    for row in rows:
        due = posts.due_time(row, now=current)
        request_id = row["request_id"]
        if request_id not in earliest or due < earliest[request_id]:
            earliest[request_id] = due
    horizon = current + timedelta(seconds=DUE_AHEAD_SECONDS)
    oldest = current - timedelta(days=posts.FOLLOW_DAYS)
    due_now = [(due, rid) for rid, due in earliest.items() if oldest <= due <= horizon]
    return [rid for _, rid in sorted(due_now)]


#: Request id -> unix time the background refresh last asked about it. In
#: memory only, like the worker's Upload-Post check times: after a restart
#: every due request is new again.
_last_checked: dict[str, float] = {}


def refresh_in_flight(db, data_dir: Path, *, limit: int = 60, ids=None, now=None,
                      last_checked: dict | None = None, account_id: str = "") -> dict:
    """Ask WoopSocial what became of everything still in the air.

    Nothing did this before, so a batch stayed at "processing" forever: the
    clips were delivered at WoopSocial's own pace and the app never looked
    again, which left no way to tell a slow queue from a failure. That is the
    whole reason this exists.

    Without `ids` it checks the requests that are due (due_in_flight), the
    ones never asked about first, oldest-due first, then the ones asked about
    longest ago. Requests whose check keeps failing (a post deleted on
    WoopSocial answers 404 for good) stay in flight, and with more than
    `limit` of them a plain oldest-first pick would ask only about those,
    every pass, and never about a post that came due after them. The check
    times live in `last_checked` (the module's own when not given). A caller
    that wants particular requests names them in `ids`.

    Failures are counted, not raised. A refresh is a read; one unreachable
    request must not stop the rest being updated. `account_id` checks through
    that named key, so a post id that happens to repeat on two keys is still
    followed with the credentials that sent it.
    """
    from publish.errors import PublishError

    current = posts.as_utc(now)
    checked_key = (lambda rid: f"{account_id}\0{rid}") if account_id else (lambda rid: rid)
    checked = None
    if ids is None:
        checked = _last_checked if last_checked is None else last_checked
        due = due_in_flight(db, now=current, account_id=account_id)
        keep = {checked_key(r) for r in due}
        for gone in [r for r in list(checked) if r not in keep]:
            checked.pop(gone, None)
        # sorted() is stable: among requests asked about equally long ago,
        # due_in_flight's oldest-due order stands.
        ids = sorted(due, key=lambda r: checked.get(checked_key(r), 0.0))
    ids = list(ids)[:limit]
    if not ids:
        return {"checked": 0, "updated": 0, "still_waiting": 0, "failed": 0}

    publisher = _publisher_for(db, data_dir, account_id)

    updated = failed = 0
    for request_id in ids:
        if checked is not None:
            checked[checked_key(request_id)] = current.timestamp()
        # A blank id is the legacy account's own rows: one argument, as older
        # DB doubles expose only publishes_for_request(request_id). A named
        # key passes its id so the check reads only that key's rows.
        if is_legacy_account(account_id):
            rows = db.publishes_for_request(request_id)
        else:
            rows = db.publishes_for_request(request_id, account_id)
        if not rows:
            continue
        clip_id = int(rows[0]["clip_id"])
        try:
            result = publisher.check(request_id)
        except PublishError:
            failed += 1
            continue
        except Exception:
            failed += 1
            continue
        # Only what changed is written: a check that repeats the rows leaves
        # updated_at alone (server/posts.py news).
        if result.outcomes and record_refresh(db, clip_id, result, known=rows,
                                              account_id=account_id):
            updated += 1

    return {
        "checked": len(ids),
        "updated": updated,
        "still_waiting": len(in_flight(db, account_id=account_id)),
        "failed": failed,
    }


def refresh_missing_urls(db, data_dir: Path, *, now=None, account_id: str = "") -> dict:
    """Ask again about posts WoopSocial called published without a link, for
    a day after each one was due (server/posts.py missing_url_requests)."""
    ids = posts.missing_url_requests(db, "woopsocial", now=now)
    # The same request id can be in flight under two keys; each pass asks its
    # own key about only its own rows, including the legacy blank ones.
    ids = [rid for rid in ids if db.publishes_for_request(rid, account_id)]
    return refresh_in_flight(db, data_dir, ids=ids, now=now, account_id=account_id)


def upcoming(db, *, limit: int = 200, account_id: str = "") -> list[dict]:
    """What is due, soonest first, with the clip's own title.

    The point of storing scheduled_for: a run spread across eight days is a
    plan, and a plan you cannot see is indistinguishable from a queue that
    silently dropped your posts. Which is exactly what happened.
    """
    rows = db.conn.execute(
        "SELECT p.clip_id, p.platform, p.account_id, p.account_name, p.state, "
        "       p.scheduled_for, p.post_url, p.error, c.title, c.hook "
        "FROM clip_publishes p LEFT JOIN clips c ON c.id = p.clip_id "
        "WHERE p.scheduled_for != '' AND (? = '' OR p.account_id = ?) "
        "ORDER BY p.scheduled_for ASC LIMIT ?",
        (account_id, account_id, int(limit)),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "clip_id": r["clip_id"],
                "platform": r["platform"],
                "account_id": r["account_id"] or "",
                "account_name": r["account_name"] or "",
                "state": r["state"],
                "scheduled_for": r["scheduled_for"],
                "post_url": r["post_url"] or "",
                "error": r["error"] or "",
                "title": (r["title"] or r["hook"] or f"Clip {r['clip_id']}"),
            }
        )
    return out


def status_payload(db, data_dir: Path) -> dict:
    """What the renderer is allowed to know. No key, ever."""
    settings = load_settings(db)
    selected = get_profile(db)
    has, tail = _key_state(data_dir, selected["id"])
    return {
        "enabled": bool(settings.get("enabled")),
        "has_key": has,
        "key_tail": tail,
        "project_id": selected["project_id"],
        "platforms": selected["platforms"],
        "selected_account_id": selected["id"],
        # Named keys, with just enough to tell them apart. No key material.
        "accounts": [profile_payload(db, data_dir, p) for p in profiles(db)],
        "common_description": settings.get("common_description") or "",
        "affiliate_url": settings.get("affiliate_url") or AFFILIATE_URL,
        "storage": secrets.backend_name(),
    }
