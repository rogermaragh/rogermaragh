"""Offline tests for scripts/live_status.py — no network: a fake web answers every request.

The fake web mirrors what the real domains did on 2026-09-27: a site that answers, domains that
forward to App Store pages, and NineFifo's shape (no DNS without www, HTTPS timing out, plain
http://www forwarding).
"""
import datetime as dt
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import live_status as ls  # noqa: E402

DNS = urllib.error.URLError(socket.gaierror(8, 'nodename nor servname provided'))
TIMEOUT = urllib.error.URLError(socket.timeout('timed out'))
PIPS = 'https://apps.apple.com/us/app/pips-market-munch/id6806082015'
APPS = {6806082015: {'trackName': 'Pip’s Market Munch'},
        6738878207: {'trackName': 'MacMagical', 'version': '1.0', 'averageUserRating': 5.0,
                     'userRatingCount': 5, 'trackViewUrl': 'https://apps.apple.com/us/app/macmagical/id6738878207?uo=4'}}


class Web:
    """url -> (status, location) or an exception to raise; records every request."""
    def __init__(self, pages, seconds=1.23):
        self.pages, self.seconds, self.asked = dict(pages), seconds, []

    def __call__(self, url):
        self.asked.append(url)
        page = self.pages.get(url, DNS)
        if isinstance(page, list):          # a sequence of answers, one per request
            page = page.pop(0) if len(page) > 1 else page[0]
        if isinstance(page, Exception):
            raise page
        code, loc = page
        return code, loc, self.seconds


def lookup(app_id):
    return APPS.get(app_id)


def check(domain, pages, **kw):
    return ls.check_domain(domain, Web(pages), lambda i: (APPS.get(i) or {}).get('trackName'),
                           sleep=lambda s: None, **kw)


class DomainTest(unittest.TestCase):
    def test_a_site_that_answers_is_up(self):
        r = check('a.test', {'https://a.test/': (200, None)})
        self.assertEqual((r['state'], r['detail'], r['link']), ('up', 'Answers in 1.2 s', 'https://a.test/'))

    def test_redirects_inside_the_site_are_followed(self):
        r = check('a.test', {'https://a.test/': (301, 'https://www.a.test/home'), 'https://www.a.test/home': (200, None)})
        self.assertEqual(r['state'], 'up')

    def test_a_redirect_to_the_app_store_is_a_forward_named_by_apple(self):
        r = check('b.test', {'https://b.test/': (301, PIPS)})
        self.assertEqual((r['state'], r['detail']), ('forwards', 'To Pip’s Market Munch on the App Store'))

    def test_the_app_name_falls_back_to_the_link_when_apple_is_silent(self):
        r = ls.check_domain('b.test', Web({'https://b.test/': (301, PIPS)}), lambda i: None, sleep=lambda s: None)
        self.assertEqual(r['detail'], 'To Pips Market Munch on the App Store')

    def test_developer_page_and_other_domains(self):
        dev = check('c.test', {'https://c.test/': (302, 'https://apps.apple.com/us/developer/roger-maragh/id1037386773')})
        other = check('d.test', {'https://d.test/': (301, 'https://www.elsewhere.test/x')})
        self.assertEqual(dev['detail'], 'To my App Store developer page')
        self.assertEqual(other['detail'], 'To elsewhere.test')

    def test_ninefifo_shape_forwards_via_www_over_plain_http(self):
        r = check('n.test', {'https://n.test/': DNS, 'https://www.n.test/': TIMEOUT, 'http://n.test/': DNS,
                             'http://www.n.test/': (301, 'https://apps.apple.com/us/app/marketlens-market-scanner/id6792598613')})
        self.assertEqual(r['state'], 'forwards')
        self.assertEqual(r['detail'], 'To Marketlens Market Scanner on the App Store (www only, no HTTPS)')
        self.assertEqual(r['link'], 'http://www.n.test/')

    def test_a_dead_domain_gets_a_second_round_then_reads_down(self):
        web, pauses = Web({}), []
        r = ls.check_domain('dead.test', web, None, sleep=pauses.append)
        self.assertEqual((r['state'], r['detail'], r['link']), ('down', "Domain doesn't resolve", None))
        self.assertEqual((len(pauses), len(web.asked)), (1, 8))

    def test_a_server_error_is_retried_once(self):
        flaky = check('e.test', {'https://e.test/': [(503, None), (200, None)]})
        broken = check('e.test', {'https://e.test/': (503, None)})
        self.assertEqual(flaky['state'], 'up')
        self.assertEqual((broken['state'], broken['detail']), ('down', 'HTTP 503'))

    def test_a_404_is_down_without_waiting(self):
        pauses = []
        r = ls.check_domain('f.test', Web({'https://f.test/': (404, None)}), None, sleep=pauses.append)
        self.assertEqual((r['detail'], pauses), ('HTTP 404', []))

    def test_connection_failures_in_plain_words(self):
        self.assertEqual(ls.why(DNS), 'dns')
        self.assertEqual(ls.why(TIMEOUT), 'timeout')
        self.assertEqual(ls.why(urllib.error.URLError(ssl.SSLCertVerificationError('bad cert'))), 'tls')
        self.assertEqual(ls.why(urllib.error.URLError(ConnectionRefusedError())), 'refused')
        self.assertEqual(ls.why(TimeoutError()), 'timeout')


