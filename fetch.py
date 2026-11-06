#!/usr/bin/env python3
"""Fetch selected YouTube videos, preserve their IDs and build review sheets."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
FORMAT = "bv*[height<=1280][vcodec^=avc1]+ba/b[height<=1280]/bv*[height<=1280]+ba"


def video_id(value):
    """Accept a video ID or a canonical YouTube watch/shorts/share URL."""
    url = urlsplit(value)
    candidate = value
    if url.scheme in {"https", "http"}:
        if url.hostname == "youtu.be":
            candidate = url.path.strip("/")
        elif url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if url.path == "/watch":
                candidate = parse_qs(url.query).get("v", [""])[0]
            elif url.path.startswith("/shorts/"):
                candidate = url.path[len("/shorts/"):].rstrip("/")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", candidate):
        raise ValueError(f"Invalid YouTube video ID or URL: {value!r}")
    return candidate


def check_info(path, expected_id):
    if json.loads(path.read_text())["id"] != expected_id:
        raise ValueError(f"Metadata ID differs from video ID {expected_id} in {path.name}")


def duration_of(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_type:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    duration = float(info["format"]["duration"])
    if not info["streams"] or not duration > 0:
        raise ValueError(f"No playable video in {path.name}")
    return duration


def contact_sheet(video, output, duration):
    step = duration / 24
    filters = (f"select='gte(t,selected_n*{step})',"
               "scale=180:320:force_original_aspect_ratio=decrease,"
               "pad=180:348:(ow-iw)/2:0:black,"
               f"drawtext=text='{video.stem} %{{pts\\:flt}}s':fontcolor=white:"
               "fontsize=13:x=4:y=327,tile=6x4")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
                    "-vf", filters, "-frames:v", "1", "-q:v", "3", str(output)], check=True)


def fetch_one(vid, args):
    out = args.output
    sources = out / "sources"
    inspection = out / "inspection"
    for folder in (sources, inspection):
        folder.mkdir(parents=True, exist_ok=True)
    target = sources / f"{vid}.mp4"
    poster = inspection / f"{vid}.jpg"
    log_path = inspection / f"{vid}.download.log"
    # Stage on the destination filesystem so a failed download leaves no partial MP4.
    with tempfile.TemporaryDirectory(dir=out, prefix=f".{vid}-") as tmp:
        stage = Path(tmp)
        media = target
        if not target.is_file():
            media = stage / target.name
            cache = next((p / target.name for p in args.cache if (p / target.name).is_file()), None)
            if cache:
                shutil.copyfile(cache, media)
                info = cache.with_suffix(".info.json")
                if info.is_file():
                    shutil.copyfile(info, media.with_suffix(".info.json"))
            else:
                command = [args.yt_dlp, "--ignore-config", "--js-runtimes", "node",
                           "--write-info-json", "--no-playlist", "--no-progress",
                           "--socket-timeout", "20", "--retries", "1", "-f", FORMAT,
                           "--merge-output-format", "mp4", "--remux-video", "mp4",
                           "-o", str(stage / "%(id)s.%(ext)s"), "--",
                           f"https://www.youtube.com/watch?v={vid}"]
                log = stage / "download.log"
                with log.open("w") as stream:
                    result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
                log.replace(log_path)
                if result.returncode:
                    raise ValueError(f"Download failed for {vid}; see {log_path}")
                if not media.is_file() or not media.with_suffix(".info.json").is_file():
                    raise ValueError(f"Downloader did not produce the expected MP4 and metadata for {vid}")
        info = media.with_suffix(".info.json")
        if info.is_file():
            check_info(info, vid)
        duration = duration_of(media)
        if media != target:
            if info.is_file():
                info.replace(target.with_suffix(".info.json"))
            media.replace(target)
        if not args.no_sheets and not poster.is_file():
            temp_poster = stage / poster.name
            contact_sheet(target, temp_poster, duration)
            temp_poster.replace(poster)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="*", help="YouTube video IDs or URLs")
    parser.add_argument("--from-report", type=Path, help="rank report containing videos[].id")
    parser.add_argument("--limit", type=int, help="maximum unique videos to fetch")
    parser.add_argument("--output", type=Path, default=ROOT / "downloads")
    parser.add_argument("--cache", action="append", type=Path, default=[],
                        help="existing source directory containing VIDEO_ID.mp4; repeatable")
    parser.add_argument("--yt-dlp", default="yt-dlp", help="path to the yt-dlp executable")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--no-sheets", action="store_true", help="skip timestamped JPEG review sheets")
    args = parser.parse_args(argv)
    if args.jobs < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--jobs and --limit must be positive")
    if not args.videos and not args.from_report:
        parser.error("supply video IDs, URLs, or --from-report")
    try:
        values = list(args.videos)
        if args.from_report:
            values.extend(row["id"] for row in json.loads(args.from_report.read_text())["videos"])
        ids = list(dict.fromkeys(video_id(value) for value in values))[:args.limit]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    failed = False
    with ThreadPoolExecutor(args.jobs) as pool:
        jobs = [(vid, pool.submit(fetch_one, vid, args)) for vid in ids]
        for vid, job in jobs:
            try:
                print(job.result())
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                print(f"{vid}: {exc}", file=sys.stderr)
                failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
