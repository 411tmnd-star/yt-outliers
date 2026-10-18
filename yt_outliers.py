#!/usr/bin/env python3
"""yt-outliers: find the YouTube videos that beat their own channel's normal.

For each channel: list its uploads, drop Shorts, and compare every video's views with the
typical views of the channel's *other* videos (median by default). A video above
MULTIPLE x that baseline is an outlier: a topic or packaging that worked unusually well.

    python yt_outliers.py --dry-run                                 # bundled sample data, no key needed
    YOUTUBE_API_KEY=... python yt_outliers.py --channels UCxxxx UCyyyy
    python yt_outliers.py --channels-file channels.txt --multiple 3 --since-days 90 --csv all.csv

Writes OUT/digest.md, one OUT/<video id>.md snapshot per outlier (front matter: url,
retrieved_at, status) and keeps every video seen in a rows file, keyed by video id, so a
re-run updates view counts instead of duplicating them. Standard library only.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "1.0.0"
API = "https://www.googleapis.com/youtube/v3"
DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")  # ISO 8601, e.g. PT1H2M3S
SHORT_SECONDS = 60  # under a minute is treated as a Short
GENERATOR = "yt-outliers"  # marks the files this tool may replace in OUT
HERE = Path(__file__).resolve().parent


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- YouTube Data API v3
def seconds(iso):
    """ISO 8601 duration -> seconds, or None if it does not parse (live streams report P0D)."""
    m = DURATION.match(iso or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def make_get(key, opener=urllib.request.urlopen):
    """Return get(resource, **params) bound to an API key; errors become ApiError with Google's message."""
    def get(resource, **params):
        url = f"{API}/{resource}?{urllib.parse.urlencode({**params, 'key': key})}"
        try:
            with opener(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e)["error"]["message"]
            except Exception:
                msg = e.reason
            raise ApiError(f"{resource}: HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{resource}: network error: {e.reason}") from None
    return get


