"""YouTube API köməkçisi: kanal həkimi (channel-doctor) üçün.

Əmrlər:
  python tools/yt_api.py auth <kanal>           # OAuth girişi, secrets/token_<kanal>.json yaradır
  python tools/yt_api.py whoami <kanal>         # token hansı kanala bağlıdır
  python tools/yt_api.py backup <kanal>         # bütün videoların metadata-sını analysis/backup/ qovluğuna yazır
  python tools/yt_api.py apply <kanal>          # analysis/fixes/fixes-<kanal>.json: YALNIZ fərqi göstərir (dry-run)
  python tools/yt_api.py apply <kanal> --confirm   # istifadəçi təsdiqindən sonra YouTube-a tətbiq edir

<kanal>: big_bass | piano_rain | flute_handpan
Kvota: videos.list = 1 vahid, videos.update = 50, channels.update = 50 (gündəlik limit 10 000).
"""
import json
import os
import sys
import datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
BACKUP = ROOT / "analysis" / "backup"
FIXES = ROOT / "analysis" / "fixes"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

CHANNELS = {
    "big_bass": "@MidnightGasUltraBassboosted",
    "piano_rain": "@SleepPianoRain432Hz",
    "flute_handpan": "@AetherPanFluteMeditationSleep",
}

VIDEO_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


def die(msg):
    print(f"XƏTA: {msg}")
    sys.exit(1)


def token_path(ch):
    return SECRETS / f"token_{ch}.json"


