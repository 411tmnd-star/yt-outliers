# yt-outliers

Find the YouTube videos that beat their own channel's normal: the topics and packaging that worked unusually well. Use them to decide what to make or what to advertise next.

```
$ python yt_outliers.py --dry-run
13 videos read, 2 Shorts/unparseable dropped
2 outlier(s) -> outliers_out_sample/digest.md
  7.13x  I styled 1 shirt 7 ways  (Lagos Thrift Hauls)
  6.86x  Street style at the Garden City mall  (Street Style PH)
  not judged: Tailor Talk (2 video(s))
```

One Python file, standard library only, no API key needed for the demo. The sample channels in `fixtures/videos.json` are invented. A `--dry-run` writes to its own `outliers_out_sample/` folder, so sample data never mixes with your real results.

## How it decides

For each channel it reads the newest uploads, drops Shorts (under 60 s) and live-stream replays, then compares every video with **the median views of the channel's other videos**.

- **The video is left out of its own baseline.** With a plain average, one viral video raises the bar it is measured against: the 31,000-view video above would be only 3.2x a mean of 9,740 that includes itself. Against the median of its peers (4,350) it is 7.13x.
- **Median, not mean, by default**, so a second hit doesn't hide the first. `--baseline mean` is available.
- **Small channels are reported, not guessed.** A channel needs at least two other long-form videos (`--min-others`) before any of its videos is judged.
- `--scope all` compares against every video from every channel instead of the same channel.
- `--since-days 90` flags only recent videos; older ones still count towards the baseline.

## Output

| File | Contents |
|---|---|
| `outliers_out/digest.md` | Ranked list: multiple, title, link, views vs typical, the settings used, channels not judged |
| `outliers_out/<video id>.md` | One snapshot per outlier with front matter (`url`, `retrieved_at`, `status: needs-review`) for human review |
| `outliers_out/rows.json` | Every video seen, keyed by id. Re-runs update view counts and never duplicate |
| `--csv all.csv` | Every video with outliers marked, for a spreadsheet |

Only files this tool wrote (front matter `generator: yt-outliers`) are replaced on a re-run. Anything else in the output folder is left alone.

## Real channels

1. Create a YouTube Data API v3 key in the Google Cloud console.
2. Run:

```
export YOUTUBE_API_KEY=...            # Windows: set YOUTUBE_API_KEY=...
python yt_outliers.py --channels UCxxxxxxxxxxxxxxxxxxxxxx UCyyyyyyyyyyyyyyyyyyyyyy
python yt_outliers.py --channels-file channels.txt --multiple 3 --since-days 90 --csv all.csv
```

`channels.txt` holds one channel id (`UC…`) per line, and `#` starts a comment. Quota: each channel costs about 3 units for 50 videos (one `channels.list`, one `playlistItems.list` per 50 videos, one `videos.list` per 50 videos). Every one of those calls costs 1 unit against the default 10,000 a day ([Google's quota table](https://developers.google.com/youtube/v3/determine_quota_cost), checked 2026-10-18). A channel that fails, for example a wrong id, is skipped with Google's error message, and the run continues.

## Tests

```
python -m unittest discover -s tests -v
```

21 tests, no network: the API is replaced by a fake that pages like the real one. They cover duration parsing, leave-one-out baselines, small-channel handling, the date filter, upserts, paging and 50-id batching, error messages, and output files that must survive a re-run. CI runs them on Ubuntu and Windows with Python 3.11 and 3.13.

## Limits

- It uses views only. Views per day since publishing would be fairer to new videos and isn't implemented yet.
- Shorts are detected by duration (under 60 s), not by YouTube's own label, which the API does not expose.
- Channel ids only. @handles aren't resolved yet.
