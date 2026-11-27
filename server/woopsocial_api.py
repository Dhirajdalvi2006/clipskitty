"""HTTP routes for publishing through WoopSocial.

Mirrors server/uploadpost_api.py: same route shapes, same rules, same
`clip_publishes` table underneath, so the renderer talks to whichever
provider is switched on without caring which it is. Installed with one call
from create_app().

The two rules that hold everywhere here:

* A switched-off feature 404s and /woopsocial/status says only
  `{"enabled": false}`. Off should read as absent.
* **The API key never leaves the backend.** The UI gets `has_key` and four
  characters. Errors go through redact().
"""

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from campaigns import post_links
from publish.errors import PublishError
from publish.woopsocial import FROM_PLATFORM
from server import posts
from server import woopsocial_service as service
from server.feedback import redact


class KeyIn(BaseModel):
    api_key: str


class AccountIn(BaseModel):
    """A named API key. The name is what people see; the id never holds a key."""

    name: str = ""
    api_key: str = ""


class RenameIn(BaseModel):
    name: str = ""


class SelectIn(BaseModel):
    account_id: str = ""


class RefreshIn(BaseModel):
    account_id: str = ""


class SettingsIn(BaseModel):
    enabled: bool | None = None
    platforms: list[str] | None = None
    common_description: str | None = None
    affiliate_url: str | None = None


class ConnectIn(BaseModel):
    platform: str = ""
    account_id: str = ""


class BatchIn(BaseModel):
    discord_destination_id: int | None = Field(default=None, ge=1)
    clip_ids: list[int] = []
    platforms: list[str] = []
    overrides: dict[str, dict] = {}
    # Hours between posts. 24 is one a day, which is the usual shape for
    # spreading a video's clips across a month.
    every_hours: float = 0
    start_at: str = ""
    # Added to every clip in the run, on top of whatever tags each clip
    # already carries. The "#" is optional; duplicates are dropped.
    hashtags: list[str] = []
    # Clip id -> platforms to hold it back from. Absent means every clip
    # goes to every platform picked, which is the usual case; this is for
    # the handful a stricter platform should not get.
    exclude: dict[str, list[str]] = {}
    # A daily budget, which is the shape posting limits actually take:
    # WoopSocial allows five YouTube posts a day. per_day 0 keeps the old
    # flat every_hours behaviour for anything already calling it.
    per_day: int = 0
    gap_hours: float = 1
    # Which named key sends this run. Empty means the key selected in Settings.
    account_id: str = ""
    # Optional connected social account ids inside that key's project, for
    # choosing one of two channels on the same platform.
    social_account_ids: list[str] = []


class PublishIn(BaseModel):
    discord_destination_id: int | None = Field(default=None, ge=1)
    platforms: list[str] = []
    title: str = ""
    description: str = ""
    tags: list[str] = []
    overrides: dict[str, dict] = {}
    scheduled_date: str = ""
    account_id: str = ""
    social_account_ids: list[str] = []


