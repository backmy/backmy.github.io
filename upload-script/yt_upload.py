# -*- coding: utf-8 -*-
"""YT Autopilot - video upload script (YouTube Data API v3).

Uploads ONE video that the channel owner has already reviewed and approved, to the
owner's own channel, then (optionally) sets a custom thumbnail and adds it to a playlist.

Usage:
  python tools/yt_upload.py <channel> <video_file> <meta.json> --certify-community-guidelines [--dry-run]

<channel>: big_bass | ocean_sleep | handpan   (OAuth token: secrets/token_<channel>.json)
meta.json:
  {"title": "...", "description": "...", "tags": ["..."], "categoryId": "10",
   "privacyStatus": "private|unlisted|public", "publishAt": "2026-11-23T08:00:00Z" (optional),
   "thumbnail": "path/to/thumb.jpg" (optional, long videos only), "playlistId": "PL..." (optional),
   "defaultLanguage": "en", "defaultAudioLanguage": "en", "madeForKids": false}

Safety rules built into the script:
  * Nothing is uploaded unless the owner passes --certify-community-guidelines, i.e. the owner
    certifies that the content complies with the YouTube Community Guidelines
    (https://www.youtube.com/howyoutubeworks/policies/community-guidelines/).
  * Only the channel the token belongs to is used; the channel handle is checked first.
  * The description is uploaded exactly as approved; nothing is appended afterwards.
  * Every upload is logged to analysis/uploads.jsonl (video id, time, title).

Quota: videos.insert uses its own bucket (1 call); thumbnails.set 50 units; playlistItems.insert 50 units.
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path

from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yt_api  # noqa: E402  (OAuth helpers: get_creds, yt, check_channel)

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "analysis" / "uploads.jsonl"
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


def validate(meta, video):
    errs = []
    if not Path(video).is_file():
        errs.append(f"video file not found: {video}")
    t = meta.get("title", "")
    if not t or len(t) > 100 or "<" in t or ">" in t:
        errs.append("title must be 1-100 characters without < >")
    if len(meta.get("description", "").encode("utf-8")) > 5000:
        errs.append("description longer than 5000 bytes")
    if sum(len(x) + 2 for x in meta.get("tags", [])) > 500:
        errs.append("tags longer than 500 characters in total")
    if meta.get("privacyStatus", "private") not in ("private", "unlisted", "public"):
        errs.append("privacyStatus must be private, unlisted or public")
    th = meta.get("thumbnail")
    if th and not Path(th).is_file():
        errs.append(f"thumbnail not found: {th}")
    return errs


def resumable_upload(request):
    response, retry = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"  uploaded {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status not in RETRY_STATUS:
                raise
            retry += 1
        except (ConnectionError, TimeoutError, ResumableUploadError):
            retry += 1
        else:
            continue
        if retry > MAX_RETRIES:
            raise SystemExit("ERROR: too many retries, upload aborted")
        wait = min(2 ** retry, 64)
        print(f"  temporary error, retry {retry}/{MAX_RETRIES} in {wait}s")
        time.sleep(wait)
    return response


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", choices=sorted(yt_api.CHANNELS))
    ap.add_argument("video")
    ap.add_argument("meta")
    ap.add_argument("--certify-community-guidelines", action="store_true",
                    help="owner certifies the content complies with the YouTube Community Guidelines")
    ap.add_argument("--dry-run", action="store_true", help="validate only, upload nothing")
    a = ap.parse_args()

    meta = json.loads(Path(a.meta).read_text(encoding="utf-8"))
    errs = validate(meta, a.video)
    if errs:
        raise SystemExit("ERROR:\n  " + "\n  ".join(errs))
    if not a.certify_community_guidelines:
        raise SystemExit("ERROR: upload refused. The owner must certify that the content complies with the "
                         "YouTube Community Guidelines (--certify-community-guidelines).")

    svc = yt_api.yt(a.channel)
    ch = yt_api.check_channel(a.channel, svc)
    print(f"Channel: {ch['snippet']['title']} ({ch['snippet'].get('customUrl')})")
    print(f"Title:   {meta['title']}")
    if a.dry_run:
        print("DRY-RUN: validation passed, nothing uploaded.")
        return

    status = {"privacyStatus": meta.get("privacyStatus", "private"),
              "selfDeclaredMadeForKids": bool(meta.get("madeForKids", False)),
              "embeddable": True, "publicStatsViewable": True, "license": "youtube"}
    if meta.get("publishAt"):
        status["privacyStatus"] = "private"  # required for scheduled publishing
        status["publishAt"] = meta["publishAt"]
    body = {"snippet": {k: meta[k] for k in ("title", "description", "tags", "categoryId",
                                              "defaultLanguage", "defaultAudioLanguage") if k in meta},
            "status": status}
    body["snippet"].setdefault("categoryId", "10")

    media = MediaFileUpload(a.video, chunksize=16 * 1024 * 1024, resumable=True)
    req = svc.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    print("Uploading...")
    video = resumable_upload(req)
    vid = video["id"]
    print(f"OK: https://youtu.be/{vid}  (privacy: {video['status']['privacyStatus']})")

    if meta.get("thumbnail"):
        try:
            svc.thumbnails().set(videoId=vid, media_body=MediaFileUpload(meta["thumbnail"])).execute()
            print("Thumbnail set.")
        except HttpError as e:
            print(f"WARNING: thumbnail not set: {e}")
    if meta.get("playlistId"):
        try:
            svc.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": meta["playlistId"], "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()
            print("Added to playlist.")
        except HttpError as e:
            print(f"WARNING: playlist not updated: {e}")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds"), "channel": a.channel,
                            "videoId": vid, "title": meta["title"],
                            "privacy": video["status"]["privacyStatus"]}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
