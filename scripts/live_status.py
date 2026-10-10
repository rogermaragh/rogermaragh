#!/usr/bin/env python3
"""Rewrite the "Live status" block in README.md from live, public checks.

A row in live-status.json is a site ("domain": opened the way a visitor would — https, then
www, then plain http; redirects inside the site are followed, a redirect to another domain
means the domain forwards) or an App Store app ("app_store_id": Apple's public lookup API).
Standard library only; no tokens, no secrets. The block is rewritten only when a row's status
changes, or once a UTC day so the "updated" date shows the checker is still running.

    python3 scripts/live_status.py [README.md] [--config live-status.json] [--dry-run]

Exit 0 when checks completed, 1 when Apple lookup is unavailable, 2 on bad input.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

START, END = '<!-- live-status:start -->', '<!-- live-status:end -->'
REPO = 'https://github.com/rogermaragh/rogermaragh'
WORKFLOW = REPO + '/actions/workflows/live-status.yml'
UA = 'Mozilla/5.0 (compatible; rogermaragh-live-status/1.0; +%s)' % REPO
TIMEOUT = 15
VARIANTS = (('https', ''), ('https', 'www.'), ('http', ''), ('http', 'www.'))
WORDS = {'dns': "Domain doesn't resolve", 'timeout': 'No answer in %d s' % TIMEOUT,
         'tls': 'Certificate problem', 'refused': 'Refuses connections', 'unreachable': 'Unreachable'}
STATE = {'up': '🟢 Responding', 'forwards': '🟡 Forwards', 'down': '🔴 Down', 'app': '🟢 Listed in US App Store', 'protected': '🔒 Access restricted'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # hand every 3xx back, so we see where a domain points


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url, timeout=TIMEOUT):
    """One GET, redirects not followed -> (status, Location or None, seconds)."""
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    t0 = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            r.read(4096)
            return r.status, None, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location'), time.monotonic() - t0


def app_lookup(app_id, timeout=TIMEOUT):
    """Apple's public lookup -> the app's record, or None when the App Store no longer lists it."""
    req = urllib.request.Request('https://itunes.apple.com/lookup?id=%d&country=us' % int(app_id),
                                 headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        results = json.load(r).get('results', [])
    return results[0] if results else None


def why(err):
    """A connection failure as one word: dns · timeout · tls · refused · unreachable."""
    r = getattr(err, 'reason', err)
    if isinstance(r, socket.gaierror):
        return 'dns'
    if isinstance(r, (socket.timeout, TimeoutError)):
        return 'timeout'
    if isinstance(r, ssl.SSLError):
        return 'tls'
    if isinstance(r, ConnectionRefusedError):
        return 'refused'
    return 'unreachable'


def site_of(host):
    host = (host or '').lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def follow(url, get):
    """Open url, following redirects that stay on the same site (http→https, www) for 5 hops.
    -> ('up', seconds) · ('forwards', target url) · ('http', code) · ('error', reason)"""
    site = site_of(urllib.parse.urlsplit(url).hostname)
    for _ in range(6):
        try:
            code, loc, secs = get(url)
        except Exception as e:     # any failure to connect is a result here, not a crash
            return ('error', why(e))
        if 300 <= code < 400 and loc:
            nxt = urllib.parse.urljoin(url, loc)
            if site_of(urllib.parse.urlsplit(nxt).hostname) != site:
                return ('forwards', nxt)
            url = nxt
            continue
        if 200 <= code < 300:
            return ('up', secs)
        return ('http', code)
    return ('error', 'unreachable')


def where(url, app_name=None):
    """Where a forwarding domain points, in words."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or '').lower()
    if host == 'apps.apple.com' or host.endswith('.apps.apple.com'):
        if '/developer/' in u.path:
            return 'To my App Store developer page'
        m = re.search(r'/id(\d+)', u.path)
        name = None
        if m and app_name:
            try:
                name = app_name(int(m.group(1)))
            except Exception:
                name = None
        if not name:
            s = re.search(r'/app/([^/]+)/id', u.path)
            name = ' '.join(w.capitalize() for w in s.group(1).split('-')) if s else 'an app'
        return 'To %s on the App Store' % name
    return 'To %s' % site_of(host)


def caveat(scheme, www):
    bits = (['www only'] if www else []) + (['no HTTPS'] if scheme == 'http' else [])
    return ' (%s)' % ', '.join(bits) if bits else ''


def result(state, detail, link, sig):
    return {'state': state, 'detail': detail, 'link': link, 'sig': sig}


def check_domain(domain, get=http_get, app_name=None, pause=5, sleep=time.sleep):
    """A site, tried as https, https www., http, http www. — the first that answers decides.
    Connection failures and 5xx/429 get one more round after a pause before counting as down."""
    first_error, code = None, None
    for attempt in (1, 2):
        for scheme, www in VARIANTS:
            if domain.startswith('www.') and www:
                continue
            url = '%s://%s%s/' % (scheme, www, domain)
            kind, val = follow(url, get)
            if kind == 'error':
                first_error = first_error or val
                continue
            note = caveat(scheme, www)
            if kind == 'up':
                return result('up', 'Answers in %.1f s%s' % (val, note), url, 'up' + note)
            if kind == 'forwards':
                target = where(val, app_name) + note
                return result('forwards', target, url, 'forwards:' + target)
            code = val
            if code in (401, 403):
                return result('protected', 'HTTP %d; functionality unverified' % code, url, 'protected:%d' % code)
            break
        if code is not None and code < 500 and code != 429:
            break
        if attempt == 1:
            sleep(pause)
            first_error, code = None, None
    if code is not None:
        return result('down', 'HTTP %d' % code, None, 'down:http%d' % code)
    return result('down', WORDS[first_error or 'unreachable'], None, 'down:' + (first_error or 'unreachable'))


