#!/usr/bin/env python3
"""Build data/feed.json for the Portfolio Tracker.

Runs on GitHub Actions on a schedule. Fetches, per ticker in data/tickers.json:
  - daily closes (3 months) + live price / previous close   (Yahoo chart API)
  - headlines for the past 14 days                          (Yahoo RSS + search feed)
  - next earnings date + EPS / revenue estimates            (Yahoo quoteSummary, crumb auth)
and writes one same-origin JSON file the app can read without any CORS proxy or API key.

Per-ticker failures keep that ticker's previous data, so a flaky run never blanks the feed.
"""
import json, os, re, sys, time, html
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS = os.path.join(ROOT, 'data', 'tickers.json')
OUT = os.path.join(ROOT, 'data', 'feed.json')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36'
NEWS_DAYS, NEWS_MAX = 14, 15
# Words that identify each company in a headline. Yahoo's per-ticker RSS mixes in generic
# market stories, so anything not tagged by Yahoo itself must mention one of these.
ALIAS = {
    'AAPL': ['Apple', 'iPhone'], 'AMZN': ['Amazon', 'AWS'], 'GOOGL': ['Google', 'Alphabet'], 'GOOG': ['Google', 'Alphabet'],
    'META': ['Meta', 'Facebook', 'Instagram'], 'MSFT': ['Microsoft'], 'NVDA': ['Nvidia'], 'AMD': ['AMD', 'Advanced Micro Devices'],
    'IBM': ['IBM'], 'TSM': ['TSMC', 'Taiwan Semiconductor'], 'KO': ['Coca-Cola', 'Coke'], 'PAAS': ['Pan American Silver'],
    'QUBT': ['Quantum Computing Inc'], 'HIMX': ['Himax'], 'AEM': ['Agnico'], 'BABA': ['Alibaba'], 'WLN': ['Worldline'],
    'NFLX': ['Netflix'], 'AVGO': ['Broadcom'], 'CSCO': ['Cisco'], 'ORCL': ['Oracle'], 'PLTR': ['Palantir'],
    'QCOM': ['Qualcomm'], 'TSLA': ['Tesla', 'Musk'], 'GRU': ['Geely'],
}
GENERIC = {'the', 'inc', 'corp', 'corporation', 'company', 'group', 'holdings', 'holding', 'limited', 'ltd', 'plc', 'sa',
           'international', 'advanced', 'global', 'technologies', 'systems', 'quantum', 'taiwan'}


def keywords(base, name):
    kws = set(ALIAS.get(base, []))
    if len(base) >= 3:
        kws.add(base)
    w = re.split(r'[\s,.]+', name or '')[0] if name else ''
    if len(w) >= 4 and w.lower() not in GENERIC:
        kws.add(w)
    return kws


def relevant(item, kws):
    text = (item.get('title', '') + ' ' + item.get('sum', ''))
    return any(re.search(r'(?<![A-Za-z])' + re.escape(k) + r'(?![A-Za-z])', text, re.I) for k in kws)


def norm_title(t):
    return re.sub(r'\W+', '', t.lower())[:80]

S = requests.Session()
S.headers.update({'User-Agent': UA, 'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'})
CRUMB = None


def log(*a):
    print(*a, flush=True)


def get(url, **kw):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=25, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1)); continue
            return r
        except requests.RequestException as e:
            log('  retry', url[:80], e); time.sleep(2)
    return None


def init_crumb():
    """Yahoo's quoteSummary needs a session cookie + crumb; the other endpoints don't."""
    global CRUMB
    try:
        S.get('https://fc.yahoo.com', timeout=15)                       # sets the A1/A3 cookies (404 is fine)
        r = S.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=15)
        if r.ok and r.text and '<' not in r.text:
            CRUMB = r.text.strip(); log('crumb ok')
        else:
            log('crumb failed', r.status_code, r.text[:80])
    except Exception as e:
        log('crumb error', e)


