"""Tests for yt_outliers. No network: the API is replaced by fakes. Run: python -m unittest -v"""
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yt_outliers as yo  # noqa: E402


def row(i, views, ch="UCa", published="2026-09-01", dur=600):
    return {"id": i, "title": f"T{i}", "channel_id": ch, "channel": f"Chan {ch}", "published": published + "T00:00:00Z",
            "duration_s": dur, "views": views, "likes": None, "comments": None, "fetched_at": "x"}


def item(i, ch="UCa", views=1000, dur="PT10M"):
    return {"id": i, "snippet": {"title": f"T{i}", "channelId": ch, "channelTitle": f"Chan {ch}",
                                 "publishedAt": "2026-09-01T00:00:00Z"},
            "contentDetails": {"duration": dur}, "statistics": {"viewCount": str(views)}}


class FakeApi:
    """Answers channels / playlistItems (paged, 50 a page) / videos like the Data API, and records calls."""

    def __init__(self, n_videos=120, missing=()):
        self.n, self.missing, self.calls = n_videos, set(missing), []

    def __call__(self, resource, **p):
        self.calls.append((resource, p))
        if resource == "channels":
            if p["id"] in self.missing:
                return {"items": []}
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU" + p["id"][2:]}}}]}
        if resource == "playlistItems":
            start = int(p.get("pageToken", 0))
            ids = list(range(start, min(start + p["maxResults"], self.n)))
            page = {"items": [{"contentDetails": {"videoId": f"v{k}"}} for k in ids]}
            if ids and ids[-1] + 1 < self.n:
                page["nextPageToken"] = str(ids[-1] + 1)
            return page
        if resource == "videos":
            ids = p["id"].split(",")
            assert len(ids) <= 50
            return {"items": [item(i, views=1000 if i != "v3" else 50000) for i in ids]}
        raise AssertionError(resource)


class Durations(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(yo.seconds("PT1H2M3S"), 3723)
        self.assertEqual(yo.seconds("PT45S"), 45)
        self.assertEqual(yo.seconds("PT10M"), 600)
        self.assertEqual(yo.seconds("P1DT1S"), 86401)
        self.assertEqual(yo.seconds("P0D"), 0)  # live streams
        self.assertIsNone(yo.seconds("P"))
        self.assertIsNone(yo.seconds(None))
        self.assertIsNone(yo.seconds("10:00"))

    def test_shorts(self):
        self.assertTrue(yo.is_short(row("a", 1, dur=59)))
        self.assertFalse(yo.is_short(row("a", 1, dur=60)))
        self.assertTrue(yo.is_short(row("a", 1, dur=None)))


class Flagging(unittest.TestCase):
    rows = [row("a", 1000), row("b", 1200), row("c", 1100), row("hit", 9000)]

    def test_video_is_not_part_of_its_own_baseline(self):
        out, _ = yo.flag(self.rows, 2)
        self.assertEqual([o["id"] for o in out], ["hit"])
        self.assertEqual(out[0]["baseline"], 1100)  # median of 1000, 1200, 1100
        self.assertEqual(out[0]["multiple"], 8.18)

    def test_mean_baseline(self):
        out, _ = yo.flag(self.rows, 2, baseline="mean")
        self.assertEqual(out[0]["baseline"], 1100)

    def test_one_viral_video_does_not_hide_another(self):
        rows = self.rows + [row("hit2", 8000)]
        self.assertEqual({o["id"] for o in yo.flag(rows, 2)[0]}, {"hit", "hit2"})

    def test_threshold_is_strict(self):
        rows = [row("a", 100), row("b", 100), row("c", 200)]
        self.assertEqual(yo.flag(rows, 2)[0], [])

    def test_small_channels_are_reported_not_judged(self):
        rows = self.rows + [row("x", 10, ch="UCb"), row("y", 99999, ch="UCb")]
        out, skipped = yo.flag(rows, 2)
        self.assertNotIn("y", [o["id"] for o in out])
        self.assertEqual(skipped, {"Chan UCb": 2})

    def test_channels_are_compared_separately_unless_scope_all(self):
        rows = [row(f"a{i}", 1000) for i in range(3)] + [row(f"b{i}", 100000, ch="UCb") for i in range(3)]
        self.assertEqual(yo.flag(rows, 2)[0], [])
        # against every video, the big channel's normal videos look like outliers (others' median is 1000)
        self.assertEqual({o["id"] for o in yo.flag(rows, 2, scope="all")[0]}, {"b0", "b1", "b2"})

    def test_since_limits_flags_but_not_baselines(self):
        rows = [row("old", 9000, published="2026-01-01")] + self.rows[:3]
        self.assertEqual(yo.flag(rows, 2, since=dt.date(2026, 6, 1))[0], [])
        self.assertEqual(len(yo.flag(rows, 2)[0]), 1)

    def test_zero_baseline_never_divides(self):
        rows = [row("a", 0), row("b", 0), row("c", 5)]
        self.assertEqual(yo.flag(rows, 2)[0], [])


class Rows(unittest.TestCase):
    def test_upsert_updates_without_duplicates(self):
        merged = yo.upsert([row("a", 1), row("b", 2)], [row("b", 20), row("c", 3)])
        self.assertEqual([(r["id"], r["views"]) for r in merged], [("a", 1), ("b", 20), ("c", 3)])

    def test_to_row_reads_optional_stats(self):
        it = item("z", views=5)
        it["statistics"]["likeCount"] = "7"
        r = yo.to_row(it, "now")
        self.assertEqual((r["views"], r["likes"], r["comments"], r["duration_s"]), (5, 7, None, 600))


class Api(unittest.TestCase):
    def test_pages_through_uploads_and_batches_video_calls(self):
        api = FakeApi(n_videos=120)
        items = yo.fetch_channel("UCabc", api, max_videos=120)
        self.assertEqual(len(items), 120)
        pages = [p for r, p in api.calls if r == "playlistItems"]
        self.assertEqual([p.get("pageToken") for p in pages], [None, "50", "100"])
        self.assertTrue(all(p["playlistId"] == "UUabc" for p in pages))
        self.assertEqual([len(p["id"].split(",")) for r, p in api.calls if r == "videos"], [50, 50, 20])

    def test_stops_at_max_videos(self):
        api = FakeApi(n_videos=500)
        self.assertEqual(len(yo.fetch_channel("UCabc", api, max_videos=60)), 60)

    def test_unknown_channel_raises(self):
        with self.assertRaises(yo.ApiError):
            yo.fetch_channel("UCnope", FakeApi(missing={"UCnope"}))

    def test_http_error_keeps_googles_message(self):
        def opener(url, timeout):
            self.assertIn("key=SECRET", url)
            body = io.BytesIO(json.dumps({"error": {"message": "quota exceeded"}}).encode())
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, body)
        with self.assertRaisesRegex(yo.ApiError, "HTTP 403: quota exceeded"):
            yo.make_get("SECRET", opener)("videos", id="x")