def install(app, *, config, db, data_dir, publish_worker=None) -> None:
    data_path = Path(data_dir)

    def _guard(d):
        if not service.is_enabled(d):
            raise HTTPException(404, "WoopSocial publishing is not enabled")

    def _fail(e: Exception) -> HTTPException:
        message = e.message if isinstance(e, PublishError) else str(e)
        return HTTPException(400, redact(message)[:500])

    def _profile(d, account_id: str = "") -> dict:
        """The named key a request means, or the one selected in Settings."""
        return service.get_profile(d, account_id or None)

    def _has_key(account_id: str) -> bool:
        # Blank legacy rows and the default profile share the original secret
        # and its one-argument helpers, so an upgrade needs no reconnect.
        return service.has_key_for(data_path, account_id)

    def _client_for(account_id: str):
        if not _has_key(account_id):
            raise HTTPException(400, "No WoopSocial API key is set for that account.")
        if service.is_legacy_account(account_id):
            return service.make_client(data_path)
        return service.make_client(data_path, account_id)

    def _client(d, account_id: str = ""):
        return _client_for(_profile(d, account_id)["id"])

    def _project(d, client, account_id: str = "") -> str:
        """The project behind one named key, created on first use.

        Their accounts hang off a project, and a creator should never be
        asked to make one — so if none is remembered, an existing one is
        adopted or a new one is created under a fixed name.
        """
        if service.is_legacy_account(account_id):
            return service.resolve_project(d, client)
        return service.resolve_project(d, client, account_id)

    def _account_payload(d, account_id: str = "") -> dict:
        return service.profile_payload(d, data_path, _profile(d, account_id))

    # ---- status and settings --------------------------------------------

    @app.get("/woopsocial/status")
    def woopsocial_status():
        d = db()
        try:
            return service.status_payload(d, data_path)
        finally:
            d.close()

    @app.patch("/woopsocial/settings")
    def woopsocial_settings(body: SettingsIn):
        d = db()
        try:
            service.save_settings(
                d, {k: v for k, v in body.model_dump().items() if v is not None}
            )
            return service.status_payload(d, data_path)
        finally:
            d.close()

    # ---- named keys -------------------------------------------------------

    def _save_key(account_id: str, key: str) -> None:
        service.save_key(data_path, key, account_id)

    def _wipe_key(account_id: str) -> bool:
        if account_id == service.DEFAULT_ACCOUNT_ID:
            return service.wipe_key(data_path)
        return service.wipe_key(data_path, account_id)

    def _validate_key(account_id: str) -> int:
        """Check a key that was just saved, discarding it if it does not work."""
        try:
            client = _client_for(account_id)
            account = client.validate_key()
        except PublishError as e:
            _wipe_key(account_id)
            raise _fail(e) from e
        return len((account or {}).get("projects") or [])

    @app.get("/woopsocial/accounts")
    def list_accounts():
        d = db()
        try:
            return {
                "accounts": [service.profile_payload(d, data_path, p)
                             for p in service.profiles(d)],
                "selected_account_id": service.selected_account_id(d),
            }
        finally:
            d.close()

    @app.post("/woopsocial/accounts")
    def add_account(body: AccountIn):
        d = db()
        try:
            try:
                profile = service.create_profile(d, data_path, body.name)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            projects = 0
            key = (body.api_key or "").strip()
            if key:
                _save_key(profile["id"], key)
                try:
                    projects = _validate_key(profile["id"])
                except HTTPException:
                    # Do not leave a half-made profile behind after a bad key.
                    try:
                        service.delete_profile(d, data_path, profile["id"])
                    except ValueError:
                        pass
                    raise
            return {
                **service.status_payload(d, data_path),
                "account": service.profile_payload(d, data_path, profile),
                "projects": projects,
            }
        finally:
            d.close()

    @app.patch("/woopsocial/accounts/{account_id}")
    def rename_account(account_id: str, body: RenameIn):
        d = db()
        try:
            try:
                profile = service.rename_profile(d, account_id, body.name)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return service.profile_payload(d, data_path, profile)
        finally:
            d.close()

    @app.delete("/woopsocial/accounts/{account_id}")
    def remove_account(account_id: str):
        d = db()
        try:
            try:
                service.delete_profile(d, data_path, account_id)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return service.status_payload(d, data_path)
        finally:
            d.close()

    @app.post("/woopsocial/accounts/{account_id}/select")
    def select_account(account_id: str):
        d = db()
        try:
            try:
                service.select_profile(d, account_id)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return service.status_payload(d, data_path)
        finally:
            d.close()

    @app.put("/woopsocial/accounts/{account_id}/key")
    def put_named_key(account_id: str, body: KeyIn):
        key = (body.api_key or "").strip()
        if not key:
            raise HTTPException(400, "Enter your WoopSocial API key.")
        d = db()
        try:
            if service.find_profile(d, account_id) is None:
                raise HTTPException(400, "There is no WoopSocial key with that name.")
            _save_key(account_id, key)
            projects = _validate_key(account_id)
            return {
                **service.status_payload(d, data_path),
                "projects": projects,
            }
        finally:
            d.close()

    @app.delete("/woopsocial/accounts/{account_id}/key")
    def delete_named_key(account_id: str):
        d = db()
        try:
            if service.find_profile(d, account_id) is None:
                raise HTTPException(400, "There is no WoopSocial key with that name.")
            return {"removed": _wipe_key(account_id), **service.status_payload(d, data_path)}
        finally:
            d.close()

    @app.get("/woopsocial/accounts/{account_id}/connections")
    def named_connections(account_id: str):
        d = db()
        try:
            _guard(d)
            profile = service.find_profile(d, account_id)
            if profile is None:
                raise HTTPException(400, "There is no WoopSocial key with that name.")
            client = _client_for(account_id)
            try:
                rows = client.social_accounts(_project(d, client, account_id))
            except PublishError as e:
                raise _fail(e) from e
            return _connections_payload(rows)
        finally:
            d.close()

    @app.post("/woopsocial/accounts/{account_id}/connect")
    def named_connect(account_id: str, body: ConnectIn):
        d = db()
        try:
            _guard(d)
            if not body.platform:
                raise HTTPException(400, "Which platform?")
            if service.find_profile(d, account_id) is None:
                raise HTTPException(400, "There is no WoopSocial key with that name.")
            client = _client_for(account_id)
            try:
                url = client.connect_url(_project(d, client, account_id), body.platform)
            except PublishError as e:
                raise _fail(e) from e
            if not url:
                raise HTTPException(
                    400, "WoopSocial did not return a connection link. Try again."
                )
            return {"url": url, "platform": body.platform}
        finally:
            d.close()

    # ---- the selected key, kept for older clients ------------------------

    @app.put("/woopsocial/key")
    def put_key(body: KeyIn):
        key = (body.api_key or "").strip()
        if not key:
            raise HTTPException(400, "Enter your WoopSocial API key.")
        d = db()
        try:
            account_id = service.selected_account_id(d)
            _save_key(account_id, key)
            projects = _validate_key(account_id)
            return {
                **service.status_payload(d, data_path),
                "projects": projects,
            }
        finally:
            d.close()

    @app.delete("/woopsocial/key")
    def delete_key():
        d = db()
        try:
            account_id = service.selected_account_id(d)
            return {"removed": _wipe_key(account_id), **service.status_payload(d, data_path)}
        finally:
            d.close()

    # ---- social accounts -------------------------------------------------

    def _connections_payload(rows) -> dict:
        accounts = []
        for row in rows:
            platform = FROM_PLATFORM.get(str(row.get("platform") or "").upper())
            identifier = row.get("id")
            if not platform or not identifier:
                continue
            username = row.get("username")
            accounts.append({"id": str(identifier), "platform": platform,
                             "username": username[:100] if isinstance(username, str) else ""})
        return {"connected": sorted({a["platform"] for a in accounts}), "accounts": accounts}

    @app.get("/woopsocial/connections")
    def connections(account_id: str = ""):
        d = db()
        try:
            _guard(d)
            profile = _profile(d, account_id)
            client = _client_for(profile["id"])
            try:
                rows = client.social_accounts(_project(d, client, profile["id"]))
            except PublishError as e:
                raise _fail(e) from e
            return _connections_payload(rows)
        finally:
            d.close()

    @app.post("/woopsocial/connect")
    def connect(body: ConnectIn):
        """A browser link for connecting one platform.

        Unlike Upload-Post's single hosted page, WoopSocial authorises one
        platform at a time, so the caller names which.
        """
        d = db()
        try:
            _guard(d)
            if not body.platform:
                raise HTTPException(400, "Which platform?")
            profile = _profile(d, body.account_id)
            client = _client_for(profile["id"])
            try:
                url = client.connect_url(_project(d, client, profile["id"]), body.platform)
            except PublishError as e:
                raise _fail(e) from e
            if not url:
                raise HTTPException(
                    400, "WoopSocial did not return a connection link. Try again."
                )
            return {"url": url, "platform": body.platform}
        finally:
            d.close()

    # ---- publishing ------------------------------------------------------

    @app.post("/woopsocial/clips/{clip_id}/publish")
    def publish_clip(clip_id: int, body: PublishIn):
        from publish.uploadpost import validate_schedule
        from publish.woopsocial import WoopSocialPublisher

        d = db()
        try:
            _guard(d)
            clip = d.get_clip(clip_id)
            if clip is None:
                raise HTTPException(404, "no such clip")
            if not body.title.strip():
                raise HTTPException(400, "A title is required.")
            if not body.platforms:
                raise HTTPException(400, "Pick at least one platform.")
            if not (clip["path"] and Path(clip["path"]).exists()):
                raise HTTPException(
                    400, "This clip has no rendered file yet. Apply your edits first."
                )

            settings = service.load_settings(d)
            text = body.description
            standing = (settings.get("common_description") or "").strip()
            if standing:
                text = f"{text}\n\n{standing}".strip()
            if body.tags:
                text = f"{text}\n\n{' '.join('#' + t.lstrip('#') for t in body.tags)}".strip()

            try:
                when = validate_schedule(body.scheduled_date)
            except PublishError as e:
                raise _fail(e) from e

            profile = _profile(d, body.account_id)
            client = _client_for(profile["id"])
            publisher = WoopSocialPublisher(
                client, _project(d, client, profile["id"])
            )
            try:
                post_links.prepare(d, destination_id=body.discord_destination_id, clip_id=clip_id,
                                   platforms=body.platforms, provider="woopsocial", title=body.title.strip())
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            try:
                result = publisher.start(
                    Path(clip["path"]),
                    platforms=body.platforms,
                    title=body.title.strip(),
                    text=text,
                    scheduled_for=when,
                    overrides=body.overrides,
                    social_account_ids=body.social_account_ids,
                )
            except PublishError as e:
                raise _fail(e) from e

            # Publish time: the post's identity, its time, its text and the
            # visibility asked for. With no time the post is due now. The named
            # key belongs to the row's identity, so two keys can post this clip
            # to the same platform independently.
            service.record_outcomes(
                d, clip_id, clip, result, scheduled_for=when, caption=text,
                visibility=posts.visibilities("woopsocial", body.platforms, body.overrides),
                account_id=profile["id"], account_name=profile["name"],
            )
            if body.discord_destination_id is not None:
                post_links.recover(d, completed_clip=clip_id, provider="woopsocial")
            service.update_profile(d, profile["id"], {"platforms": body.platforms})
            return _payload(d, clip_id, result.request_id)
        finally:
            d.close()

    @app.post("/woopsocial/batch/plan")
    def plan_batch(body: BatchIn):
        """Work out what a batch WOULD do. Creates and publishes nothing.

        This is the half of batch publishing the assistant is allowed to
        call. It resolves the clips, the platforms and the exact time each
        post would land, and hands that back for a person to look at and
        agree to — the same shape as /publish/plan for YouTube, and for the
        same reason: an upload cannot be taken back, so a model may describe
        one but never start one.
        """
        from datetime import datetime, timedelta
        from datetime import timezone as tz

        d = db()
        try:
            _guard(d)
            clip_ids = list(body.clip_ids)
            if not clip_ids:
                raise HTTPException(400, "No clips were chosen.")
            platforms = body.platforms or ["youtube"]

            start = datetime.now(tz.utc) + timedelta(minutes=2)
            if body.start_at:
                try:
                    start = datetime.fromisoformat(body.start_at.replace("Z", "+00:00"))
                except ValueError as e:
                    raise HTTPException(400, "That start time could not be read.") from e

            items, warnings = [], []
            for index, clip_id in enumerate(clip_ids):
                clip = d.get_clip(clip_id)
                if clip is None:
                    warnings.append(f"Clip {clip_id} no longer exists.")
                    continue
                if not (clip["path"] and Path(clip["path"]).exists()):
                    warnings.append(f"Clip {clip_id} has not been rendered yet.")
                    continue
                at = start + timedelta(hours=body.every_hours * index)
                items.append(
                    {
                        "clip_id": clip_id,
                        "title": (clip["title"] or clip["hook"] or f"Clip {clip_id}").strip(),
                        "publish_at": at.isoformat() if body.every_hours or body.start_at else "",
                    }
                )
            profile = _profile(d, body.account_id or service.selected_account_id(d))
            if body.per_day:
                # The same slots publish_clips will use, so the plan a person
                # agrees to is the schedule that is sent.
                slots = service.schedule_times(
                    d, len(items), per_day=body.per_day, gap_hours=body.gap_hours,
                    start_at=body.start_at, account_id=profile["id"],
                )
                for item, when in zip(items, slots, strict=False):
                    item["publish_at"] = when

            connected: list[str] = []
            try:
                client = _client_for(profile["id"])
                connected = client.connected_platforms(_project(d, client, profile["id"]))
            except (HTTPException, PublishError):
                # Worth planning anyway — the person may be about to connect.
                warnings.append("Could not check which accounts are connected.")
            if connected:
                for p in platforms:
                    if p not in connected:
                        warnings.append(f"{p} is not connected yet and would be skipped.")

            return {
                "provider": "woopsocial",
                "account_id": profile["id"],
                "account_name": profile["name"],
                "platforms": platforms,
                "every_hours": body.every_hours,
                "per_day": body.per_day,
                "gap_hours": body.gap_hours,
                "hashtags": [h.lstrip("#").strip() for h in body.hashtags if h.strip()],
                "items": items,
                "warnings": warnings,
            }
        finally:
            d.close()

    @app.post("/woopsocial/batch")
    def publish_batch(body: BatchIn):
        """Publish several clips, spaced out over time.

        The loop itself lives in woopsocial_service.publish_clips, because the
        worker needs the same behaviour when a job was queued with "publish
        when done" (server/jobs.py). Two copies would drift.
        """
        from publish.uploadpost import validate_schedule

        if not body.clip_ids:
            raise HTTPException(400, "No clips were chosen.")
        if not body.platforms:
            raise HTTPException(400, "Pick at least one platform.")
        if body.start_at:
            try:
                validate_schedule(body.start_at)
            except PublishError as e:
                raise _fail(e) from e

        d = db()
        try:
            _guard(d)
            try:
                post_links.validate_destination(d, body.discord_destination_id)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            # No picker in the request means the key chosen in Settings, the
            # same default the single-clip route has always used.
            account_id = body.account_id or service.selected_account_id(d)
            try:
                return service.publish_clips(
                    d,
                    data_path,
                    clip_ids=body.clip_ids,
                    platforms=body.platforms,
                    hashtags=body.hashtags,
                    every_hours=body.every_hours,
                    start_at=body.start_at,
                    overrides=body.overrides,
                    exclude=body.exclude,
                    per_day=body.per_day,
                    gap_hours=body.gap_hours,
                    discord_destination_id=body.discord_destination_id,
                    account_id=account_id,
                    social_account_ids=body.social_account_ids,
                )
            except PublishError as e:
                raise _fail(e) from e
        finally:
            d.close()

    @app.post("/woopsocial/refresh")
    def refresh_all():
        """Catch up on every publish still in the air, for every named key.

        A batch is accepted in one go and then delivered at WoopSocial's own
        pace, so without this the rows stay at "processing" and there is no
        way to tell a slow queue from a failure. Each key asks only about the
        posts it sent: a request id used by two keys is two separate posts.
        """
        d = db()
        try:
            _guard(d)
            totals = {"checked": 0, "updated": 0, "still_waiting": 0, "failed": 0}
            for account_id in service.refresh_account_ids(d):
                try:
                    # The newest 60 per key, as this has always checked; the
                    # background refresh is the one that picks the due ones.
                    out = service.refresh_in_flight(
                        d, data_path, account_id=account_id,
                        ids=service.in_flight(d, account_id=account_id)[:60],
                    )
                except PublishError as e:
                    raise _fail(e) from e
                for key in totals:
                    totals[key] += out.get(key, 0)
            return totals
        finally:
            d.close()

    @app.get("/woopsocial/schedule")
    def schedule():
        """Everything with a time on it, soonest first."""
        d = db()
        try:
            _guard(d)
            return {"posts": service.upcoming(d)}
        finally:
            d.close()

    @app.get("/woopsocial/in-flight")
    def in_flight_count():
        """How much is still unfinished, without asking WoopSocial."""
        d = db()
        try:
            return {"request_ids": service.in_flight(d)}
        finally:
            d.close()

    @app.post("/woopsocial/refresh/{post_id}")
    def refresh(post_id: str):
        from publish.woopsocial import WoopSocialPublisher

        d = db()
        try:
            _guard(d)
            rows = d.publishes_for_request(post_id)
            if not rows:
                raise HTTPException(404, "no such publish")
            clip_id = int(rows[0]["clip_id"])
            # The same request id can exist under two named keys. Ask each
            # key about only the rows it made, or one account's status would
            # be written onto another account's post.
            groups: dict[str, list] = {}
            for row in rows:
                groups.setdefault(str(row["account_id"] or ""), []).append(row)
            updated = failed = 0
            for account_id, group in groups.items():
                # A blank id is a row from before names existed: it was sent
                # with the original key, whatever Settings now has selected.
                key_id = account_id or service.DEFAULT_ACCOUNT_ID
                profile = _profile(d, key_id)
                client = _client_for(profile["id"])
                publisher = WoopSocialPublisher(
                    client, _project(d, client, profile["id"])
                )
                try:
                    result = publisher.check(post_id)
                except PublishError:
                    failed += 1
                    continue
                if result.outcomes:
                    # With this request's rows, so a platform sent again since
                    # under a newer request keeps that request's row.
                    if service.record_refresh(d, clip_id, result, known=group,
                                              account_id=account_id):
                        updated += 1
            return {**_payload(d, clip_id, post_id), "updated": updated, "failed": failed}
        finally:
            d.close()

    def _payload(d, clip_id: int, request_id: str) -> dict:
        rows = [dict(r) for r in d.clip_publishes(clip_id)]
        live = [r for r in rows if r.get("state") in ("queued", "processing")]
        return {
            "clip_id": clip_id,
            "request_id": request_id,
            "platforms": rows,
            "done": not live,
        }
