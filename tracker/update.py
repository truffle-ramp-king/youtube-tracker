#!/usr/bin/env python3
"""Refresh the watchlist and rebuild the default report; no packages required."""
import youtube_tracker

if __name__ == '__main__':
    code = youtube_tracker.main(['refresh'])
    if not code:
        code = youtube_tracker.main(['rank'])
    raise SystemExit(code)