class Cli(unittest.TestCase):
    def run_main(self, *args, get=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = yo.main(list(args), get=get)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            o = Path(t) / "out"
            o.mkdir()
            (o / "my-notes.md").write_text("keep me")  # not written by the tool: must survive
            (o / "stale.md").write_text("---\ngenerator: yt-outliers\n---\nold")
            rc, out, _ = self.run_main("--dry-run", "--out", str(o), "--rows", str(o / "rows.json"),
                                       "--csv", str(Path(t) / "all.csv"), "--today", "2026-10-18")
            self.assertEqual(rc, 0)
            self.assertIn("13 videos read, 2 Shorts/unparseable dropped", out)
            self.assertTrue((o / "my-notes.md").exists())
            self.assertFalse((o / "stale.md").exists())
            self.assertEqual(sorted(p.name for p in o.glob("*.md")), ["digest.md", "lt004.md", "my-notes.md", "ss001.md"])
            digest = (o / "digest.md").read_text(encoding="utf-8")
            self.assertIn("7.13x", digest)
            self.assertIn("Tailor Talk: 2 video(s)", digest)
            snap = (o / "lt004.md").read_text(encoding="utf-8")
            self.assertIn("url: https://www.youtube.com/watch?v=lt004", snap)
            self.assertIn("status: needs-review", snap)
            with open(Path(t) / "all.csv", encoding="utf-8-sig") as f:
                table = list(csv.DictReader(f))
            self.assertEqual(len(table), 11)
            self.assertEqual([r["id"] for r in table if r["multiple"]], ["ss001", "lt004"])

            rc, out, _ = self.run_main("--dry-run", "--out", str(o), "--rows", str(o / "rows.json"))
            self.assertEqual(len(json.loads((o / "rows.json").read_text())), 11)  # re-run: no duplicates

    def test_dry_run_never_touches_the_real_output_folder(self):
        with tempfile.TemporaryDirectory() as t:
            old = os.getcwd()
            os.chdir(t)
            try:
                rc, _, _ = self.run_main("--dry-run", "--today", "2026-10-18")
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.exists("outliers_out_sample/digest.md"))
                self.assertTrue(os.path.exists("outliers_out_sample/rows.json"))
                self.assertFalse(os.path.exists("outliers_out"))
                # a real run afterwards starts from nothing, not from the sample videos
                rc, _, _ = self.run_main("--channels", "UCgood", "--today", "2026-10-18", get=FakeApi(n_videos=10))
                self.assertEqual(rc, 0)
                rows = json.loads(Path("outliers_out/rows.json").read_text())
                self.assertEqual({r["channel_id"] for r in rows}, {"UCa"})
                self.assertNotIn("Lagos Thrift Hauls", Path("outliers_out/digest.md").read_text(encoding="utf-8"))
            finally:
                os.chdir(old)

    def test_since_days(self):
        with tempfile.TemporaryDirectory() as t:
            rc, out, _ = self.run_main("--dry-run", "--out", t, "--rows", os.path.join(t, "r.json"),
                                       "--today", "2026-10-18", "--since-days", "90")
            self.assertIn("1 outlier(s)", out)
            self.assertNotIn("Garden City", out)

    def test_live_api_path_with_fake(self):
        with tempfile.TemporaryDirectory() as t:
            ch = Path(t) / "channels.txt"
            ch.write_text("# my list\nUCgood   # main channel\nUCgone\n")
            rc, out, err = self.run_main("--channels-file", str(ch), "--out", t, "--rows", os.path.join(t, "r.json"),
                                         get=FakeApi(n_videos=10, missing={"UCgone"}))
            self.assertEqual(rc, 0)
            self.assertIn("UCgood: 10 videos", out)
            self.assertIn("UCgone: skipped (channel not found: UCgone)", err)
            self.assertIn("50.0x  Tv3", out)

    def test_all_channels_failing_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            rc, _, _ = self.run_main("--channels", "UCx", "--out", t, "--rows", os.path.join(t, "r.json"),
                                     get=FakeApi(missing={"UCx"}))
            self.assertEqual(rc, 1)

    def test_needs_channels_and_key(self):
        self.assertEqual(self.run_main()[0], 2)
        old = os.environ.pop("YOUTUBE_API_KEY", None)
        try:
            rc, _, err = self.run_main("--channels", "UCx")
        finally:
            if old is not None:
                os.environ["YOUTUBE_API_KEY"] = old
        self.assertEqual(rc, 2)
        self.assertIn("YOUTUBE_API_KEY", err)


if __name__ == "__main__":
    unittest.main()
