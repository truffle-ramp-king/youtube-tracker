#!/usr/bin/env python3
"""Local YouTube watchlist tracker. Python 3.10+ standard library only."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib import error, parse, request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UTC = timezone.utc
BASE = 'https://www.googleapis.com/youtube/v3/'


def utcnow():
    return datetime.now(UTC)


def stamp(value=None):
    return (value or utcnow()).isoformat(timespec='seconds').replace('+00:00', 'Z')


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def load_key(path):
    """Read one variable without executing .env, interpolating, or logging it."""
    key = os.environ.get('YOUTUBE_DATA_API_KEY', '').strip()
    if key:
        return key
    if not path.exists():
        raise ValueError(f'Missing API key. Set YOUTUBE_DATA_API_KEY in {path.name} or the environment.')
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, sep, value = line.partition('=')
        if sep and name.strip() == 'YOUTUBE_DATA_API_KEY':
            value = value.strip()
            if value[:1] in ('"', "'"):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0:
                    raise ValueError('Unclosed quote in YOUTUBE_DATA_API_KEY.')
                value = value[1:end]
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
            if value:
                return value
    raise ValueError('YOUTUBE_DATA_API_KEY is empty or missing.')


def seconds(value):
    match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?', value or '')
    if not match:
        return 0.0
    return sum(float(n or 0) * m for n, m in zip(match.groups(), (86400, 3600, 60, 1)))


class APIError(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError('Unexpected API redirect refused; credentials were not forwarded.')


class YouTube:
    def __init__(self, key, max_requests=200, opener=None):
        self.key = key
        self.max_requests = max_requests
        self.calls = Counter()
        self.opener = opener or request.build_opener(NoRedirect())

    def get(self, method, **params):
        if method not in {'channels', 'playlistItems', 'videos', 'search'}:
            raise ValueError('Unsupported API method')
        url = BASE + method + '?' + parse.urlencode({k: v for k, v in params.items() if v is not None})
        # Header authentication avoids putting the key in URLs or exception strings.
        req = request.Request(url, headers={'X-Goog-Api-Key': self.key, 'User-Agent': 'LocalYouTubeWatchlist/1.0', 'Accept': 'application/json'})
        for attempt in range(4):
            if sum(self.calls.values()) >= self.max_requests:
                raise APIError('Local request budget reached. Increase --max-requests to continue.')
            self.calls[method] += 1
            try:
                with self.opener.open(req, timeout=30) as response:
                    return json.load(response)
            except error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read())
                    reason = payload.get('error', {}).get('errors', [{}])[0].get('reason', 'requestFailed')
                except (ValueError, IndexError, AttributeError):
                    reason = 'requestFailed'
                # Never echo arbitrary server messages, URLs, response bodies or credentials.
                reason = re.sub(r'[^a-zA-Z0-9_]', '', str(reason))[:80]
                if (exc.code == 429 or exc.code >= 500) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise APIError(f'YouTube {method}: HTTP {exc.code} ({reason}). Check key restrictions, API enablement or quota.') from None
            except (error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise APIError(f'YouTube {method}: network/TLS connection failed. Check network access.') from None
            except (ValueError, UnicodeError):
                raise APIError(f'YouTube {method}: invalid JSON response.') from None


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, title TEXT, uploads TEXT, data TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, channel_id TEXT, published TEXT, duration REAL, views INTEGER, data TEXT, fetched_at TEXT, available INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS snapshots (video_id TEXT, observed TEXT, views INTEGER, PRIMARY KEY(video_id,observed));
    CREATE TABLE IF NOT EXISTS decisions (video_id TEXT PRIMARY KEY, status TEXT, note TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS runs (started TEXT PRIMARY KEY, command TEXT, summary TEXT);
    CREATE INDEX IF NOT EXISTS ix_video_channel ON videos(channel_id);
    CREATE INDEX IF NOT EXISTS ix_snapshot_video ON snapshots(video_id,observed);
    ''')
    return db


def batches(items, size=50):
    items = list(dict.fromkeys(items))
    for i in range(0, len(items), size):
        yield items[i:i+size]


def save_channels(db, api, ids):
    found = []
    for batch in batches(ids):
        data = api.get('channels', part='snippet,contentDetails,statistics', id=','.join(batch))
        for item in data.get('items', []):
            save_channel(db, item)
            found.append(item['id'])
    db.commit()
    return found


def save_channel(db, item):
    db.execute('INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?)', (item['id'], item['snippet']['title'], item.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads'), json.dumps(item, ensure_ascii=False), stamp()))


def normalize(item, observed):
    s, c, stats = item['snippet'], item.get('contentDetails', {}), item.get('statistics', {})
    regions = c.get('regionRestriction', {})
    available_us = 'US' not in regions.get('blocked', []) and ('allowed' not in regions or 'US' in regions['allowed'])
    return {'id': item['id'], 'title': s['title'], 'description': s.get('description', ''), 'channel_id': s['channelId'], 'channel': s['channelTitle'], 'published_at': s['publishedAt'], 'duration_seconds': seconds(c.get('duration')), 'views': int(stats['viewCount']) if 'viewCount' in stats else None, 'likes': int(stats['likeCount']) if 'likeCount' in stats else None, 'comments': int(stats['commentCount']) if 'commentCount' in stats else None, 'language': s.get('defaultAudioLanguage', s.get('defaultLanguage')), 'thumbnail': s.get('thumbnails', {}).get('high', {}).get('url'), 'url': 'https://www.youtube.com/watch?v=' + item['id'], 'live': s.get('liveBroadcastContent', 'none'), 'privacy': item.get('status', {}).get('privacyStatus', 'public'), 'available_us': available_us, 'fetched_at': observed}


def save_videos(db, api, ids):
    count = 0
    for batch in batches(ids):
        data = api.get('videos', part='snippet,contentDetails,statistics,status', id=','.join(batch))
        observed = stamp()
        found = set()
        for item in data.get('items', []):
            v = normalize(item, observed)
            db.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,1)', (v['id'], v['channel_id'], v['published_at'], v['duration_seconds'], v['views'], json.dumps(v, ensure_ascii=False), observed))
            if v['views'] is not None:
                db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)', (v['id'], observed, v['views']))
            found.add(v['id'])
            count += 1
        for missing in set(batch) - found:
            db.execute('UPDATE videos SET available=0,fetched_at=? WHERE id=?', (observed, missing))
        db.commit()
    return count


def read_watchlist(path):
    data = json.loads(path.read_text()) if path.exists() else {'channels': []}
    seen = set()
    for c in data['channels']:
        if not re.fullmatch(r'UC[\w-]{22}', c['id']) or c['id'] in seen:
            raise ValueError('Watchlist contains an invalid or duplicate channel ID.')
        seen.add(c['id'])
    return data


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def refresh(db, api, watchlist, days, pages):
    channels = [c for c in watchlist['channels'] if c.get('enabled', True) and not c.get('excluded', False)]
    found = set(save_channels(db, api, [c['id'] for c in channels]))
    cutoff = stamp(utcnow() - timedelta(days=days))
    ids, coverage = [], []
    for channel in channels:
        cid = channel['id']
        if cid not in found:
            coverage.append({'channel_id': cid, 'status': 'unavailable'})
            continue
        row = db.execute('SELECT * FROM channels WHERE id=?', (cid,)).fetchone()
        if not row['uploads']:
            coverage.append({'channel_id': cid, 'status': 'no_uploads_playlist'})
            continue
        token, fetched, capped = None, 0, False
        for page in range(pages):
            data = api.get('playlistItems', part='contentDetails', playlistId=row['uploads'], maxResults=50, pageToken=token)
            items = data.get('items', [])
            for item in items:
                c = item['contentDetails']
                if c.get('videoPublishedAt', '9999') >= cutoff:
                    ids.append(c['videoId']); fetched += 1
            token = data.get('nextPageToken')
            old_page = bool(items) and all(i['contentDetails'].get('videoPublishedAt', '9999') < cutoff for i in items)
            if not token or old_page:
                break
            capped = page + 1 == pages
        coverage.append({'channel_id': cid, 'title': row['title'], 'recent_ids': fetched, 'page_cap_reached': capped, 'status': 'ok'})
        # Recheck cached recent videos, including those that dropped from the first pages.
        ids.extend(r[0] for r in db.execute('SELECT id FROM videos WHERE channel_id=? AND published>=?', (cid, cutoff)))
    count = save_videos(db, api, ids)
    return {'videos_updated': count, 'coverage': coverage}


def discover(db, api, queries, days, pages, order):
    ids = []
    for query in queries:
        token = None
        for _ in range(pages):
            data = api.get('search', part='snippet', q=query, type='video', videoDuration='short', order=order, publishedAfter=stamp(utcnow()-timedelta(days=days)), maxResults=50, regionCode='US', relevanceLanguage='en', pageToken=token)
            ids.extend(i['id']['videoId'] for i in data.get('items', []) if 'videoId' in i.get('id', {}))
            token = data.get('nextPageToken')
            if not token:
                break
    count = save_videos(db, api, ids)
    channel_ids = set()
    for batch in batches(ids):
        placeholders = ','.join('?' * len(batch))
        channel_ids.update(r[0] for r in db.execute(f'SELECT DISTINCT channel_id FROM videos WHERE id IN ({placeholders})', batch))
    save_channels(db, api, channel_ids)
    return {'queries': queries, 'videos_updated': count, 'discovered_channels': len(channel_ids)}


def score_video(v, age, velocity=None, priority=1.0):
    # Observable view scale + explicit freshness. Velocity requires two spaced snapshots.
    views_term = math.log10(1 + v['views'])
    fresh_term = 1 + 0.9 * math.exp(-age / 30)
    momentum = 1 + (min(0.35, math.log10(1 + max(0, velocity)) / 20) if velocity is not None else 0)
    return round(views_term * fresh_term * momentum * priority, 4)


def ranked(db, watchlist, min_views=500000, days=90, max_duration=90, include_used=False, all_channels=False, now=None):
    now = now or utcnow()
    excluded = {c['id'] for c in watchlist['channels'] if c.get('excluded', False)}
    active = {c['id']: c for c in watchlist['channels'] if c.get('enabled', True) and c['id'] not in excluded}
    rows = db.execute('SELECT v.*,d.status AS decision,d.note AS decision_note FROM videos v LEFT JOIN decisions d ON d.video_id=v.id WHERE v.available=1').fetchall()
    result = []
    for row in rows:
        v = json.loads(row['data'])
        if v['channel_id'] in excluded:
            continue
        if not all_channels and v['channel_id'] not in active:
            continue
        if not include_used and row['decision'] in ('edited', 'rejected', 'selected'):
            continue
        age = (now - dt(v['published_at'])).total_seconds()/86400
        if age < 0 or age > days or v['views'] is None or v['views'] < min_views or not 0 < v['duration_seconds'] <= max_duration or v['live'] != 'none' or v['privacy'] != 'public' or not v['available_us']:
            continue
        observed = dt(v['fetched_at'])
        prior = db.execute('SELECT observed,views FROM snapshots WHERE video_id=? AND observed<=? ORDER BY observed DESC LIMIT 1', (v['id'], stamp(observed-timedelta(hours=6)))).fetchone()
        velocity = None
        if prior and v['views'] >= prior['views']:
            velocity = (v['views']-prior['views']) / ((observed-dt(prior['observed'])).total_seconds()/86400)
        v.update(age_days=round(age, 2), lifetime_views_per_day=round(v['views']/max(age,1)), measured_views_per_day=round(velocity,2) if velocity is not None else None, score=score_video(v,age,velocity,active.get(v['channel_id'],{}).get('priority',1.0)), decision=row['decision'], stats_age_hours=round((now-observed).total_seconds()/3600,2))
        result.append(v)
    return sorted(result,key=lambda v:(v['score'],v['views']),reverse=True)


def diversify(rows, limit, max_per_channel):
    counts, out = Counter(), []
    for v in rows:
        if max_per_channel and counts[v['channel_id']] >= max_per_channel:
            continue
        out.append(v);counts[v['channel_id']] += 1
        if len(out) >= limit:
            break
    return out


def channel_summary(db, watchlist, days=90):
    cutoff = utcnow()-timedelta(days=days)
    watched = {c['id']:c for c in watchlist['channels']}
    out = []
    for row in db.execute('SELECT * FROM channels'):
        videos = [json.loads(v[0]) for v in db.execute('SELECT data FROM videos WHERE channel_id=? AND available=1',(row['id'],))]
        recent = [v for v in videos if dt(v['published_at'])>=cutoff and 0<v['duration_seconds']<=90]
        hits = [v for v in recent if (v['views'] or 0)>=500000]
        out.append({'id':row['id'],'title':row['title'],'url':'https://www.youtube.com/channel/'+row['id'],'enabled':watched.get(row['id'],{}).get('enabled',False),'excluded':watched.get(row['id'],{}).get('excluded',False),'recent_short_candidates_sampled':len(recent),'recent_500k_hits':len(hits),'max_recent_views':max([v['views'] or 0 for v in recent],default=0),'latest_observed_upload':max([v['published_at'] for v in videos],default=None),'note':watched.get(row['id'],{}).get('note','Discovery only')})
    return sorted(out,key=lambda c:(c['recent_500k_hits'],c['max_recent_views']),reverse=True)


def report(db, watchlist, args):
    rows = diversify(ranked(db,watchlist,args.min_views,args.days,args.max_duration,args.include_used,args.all_channels),args.limit,args.max_per_channel)
    payload={'generated_at':stamp(),'filters':{'min_views':args.min_views,'max_age_days':args.days,'max_duration_seconds':args.max_duration,'max_per_channel':args.max_per_channel},'notes':['Views are global; US availability is not proof of a US audience.','Duration is a candidate filter, not proof that a video is a YouTube Short.','Lifetime views/day is an average, not current velocity; measured velocity requires snapshots at least six hours apart.','First/original upload and reuse rights are not established by this tracker.'],'videos':rows,'channels':channel_summary(db,watchlist,args.days)}
    atomic_json(args.output,payload)
    body=[]
    for v in rows:
        body.append(f'<tr><td><a href="{html.escape(v["url"])}">{html.escape(v["title"])}</a><small>{html.escape(v["channel"])}</small></td><td>{v["views"]:,}</td><td>{format(v["likes"], ",") if v.get("likes") is not None else "Unavailable"}</td><td>{v["age_days"]:.1f}d</td><td>{v["duration_seconds"]:g}s</td><td>{v["score"]}</td><td>{v["measured_views_per_day"] if v["measured_views_per_day"] is not None else "Awaiting second snapshot"}</td></tr>')
    ch=''.join(f'<tr><td><a href="{c["url"]}">{html.escape(c["title"])}</a></td><td>{"Excluded" if c["excluded"] else "Tracking" if c["enabled"] else "Discovery / inactive"}</td><td>{c["recent_500k_hits"]}</td><td>{c["max_recent_views"]:,}</td><td>{html.escape(c["note"])}</td></tr>' for c in payload['channels'])
    page='<!doctype html><html lang="en"><meta charset="utf-8"><title>YouTube watchlist</title><style>body{background:#15191f;color:#e6edf3;font:15px/1.5 system-ui;margin:40px}a{color:#a9d7ff}small{display:block;color:#aab5c0}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #38424d;text-align:left;vertical-align:top}td:nth-child(2){white-space:nowrap}</style><h1>YouTube watchlist</h1><p>'+html.escape(payload['generated_at'])+' · '+str(len(rows))+' candidates</p><p>'+html.escape(' '.join(payload['notes']))+'</p><table><tr><th>Video / channel</th><th>Views</th><th>Likes</th><th>Age</th><th>Length</th><th>Score</th><th>Measured new views/day</th></tr>'+''.join(body)+'</table><h2>Channel evidence</h2><p>Counts below describe the sampled videos, not complete channel history.</p><table><tr><th>Channel</th><th>Status</th><th>Recent 500k+ hits</th><th>Top recent views</th><th>Notes</th></tr>'+ch+'</table></html>'
    args.output.with_suffix('.html').write_text(page)
    print(f'{len(rows)} candidates → {args.output}')
    for v in rows[:20]:
        print(f'{v["id"]}  {v["views"]:>10,} views  {v["age_days"]:>5.1f}d  {v["duration_seconds"]:>4g}s  {v["channel"]}: {v["title"][:90]}')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=HERE/'data/watchlist.sqlite3')
    parser.add_argument('--watchlist',type=Path,default=HERE/'watchlist.json')
    parser.add_argument('--env',type=Path,default=ROOT/'.env')
    parser.add_argument('--max-requests',type=int,default=200)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('refresh');p.add_argument('--days',type=int,default=90);p.add_argument('--pages-per-channel',type=int,default=3)
    p=sub.add_parser('discover');p.add_argument('--query',action='append',required=True);p.add_argument('--days',type=int,default=90);p.add_argument('--pages',type=int,default=1);p.add_argument('--order',choices=['date','viewCount','relevance'],default='viewCount')
    p=sub.add_parser('add');p.add_argument('channel');p.add_argument('--note',default='');p.add_argument('--priority',type=float,default=1.0);p.add_argument('--category',default='visual-story')
    p=sub.add_parser('rank');p.add_argument('--min-views',type=int,default=500000);p.add_argument('--days',type=int,default=90);p.add_argument('--max-duration',type=int,default=90);p.add_argument('--limit',type=int,default=100);p.add_argument('--max-per-channel',type=int,default=3);p.add_argument('--include-used',action='store_true');p.add_argument('--all-channels',action='store_true');p.add_argument('--output',type=Path,default=HERE/'reports/latest.json')
    p=sub.add_parser('channels');p.add_argument('--days',type=int,default=90)
    p=sub.add_parser('mark');p.add_argument('ids',nargs='+');p.add_argument('--status',choices=['edited','rejected','selected','new'],required=True);p.add_argument('--note',default='')
    p=sub.add_parser('show');p.add_argument('id')
    args=parser.parse_args(argv)
    for name in ('days','pages','pages_per_channel','limit','max_duration','max_requests'):
        if hasattr(args,name) and getattr(args,name)<=0:parser.error(name+' must be positive')
    if getattr(args,'min_views',0)<0 or getattr(args,'max_per_channel',0)<0:parser.error('Views and channel cap must be nonnegative')
    api=None
    try:
        watchlist=read_watchlist(args.watchlist)
        with connect(args.db) as db:
            started=stamp()
            if args.command in ('refresh','discover','add'):
                api=YouTube(load_key(args.env),args.max_requests)
            if args.command=='refresh':
                summary=refresh(db,api,watchlist,args.days,args.pages_per_channel)
            elif args.command=='discover':
                summary=discover(db,api,args.query,args.days,args.pages,args.order)
            elif args.command=='add':
                value=args.channel.rstrip('/').rsplit('/',1)[-1]
                params={'id':value} if value.startswith('UC') else {'forHandle':value.lstrip('@')}
                data=api.get('channels',part='snippet,contentDetails,statistics',**params)
                if not data.get('items'):raise ValueError('No matching channel found. Use a UC channel ID or @handle.')
                item=data['items'][0];save_channel(db,item)
                entry={'id':item['id'],'label':item['snippet']['title'],'enabled':True,'priority':args.priority,'category':args.category,'note':args.note}
                watchlist['channels']=[c for c in watchlist['channels'] if c['id']!=item['id']]+[entry]
                atomic_json(args.watchlist,watchlist);summary={'added_channel':entry}
            elif args.command=='rank':
                report(db,watchlist,args);return 0
            elif args.command=='channels':
                result=channel_summary(db,watchlist,args.days);atomic_json(HERE/'reports/channels.json',result)
                for c in result:print(f'{c["id"]} | {c["title"]} | {c["recent_500k_hits"]} recent 500k+ hits | latest {c["latest_observed_upload"]} | active={c["enabled"]}')
                return 0
            elif args.command=='mark':
                for vid in args.ids:
                    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid):raise ValueError('Invalid video ID')
                    db.execute('INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)',(vid,args.status,args.note,stamp()))
                summary={'marked':len(args.ids),'status':args.status}
            elif args.command=='show':
                row=db.execute('SELECT data FROM videos WHERE id=?',(args.id,)).fetchone()
                if not row:raise ValueError('Video not in local database.')
                print(json.dumps(json.loads(row[0]),indent=2,ensure_ascii=False));return 0
            summary['api_requests']=dict(api.calls) if api else {}
            db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?)',(started,args.command,json.dumps(summary)))
            atomic_json(HERE/'reports'/f'last_{args.command}.json',summary)
            print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 0
    except (APIError,ValueError,FileNotFoundError) as exc:
        message=str(exc)
        if api:message=message.replace(api.key,'[REDACTED]')
        print('Error: '+message,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