def save_token(tp, creds):
    """Tokeni yalnız sahibin oxuya biləcəyi şəkildə yazır (0600)."""
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(str(tp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    os.chmod(tp, 0o600)


def get_creds(ch, interactive=False):
    tp = token_path(ch)
    creds = Credentials.from_authorized_user_file(str(tp), SCOPES) if tp.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token(tp, creds)
    if creds and creds.valid:
        return creds
    if not interactive:
        die(f"{ch} üçün token yoxdur. Əvvəlcə: python tools/yt_api.py auth {ch}")
    cs = SECRETS / "client_secret.json"
    if not cs.exists():
        die(f"{cs} tapılmadı. Google Cloud-dan yüklədiyin OAuth (Desktop app) JSON faylını bura qoy.")
    print(f"\nBrauzer açılacaq. Google hesabını seçəndən sonra MÜTLƏQ bu kanalı seç: {CHANNELS[ch]}\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(cs), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent select_account", timeout_seconds=900)
    save_token(tp, creds)
    return creds


def yt(ch, interactive=False):
    return build("youtube", "v3", credentials=get_creds(ch, interactive), cache_discovery=False)


def my_channel(svc):
    r = svc.channels().list(part="id,snippet,brandingSettings,contentDetails", mine=True).execute()
    items = r.get("items", [])
    if not items:
        die("Bu token heç bir kanala bağlı deyil.")
    return items[0]


def check_channel(ch, svc):
    c = my_channel(svc)
    handle = c["snippet"].get("customUrl", "")
    if handle.lower() != CHANNELS[ch].lower():
        die(f"Token '{handle}' kanalına bağlıdır, gözlənilən {CHANNELS[ch]}. "
            f"secrets/token_{ch}.json faylını sil və auth-u təkrarla, düzgün kanalı seç.")
    return c


def all_video_ids(svc, uploads_pl):
    ids, tok = [], None
    while True:
        r = svc.playlistItems().list(part="contentDetails", playlistId=uploads_pl,
                                     maxResults=50, pageToken=tok).execute()
        ids += [i["contentDetails"]["videoId"] for i in r["items"]]
        tok = r.get("nextPageToken")
        if not tok:
            return ids


def get_videos(svc, ids):
    out = []
    for i in range(0, len(ids), 50):
        r = svc.videos().list(part="snippet,status,contentDetails", id=",".join(ids[i:i + 50])).execute()
        out += r["items"]
    return out


def cmd_auth(ch):
    tp = token_path(ch)
    if tp.exists():  # həmişə yeni giriş: köhnə token .old kimi saxlanılır
        tp.replace(tp.with_suffix(".json.old"))
    try:
        svc = yt(ch, interactive=True)
    except BaseException as e:  # vaxt bitdi / ləğv edildi: köhnə token geri qaytarılır
        old = tp.with_suffix(".json.old")
        if old.exists() and not tp.exists():
            old.replace(tp)
        die(f"Giriş tamamlanmadı ({type(e).__name__}). Köhnə token saxlanıldı. Əmri təkrarla.")
    c = my_channel(svc)
    if c["snippet"].get("customUrl", "").lower() != CHANNELS[ch].lower():
        tp.unlink(missing_ok=True)
        old = tp.with_suffix(".json.old")
        if old.exists():
            old.replace(tp)
        die(f"Yanlış kanal seçildi: {c['snippet'].get('customUrl')}. Gözlənilən {CHANNELS[ch]}. Əmri təkrarla və düzgün kanalı seç.")
    print(f"OK: {ch} -> {c['snippet']['title']} ({c['snippet'].get('customUrl')})")


def cmd_whoami(ch):
    c = my_channel(yt(ch))
    print(json.dumps({"id": c["id"], "title": c["snippet"]["title"],
                      "handle": c["snippet"].get("customUrl")}, ensure_ascii=False, indent=2))


def cmd_backup(ch):
    svc = yt(ch)
    c = check_channel(ch, svc)
    ids = all_video_ids(svc, c["contentDetails"]["relatedPlaylists"]["uploads"])
    vids = get_videos(svc, ids)
    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    f = BACKUP / f"{ch}-{stamp}.json"
    f.write_text(json.dumps({"channel": c, "videos": vids}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Ehtiyat nüsxə: {f} ({len(vids)} video)")
    return f


def cmd_apply(ch, confirm):
    fx = FIXES / f"fixes-{ch}.json"
    if not fx.exists():
        die(f"{fx} tapılmadı.")
    data = json.loads(fx.read_text(encoding="utf-8"))
    videos = data.get("videos", data if isinstance(data, list) else [])
    svc = yt(ch)
    c = check_channel(ch, svc)
    if confirm:
        cmd_backup(ch)  # dəyişiklikdən əvvəl mütləq ehtiyat nüsxə

    current = {v["id"]: v for v in get_videos(svc, [v["videoId"] for v in videos])}
    changed = 0
    for fix in videos:
        vid = fix["videoId"]
        if vid not in current:
            print(f"- {vid}: kanalda tapılmadı, keçildi")
            continue
        sn = dict(current[vid]["snippet"])
        diff = {k: (sn.get(k), fix[k]) for k in VIDEO_FIELDS if k in fix and sn.get(k) != fix[k]}
        if not diff:
            continue
        changed += 1
        print(f"\n== {vid} | {sn.get('title')}")
        for k, (old, new) in diff.items():
            o, n = (json.dumps(x, ensure_ascii=False)[:160] for x in (old, new))
            print(f"  {k}:\n    indi : {o}\n    yeni : {n}")
        if confirm:
            body = {k: sn[k] for k in VIDEO_FIELDS if k in sn}
            body.update({k: fix[k] for k in VIDEO_FIELDS if k in fix})
            try:
                svc.videos().update(part="snippet", body={"id": vid, "snippet": body}).execute()
                print("  -> tətbiq edildi")
            except Exception as e:
                changed -= 1
                print(f"  -> XƏTA, keçildi: {str(e)[:300]}")
                if "quotaExceeded" in str(e):
                    die("Gündəlik kvota bitdi. Qalanını sabah davam etdir.")

    chfix = data.get("channel") if isinstance(data, dict) else None
    if chfix:
        bs = c.get("brandingSettings", {})
        cur = bs.get("channel", {})
        new = {k: chfix[k] for k in ("description", "keywords") if k in chfix and cur.get(k) != chfix[k]}
        if new:
            changed += 1
            print("\n== KANAL")
            for k, v in new.items():
                print(f"  {k}:\n    indi : {str(cur.get(k))[:160]}\n    yeni : {str(v)[:160]}")
            if confirm:
                cur.update(new)
                svc.channels().update(part="brandingSettings",
                                      body={"id": c["id"], "brandingSettings": {"channel": cur}}).execute()
                print("  -> tətbiq edildi")

    units = changed * 50
    if confirm:
        print(f"\nHazır: {changed} dəyişiklik tətbiq edildi (~{units} kvota vahidi).")
    else:
        print(f"\nDRY-RUN: {changed} dəyişiklik olacaq (~{units} kvota vahidi). "
              f"YouTube-da heç nə dəyişdirilmədi. Tətbiq etmək üçün: --confirm")


def main():
    a = sys.argv[1:]
    if len(a) < 2 or a[1] not in CHANNELS:
        print(__doc__)
        sys.exit(1)
    cmd, ch = a[0], a[1]
    if cmd == "auth":
        cmd_auth(ch)
    elif cmd == "whoami":
        cmd_whoami(ch)
    elif cmd == "backup":
        cmd_backup(ch)
    elif cmd == "apply":
        cmd_apply(ch, "--confirm" in a)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