def check_app(app_id, lookup=app_lookup):
    """An App Store listing -> result, or None when Apple didn't answer (leave the table alone)."""
    try:
        rec = lookup(app_id)
    except Exception:
        return None
    if rec is None:
        return result('down', 'No longer on the App Store', None, 'app:gone')
    version = rec.get('version', '?')
    n = int(rec.get('userRatingCount') or 0)
    stars = float(rec.get('averageUserRating') or 0)
    rating = '★ %.1f (%d rating%s)' % (stars, n, '' if n == 1 else 's') if n else 'no ratings yet'
    link = (rec.get('trackViewUrl') or '').split('?')[0] or 'https://apps.apple.com/us/app/id%d' % int(app_id)
    return result('app', 'Version %s · %s' % (version, rating), link, 'app:%s:%.1f:%d' % (version, stars, n))


def render(rows, results, now, sig):
    head = '🟠' if any(r['state'] == 'down' for r in results) else '🟢'
    out = [START, '### %s Public availability' % head, '',
           'Checks page responses and US App Store listings. They do not verify sign-in, payments, data freshness, or app functionality.']
    pairs = list(zip(rows, results))
    for title, item_name, group in (
            ('Apps', 'App', [pair for pair in pairs if 'app_store_id' in pair[0]]),
            ('Websites', 'Website', [pair for pair in pairs if 'domain' in pair[0]])):
        if not group:
            continue
        out += ['', '#### %s' % title, '', '| | %s | Status | Details |' % item_name, '|:-:|---|---|---|']
        for cfg, r in group:
            name = '[%s](%s)' % (cfg['name'], r['link']) if r['link'] else cfg['name']
            out.append('| %s | %s | %s | %s |' % (cfg['icon'], name, STATE[r['state']], r['detail'].replace('|', r'\|')))
    when = '%s %d, %s UTC' % (now.strftime('%b'), now.day, now.strftime('%Y, %H:%M'))
    out += ['', '<sub>🕒 Checked every hour by a [GitHub Action](%s) · updated %s · '
                '[![live status](%s/badge.svg)](%s)</sub>' % (WORKFLOW, when, WORKFLOW, WORKFLOW),
            '<!-- live-status:sig=%s updated=%s -->' % (sig, now.strftime('%Y-%m-%dT%H:%MZ')), END]
    return '\n'.join(out)


def update(text, rows, results, now):
    """-> (new README text, changed?). Only the block between the markers is ever touched."""
    m = re.search(re.escape(START) + r'.*?' + re.escape(END), text, re.S)
    if not m:
        raise ValueError('README.md has no %s … %s block' % (START, END))
    sig = hashlib.sha1(json.dumps(['availability-v3', *[r['sig'] for r in results]]).encode()).hexdigest()[:12]
    old = re.search(r'live-status:sig=(\w+) updated=(\d{4}-\d{2}-\d{2})', m.group(0))
    if old and old.group(1) == sig and old.group(2) == now.strftime('%Y-%m-%d'):
        return text, False
    return text[:m.start()] + render(rows, results, now, sig) + text[m.end():], True


def main(argv=None, get=http_get, lookup=app_lookup, now=None, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('readme', nargs='?', default='README.md')
    ap.add_argument('--config', default='live-status.json')
    ap.add_argument('--dry-run', action='store_true', help='print the block, change nothing')
    a = ap.parse_args(argv)
    try:
        with open(a.config, encoding='utf-8') as f:
            rows = json.load(f)['rows']
        with open(a.readme, encoding='utf-8') as f:
            text = f.read()
    except (OSError, ValueError, KeyError) as e:
        print('live-status: %s' % e, file=sys.stderr)
        return 2
    names = {}

    def app_name(i):
        if i not in names:
            rec = lookup(i)
            names[i] = rec.get('trackName') if rec else None
        return names[i]

    results = []
    for cfg in rows:
        if 'app_store_id' in cfg:
            r = check_app(cfg['app_store_id'], lookup)
            if r is None:
                print("live-status: the App Store didn't answer — table left as it is")
                return 1
        else:
            r = check_domain(cfg['domain'], get, app_name, sleep=sleep)
        print('%-20s %-9s %s' % (cfg['name'], r['state'], r['detail']))
        results.append(r)
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        new, changed = update(text, rows, results, now)
    except ValueError as e:
        print('live-status: %s' % e, file=sys.stderr)
        return 2
    if a.dry_run:
        print(render(rows, results, now, 'dry-run'))
        return 0
    if changed:
        with open(a.readme, 'w', encoding='utf-8') as f:
            f.write(new)
    print('README.md %s' % ('updated' if changed else 'unchanged (nothing new today)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