def chart(sym):
    r = get(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d')
    if not r or not r.ok:
        return None
    try:
        res = r.json()['chart']['result'][0]
    except Exception:
        return None
    ts = res.get('timestamp') or []
    q = (res.get('indicators', {}).get('quote') or [{}])[0]
    closes = q.get('close') or []
    m = {}
    for t, c in zip(ts, closes):
        if c is not None and c > 0:
            m[datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d')] = round(float(c), 6)
    meta = res.get('meta', {})
    if not m:
        return None
    return {'closes': m, 'live': meta.get('regularMarketPrice'), 'ccy': meta.get('currency'),
            'ts': meta.get('regularMarketTime'), 'name': meta.get('longName') or meta.get('shortName')}


def rss_news(sym):
    r = get(f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US')
    out = []
    if not r or not r.ok or '<rss' not in r.text:
        return out
    try:
        root = ET.fromstring(r.text.encode('utf-8'))
    except ET.ParseError:
        return out
    for it in root.iter('item'):
        g = lambda k: (it.findtext(k) or '').strip()
        link, title = g('link'), html.unescape(g('title'))
        if not link or not title:
            continue
        try:
            ts = int(datetime.strptime(g('pubDate'), '%a, %d %b %Y %H:%M:%S %z').timestamp())
        except Exception:
            ts = 0
        desc = re.sub(r'<[^>]+>', '', html.unescape(g('description')))
        out.append({'id': g('guid') or link, 'title': title, 'url': link, 'src': 'Yahoo Finance',
                    'ts': ts, 'sum': re.sub(r'\s+', ' ', desc)[:320], 'img': ''})
    return out


def google_news(query):
    """Google News RSS for a company-name query — the most on-topic source, incl. European names."""
    q = requests.utils.quote(f'{query} stock', safe='')
    r = get(f'https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en')
    out = []
    if not r or not r.ok or '<rss' not in r.text:
        return out
    try:
        root = ET.fromstring(r.text.encode('utf-8'))
    except ET.ParseError:
        return out
    for it in root.iter('item'):
        g = lambda k: (it.findtext(k) or '').strip()
        link, title = g('link'), html.unescape(g('title'))
        src = (it.find('source').text if it.find('source') is not None else '') or ''
        if src and title.endswith(' - ' + src):
            title = title[: -len(src) - 3].rstrip()
        if not link or not title:
            continue
        try:
            ts = int(datetime.strptime(g('pubDate'), '%a, %d %b %Y %H:%M:%S %Z').replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            ts = 0
        out.append({'id': g('guid') or link, 'title': title, 'url': link, 'src': src or 'Google News',
                    'ts': ts, 'sum': '', 'img': '', 'tagged': False})
    return out


def search_news(sym, base):
    """Yahoo's search feed mixes in generic market stories; keep only items tagged with this ticker."""
    r = get(f'https://query1.finance.yahoo.com/v1/finance/search?q={sym}&newsCount=30&quotesCount=0&enableFuzzyQuery=false')
    out = []
    if not r or not r.ok:
        return out
    try:
        news = r.json().get('news') or []
    except Exception:
        return out
    want = {sym.upper(), base.upper(), sym.split('.')[0].upper()}
    for n in news:
        rel = {x.upper() for x in (n.get('relatedTickers') or [])}
        if not (rel & want):
            continue
        img = ''
        try:
            rs = n['thumbnail']['resolutions']; img = rs[-1]['url']
        except Exception:
            pass
        out.append({'id': n.get('uuid') or n.get('link'), 'title': n.get('title', '').strip(), 'url': n.get('link'),
                    'src': n.get('publisher', ''), 'ts': int(n.get('providerPublishTime') or 0), 'sum': '', 'img': img,
                    'tagged': True})
    return out


def earnings(sym):
    if not CRUMB:
        return None
    r = get(f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}?modules=calendarEvents&crumb={CRUMB}')
    if not r or not r.ok:
        return None
    try:
        e = r.json()['quoteSummary']['result'][0]['calendarEvents']['earnings']
    except Exception:
        return None
    dates = [d.get('raw') for d in (e.get('earningsDate') or []) if d.get('raw')]
    if not dates:
        return None
    d0 = datetime.fromtimestamp(min(dates), timezone.utc)
    return {'date': d0.strftime('%Y-%m-%d'),
            'range_end': datetime.fromtimestamp(max(dates), timezone.utc).strftime('%Y-%m-%d') if len(dates) > 1 else None,
            'eps': (e.get('earningsAverage') or {}).get('raw'),
            'rev': (e.get('revenueAverage') or {}).get('raw')}


def main():
    with open(TICKERS, encoding='utf-8') as f:
        cfg = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
    prev = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding='utf-8') as f:
                prev = json.load(f).get('tickers', {})
        except Exception:
            prev = {}
    init_crumb()
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)).timestamp())
    feed, ok = {}, 0
    for base, cands in cfg.items():
        cands = cands if isinstance(cands, list) else [cands]
        entry, sym = None, None
        for c in cands:
            ch = chart(c)
            if ch:
                sym, entry = c, ch; break
            time.sleep(0.4)
        if not entry:
            log(base, 'UNRESOLVED', cands, '- keeping previous')
            if base in prev:
                feed[base] = prev[base]
            continue
        kws = keywords(base, entry['name'])
        gq = ALIAS.get(base, [None])[0] or (entry['name'] or base)
        seen_titles, news = set(), {}
        # Google News first (most on-topic, real publisher names), then Yahoo's tagged search
        # items, then Yahoo RSS. Yahoo tags any story that merely mentions a ticker, so every
        # item must name the company in its headline or summary. Later duplicates lose.
        for n in google_news(gq) + search_news(sym, base) + rss_news(sym):
            if n['ts'] < cutoff or not n['url'] or n['url'] in news:
                continue
            if not relevant(n, kws):
                continue
            nt = norm_title(n['title'])
            if nt in seen_titles:
                continue
            seen_titles.add(nt); n.pop('tagged', None)
            news[n['url']] = n
        items = sorted(news.values(), key=lambda n: -n['ts'])[:NEWS_MAX]
        if not items and base in prev:
            items = prev[base].get('news', [])
        closes = entry['closes']
        ds = sorted(closes)
        live = entry['live'] or closes[ds[-1]]
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        prior = [d for d in ds if d < today]
        pc = closes[prior[-1]] if prior else (closes[ds[-2]] if len(ds) > 1 else live)
        feed[base] = {
            'sym': sym, 'name': entry['name'], 'ccy': entry['ccy'],
            'quote': {'price': live, 'prevClose': pc, 'ts': entry['ts']},
            'closes': closes,
            'news': items,
            'earnings': earnings(sym) or (prev.get(base, {}).get('earnings')),
        }
        ok += 1
        log(f'{base:6} {sym:8} px={live} closes={len(closes)} news={len(items)} earn={feed[base]["earnings"] and feed[base]["earnings"]["date"]}')
        time.sleep(0.6)
    if ok == 0:
        log('No ticker resolved - refusing to overwrite the feed'); sys.exit(1)
    out = {'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'ok': ok, 'total': len(cfg), 'tickers': feed}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    log('wrote', OUT, f'{ok}/{len(cfg)} tickers', os.path.getsize(OUT), 'bytes')


if __name__ == '__main__':
    main()