def uploads_playlist(channel_id, get):
    items = get("channels", part="contentDetails,snippet", id=channel_id).get("items", [])
    if not items:
        raise ApiError(f"channel not found: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def fetch_channel(channel_id, get, max_videos=50):
    """Newest uploads first, following page tokens until max_videos; then full stats 50 ids per call."""
    playlist, ids, token = uploads_playlist(channel_id, get), [], None
    while len(ids) < max_videos:
        params = dict(part="contentDetails", playlistId=playlist, maxResults=min(50, max_videos - len(ids)))
        if token:
            params["pageToken"] = token
        page = get("playlistItems", **params)
        ids += [i["contentDetails"]["videoId"] for i in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    items = []
    for k in range(0, len(ids), 50):
        items += get("videos", part="snippet,contentDetails,statistics", id=",".join(ids[k:k + 50])).get("items", [])
    return items


# ---------------------------------------------------------------- rows
def to_row(item, fetched_at):
    sn, st = item["snippet"], item.get("statistics", {})
    return {"id": item["id"], "title": sn["title"], "channel_id": sn["channelId"], "channel": sn["channelTitle"],
            "published": sn["publishedAt"], "duration_s": seconds(item["contentDetails"].get("duration")),
            "views": int(st.get("viewCount", 0)), "likes": int(st["likeCount"]) if "likeCount" in st else None,
            "comments": int(st["commentCount"]) if "commentCount" in st else None, "fetched_at": fetched_at}


def is_short(row):
    return row["duration_s"] is None or row["duration_s"] < SHORT_SECONDS


def load_rows(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def upsert(old_rows, new_rows):
    """Keyed by video id: newer data replaces older, nothing is duplicated, order is stable."""
    merged = {r["id"]: r for r in old_rows}
    for r in new_rows:
        merged[r["id"]] = r
    return list(merged.values())


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- outliers
def published_date(row):
    return dt.date.fromisoformat(row["published"][:10])


def flag(rows, multiple=2.0, baseline="median", scope="channel", min_others=2, since=None):
    """Return (outliers, skipped_channels).

    Each video is compared with its peers *excluding itself*, so one viral video cannot raise
    its own bar. scope="channel" uses the same channel's videos; "all" uses every row.
    Channels with fewer than min_others peers per video are not judged and are reported instead.
    since (a date) limits which videos may be flagged; baselines still use every row.
    """
    pick = statistics.median if baseline == "median" else statistics.fmean
    groups = {}
    for r in rows:
        groups.setdefault(r["channel_id"] if scope == "channel" else "*", []).append(r)
    outliers, skipped = [], {}
    for members in groups.values():
        if len(members) - 1 < min_others:
            for r in members:
                skipped[r["channel"]] = len(members)
            continue
        for r in members:
            if since and published_date(r) < since:
                continue
            base = pick([o["views"] for o in members if o["id"] != r["id"]])
            if base > 0 and r["views"] > multiple * base:
                outliers.append({**r, "baseline": round(base), "multiple": round(r["views"] / base, 2)})
    return sorted(outliers, key=lambda r: -r["multiple"]), skipped


# ---------------------------------------------------------------- outputs
def url_of(row):
    return f"https://www.youtube.com/watch?v={row['id']}"


def write_outputs(outliers, skipped, out, today, settings):
    """Replace only files an earlier run of this tool wrote; anything else in OUT is left alone."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.md"):
        head = old.read_text(encoding="utf-8", errors="replace")[:300]
        if f"generator: {GENERATOR}" in head:
            old.unlink()
    lines = ["---", f"generator: {GENERATOR}", f"retrieved_at: {today}", "---",
             f"# Outlier digest {today}", "", f"Settings: {settings}", ""]
    if not outliers:
        lines.append("No outliers this run.")
    for f in outliers:
        (out / f"{f['id']}.md").write_text(
            f"---\ngenerator: {GENERATOR}\nurl: {url_of(f)}\nretrieved_at: {today}\nstatus: needs-review\n---\n"
            f"# {f['title']}\n\n- channel: {f['channel']}\n- published: {f['published']}\n"
            f"- duration_s: {f['duration_s']}\n- views: {f['views']}\n- likes: {f['likes']}\n"
            f"- comments: {f['comments']}\n- baseline views (other videos): {f['baseline']}\n"
            f"- multiple: {f['multiple']}x\n", encoding="utf-8")
        lines.append(f"- **{f['multiple']}x** [{f['title']}]({url_of(f)}) — {f['channel']}, "
                     f"{f['views']:,} views vs {f['baseline']:,} typical")
    if skipped:
        lines += ["", "Not judged (too few long-form videos to set a baseline):"]
        lines += [f"- {name}: {n} video(s)" for name, n in sorted(skipped.items())]
    (out / "digest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(rows, outliers, path):
    hits = {o["id"]: o for o in outliers}
    cols = ["id", "url", "title", "channel", "published", "duration_s", "views", "likes", "comments",
            "fetched_at", "baseline", "multiple"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda r: -r["views"]):
            w.writerow({**r, "url": url_of(r), **{k: hits.get(r["id"], {}).get(k, "") for k in ("baseline", "multiple")}})


# ---------------------------------------------------------------- CLI
def read_channels(args):
    chans = list(args.channels)
    if args.channels_file:
        for line in Path(args.channels_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                chans.append(line)
    return chans


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="yt-outliers", description=__doc__.split("\n\n")[0])
    ap.add_argument("--channels", nargs="*", default=[], metavar="ID", help="channel ids (UC...)")
    ap.add_argument("--channels-file", help="text file, one channel id per line, # comments allowed")
    ap.add_argument("--multiple", type=float, default=2.0, help="flag views above this x baseline (default 2)")
    ap.add_argument("--baseline", choices=["median", "mean"], default="median")
    ap.add_argument("--scope", choices=["channel", "all"], default="channel",
                    help="compare with the same channel (default) or with every video")
    ap.add_argument("--min-others", type=int, default=2, help="peers a video needs before it is judged")
    ap.add_argument("--since-days", type=int, help="only flag videos published in the last N days")
    ap.add_argument("--max-videos", type=int, default=50, help="newest uploads read per channel")
    ap.add_argument("--include-shorts", action="store_true")
    ap.add_argument("--out", help="output folder (default outliers_out; outliers_out_sample with --dry-run)")
    ap.add_argument("--rows", help="rows file (default OUT/rows.json)")
    ap.add_argument("--csv", help="also write every video (outliers marked) to this CSV")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the bundled sample data; no API key, no network, and its own output folder")
    ap.add_argument("--today", help=argparse.SUPPRESS)  # fixed date for tests
    ap.add_argument("--version", action="version", version=__version__)
    return ap.parse_args(argv)


def main(argv=None, get=None):
    a = parse_args(argv)
    a.out = a.out or ("outliers_out_sample" if a.dry_run else "outliers_out")  # sample data never mixes with real
    a.rows = a.rows or str(Path(a.out) / "rows.json")
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if a.dry_run:
        items = json.loads((HERE / "fixtures" / "videos.json").read_text(encoding="utf-8"))
    else:
        chans = read_channels(a)
        if not chans:
            print("give --channels or --channels-file (or --dry-run to try the sample data)", file=sys.stderr)
            return 2
        if get is None:
            key = os.environ.get("YOUTUBE_API_KEY")
            if not key:
                print("set YOUTUBE_API_KEY (a YouTube Data API v3 key) or use --dry-run", file=sys.stderr)
                return 2
            get = make_get(key)
        items, failed = [], 0
        for c in chans:
            try:
                got = fetch_channel(c, get, a.max_videos)
                items += got
                print(f"{c}: {len(got)} videos")
            except ApiError as e:
                failed += 1
                print(f"{c}: skipped ({e})", file=sys.stderr)
        if failed == len(chans):
            return 1
    rows = [to_row(i, now) for i in items]
    kept = rows if a.include_shorts else [r for r in rows if not is_short(r)]
    print(f"{len(rows)} videos read, {len(rows) - len(kept)} Shorts/unparseable dropped")
    rows_path = Path(a.rows)
    all_rows = upsert(load_rows(rows_path), kept)
    save_rows(rows_path, all_rows)
    since = today - dt.timedelta(days=a.since_days) if a.since_days else None
    outliers, skipped = flag(all_rows, a.multiple, a.baseline, a.scope, a.min_others, since)
    settings = (f"{a.baseline} of other videos, scope {a.scope}, above {a.multiple:g}x"
                + (f", published since {since}" if since else ""))
    write_outputs(outliers, skipped, Path(a.out), today.isoformat(), settings)
    if a.csv:
        write_csv(all_rows, outliers, a.csv)
    print(f"{len(outliers)} outlier(s) -> {Path(a.out) / 'digest.md'}")
    for f in outliers:
        print(f"  {f['multiple']}x  {f['title']}  ({f['channel']})")
    for name, n in sorted(skipped.items()):
        print(f"  not judged: {name} ({n} video(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