class AppTest(unittest.TestCase):
    def test_listing(self):
        r = ls.check_app(6738878207, lookup)
        self.assertEqual((r['state'], r['detail']), ('app', 'Version 1.0 · ★ 5.0 (5 ratings)'))
        self.assertEqual(r['link'], 'https://apps.apple.com/us/app/macmagical/id6738878207')

    def test_no_ratings_yet_and_gone(self):
        r = ls.check_app(1, lambda i: {'version': '2.0'})
        self.assertEqual(r['detail'], 'Version 2.0 · no ratings yet')
        self.assertEqual(ls.check_app(2, lambda i: None)['detail'], 'No longer on the App Store')

    def test_apple_not_answering_is_no_result(self):
        def fail(i):
            raise TIMEOUT
        self.assertIsNone(ls.check_app(1, fail))


class ReadmeTest(unittest.TestCase):
    ROWS = [{'icon': '🎟️', 'name': 'A.test', 'domain': 'a.test'},
            {'icon': '📱', 'name': 'App', 'app_store_id': 6738878207}]
    BEFORE, AFTER = '## Hi\n- intro\n\n', '\n\n## Bio\nUntouched text | with a pipe.\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.readme, self.config = d / 'README.md', d / 'live-status.json'
        self.readme.write_text(self.BEFORE + ls.START + '\n' + ls.END + self.AFTER, encoding='utf-8')
        self.config.write_text(json.dumps({'rows': self.ROWS}), encoding='utf-8')

    def run_at(self, when, pages=None, seconds=1.0, apps=lookup):
        web = Web(pages or {'https://a.test/': (200, None)}, seconds)
        with open(os.devnull, 'w') as quiet:
            saved, sys.stdout = sys.stdout, quiet
            try:
                rc = ls.main([str(self.readme), '--config', str(self.config)], get=web, lookup=apps,
                             now=when, sleep=lambda s: None)
            finally:
                sys.stdout = saved
        return rc, self.readme.read_text(encoding='utf-8')

    def test_fills_only_the_block(self):
        rc, text = self.run_at(dt.datetime(2026, 9, 27, 5, 10, tzinfo=dt.timezone.utc))
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith(self.BEFORE) and text.endswith(self.AFTER))
        self.assertIn('| 🎟️ | [A.test](https://a.test/) | 🟢 Up | Answers in 1.0 s |', text)
        self.assertIn('| 📱 | [App](https://apps.apple.com/us/app/macmagical/id6738878207) | 🟢 On the App Store |', text)
        self.assertIn('updated Sep 27, 2026, 05:10 UTC', text)
        self.assertIn('### 🟢 Live status', text)

    def test_rewrites_on_a_change_or_a_new_day_only(self):
        t1 = dt.datetime(2026, 9, 27, 5, 10, tzinfo=dt.timezone.utc)
        _, first = self.run_at(t1)
        _, same = self.run_at(t1 + dt.timedelta(hours=1), seconds=2.5)     # only the timing moved
        self.assertEqual(same, first)
        _, down = self.run_at(t1 + dt.timedelta(hours=2), pages={'https://a.test/': (404, None)})
        self.assertIn('| 🔴 Down | HTTP 404 |', down)
        self.assertIn('### 🟠 Live status', down)
        _, back = self.run_at(t1 + dt.timedelta(hours=3))
        _, still = self.run_at(t1 + dt.timedelta(hours=4))
        self.assertEqual(still, back)
        _, next_day = self.run_at(t1 + dt.timedelta(days=1))
        self.assertNotEqual(next_day, back)
        self.assertIn('updated Sep 28, 2026, 05:10 UTC', next_day)

    def test_apple_not_answering_leaves_the_readme_alone(self):
        before = self.readme.read_text(encoding='utf-8')

        def fail(i):
            raise TIMEOUT
        rc, after = self.run_at(dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc), apps=fail)
        self.assertEqual((rc, after), (0, before))

    def test_missing_markers_or_config_is_2(self):
        self.readme.write_text('no markers here\n', encoding='utf-8')
        self.assertEqual(self.run_at(dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc))[0], 2)
        self.config.write_text('{}', encoding='utf-8')
        self.assertEqual(self.run_at(dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc))[0], 2)


class RegistryTest(unittest.TestCase):
    def test_public_live_registry_has_expected_domains(self):
        config = Path(__file__).resolve().parents[1] / 'live-status.json'
        rows = json.loads(config.read_text(encoding='utf-8'))['rows']
        domains = [row['domain'] for row in rows if 'domain' in row]
        self.assertEqual(domains, [
            'routeddata.com', 'macmagical.com', 'rogermaragh.com',
            'www.rogermaragh.com', 'browardlocals.com', 'xyzyo.com',
            'ninefifo.com', 'magicalpc.com', 'love1tech.com', 'xerokewl.io',
            'rajhmiraj.com', 'metasage.com',
        ])
        self.assertEqual(len({row['name'] for row in rows}), len(rows))



if __name__ == '__main__':
    unittest.main()
