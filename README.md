# YouTube Tracker

Track channels, find recent videos, rank candidates using view counts and freshness, and fetch selected videos for local review. The tracker uses Python 3.10+ and its standard library. Run commands from this directory.

## Test offline

```sh
python3 -S -m unittest discover -s tracker/tests -v
```

Tests cover ranking, exclusions, historical growth, API pagination, credentials, and source fetching. They use temporary files and make no network calls.

## Track and rank

Copy `.env.example` to `.env` and set `YOUTUBE_DATA_API_KEY` to a key with YouTube Data API v3 enabled, or export the variable in your shell. Set up the watchlist with an actual channel handle or channel ID.

```sh
python3 tracker/youtube_tracker.py add @YouTubeCreators
python3 tracker/update.py
```

The update refreshes watched channels and writes a ranked report at `tracker/reports/latest.html` and `latest.json`. Refreshes retain view snapshots in SQLite. The ranking defaults to 500,000+ views, uploads in the last 90 days, videos up to 90 seconds and at most three videos per channel. To see more results in an initial test:

```sh
python3 tracker/youtube_tracker.py rank --min-views 0 --max-duration 600 --limit 20
```

Additional commands:

```sh
python3 tracker/youtube_tracker.py discover --query 'wildlife rescue short' --days 60
python3 tracker/youtube_tracker.py rank --all-channels --days 60
python3 tracker/youtube_tracker.py channels
python3 tracker/youtube_tracker.py show VIDEO_ID
python3 tracker/youtube_tracker.py mark VIDEO_ID --status selected
```

Use `mark` with `selected`, `edited`, `rejected` or `new` to maintain decisions. Ranking omits used videos by default. Watchlist entries support `priority`, `enabled` and `excluded`. An explicit exclusion also applies during broader rankings. Metadata requests send the key in a header to Google's YouTube Data API. The per-run request budget defaults to 200 HTTP attempts; use `--max-requests` before the subcommand to adjust it.

The ranking combines views, freshness, watchlist priority and growth between observations at least six hours apart. YouTube supplies global views. US availability and duration are filters; assess audience, format, and story fit when reviewing the footage.

## Fetch selected footage

Install yt-dlp with its default dependencies, FFmpeg including ffprobe and the drawtext filter, and Node.js for the downloader's JavaScript runtime. The tracker and offline tests run independently of those tools.

```sh
python3 fetch.py --from-report tracker/reports/latest.json --limit 1
python3 fetch.py 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Replace `VIDEO_ID` with an actual 11-character video ID. The downloader requests a source up to 1280 pixels high, prefers H.264, and writes `downloads/sources/VIDEO_ID.mp4` and its yt-dlp metadata. It samples up to 24 frames to build `downloads/inspection/VIDEO_ID.jpg`. Keep the supplied ID when comparing a report row, a downloaded file and an editorial decision.

Pass `--cache /path/to/sources` to reuse an existing ID-named MP4 and any matching metadata. Use `--no-sheets` to skip JPEG creation, `--yt-dlp /path/to/yt-dlp` to use an existing installation, and `--output DIR` to choose the output root. Repeated input IDs are fetched once. Failed downloads return a nonzero exit status; retrying reuses completed items. Fetching does not assign an editorial decision.

## Bring an existing tracker into a private checkout

Copy your watchlist to `tracker/watchlist.json` and use SQLite's backup command to copy the database into the new checkout. This preserves channel identities, view snapshots, decisions and run history. Paths below are examples; replace them with the existing files.

```sh
mkdir -p tracker/data
cp /path/to/old/tracker/watchlist.json tracker/watchlist.json
sqlite3 /path/to/old/tracker/data/watchlist.sqlite3 ".backup tracker/data/watchlist.sqlite3"
python3 tracker/youtube_tracker.py rank
```

Use an empty destination for the initial copy. You can point `--db`, `--watchlist` and `--env` at alternate files before a tracker subcommand. The default private watchlist, database, reports, credentials and download directory are ignored by Git.
