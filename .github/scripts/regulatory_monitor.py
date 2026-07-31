#!/usr/bin/env python3
"""Regulatory monitor extraction for RBI/SEBI/CERT-In.

Parses the fetched HTML pages into item lists, tracks a per-source sent-set of
item keys, and writes new_*.json for the email step plus the updated sent-set.

Usage:
  regulatory_monitor.py notif    # RBI Notifications (+ RSS id fallback)
  regulatory_monitor.py md       # RBI Master Directions
  regulatory_monitor.py sebi     # all 4 SEBI sources
  regulatory_monitor.py certin   # CERT-In guidelines
  regulatory_monitor.py --selftest [dir]   # run parsers over fixture files
"""

import base64
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape

try:
    from bs4 import BeautifulSoup
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "beautifulsoup4"])
    from bs4 import BeautifulSoup

STATE_FILES = {
    'notif': '.github/rbi_notif_sent.json',
    'md': '.github/rbi_md_sent.json',
    'sebi': '.github/sebi_sent.json',
    'certin': '.github/certin_sent.json',
}

# ponytail: one-time catch-up of the notification that the old regex missed
# (no title link in the fetched HTML, state had already advanced past it).
MISSED_13652 = {
    'key': '13652', 'id': '13652',
    'title': 'Reserve Bank of India (Small Finance Banks \u2013 Prudential Norms on Capital Adequacy) Fifth Amendment Directions, 2026',
    'url': 'https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=13652&Mode=0',
    'pdf': '', 'date': None,
}

SEBI_SOURCES = [
    ('sebi_pr.html',                 'SEBI PRESS RELEASE',      'new_sebi_pr.json'),
    ('sebi_public_notice.html',      'SEBI PUBLIC NOTICE',      'new_sebi_public_notice.json'),
    ('sebi_news_clarification.html', 'SEBI NEWS CLARIFICATION', 'new_sebi_news_clarification.json'),
    ('sebi_speeches.html',           'SEBI SPEECH',             'new_sebi_speeches.json'),
]


def norm(s):
    s = s or ''
    for dash in ('\u2013', '\u2014', '\u2212'):
        s = s.replace(dash, '-')
    return re.sub(r'\s+', ' ', s).strip()


def sha(t):
    return hashlib.sha1(t.encode('utf-8')).hexdigest()[:16]


def tablebg_tables(html):
    return BeautifulSoup(html, 'html.parser').find_all('table', class_='tablebg')


def count_data_rows(html):
    n = 0
    for tbl in tablebg_tables(html):
        for tr in tbl.find_all('tr'):
            if tr.find('td', class_='tableheader') is None and tr.find('td', recursive=False) is not None:
                n += 1
    return n


def dedupe(items):
    seen, out = set(), []
    for i in items:
        if i['key'] in seen:
            continue
        seen.add(i['key'])
        out.append(i)
    return out


def load_sent(path):
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return None


def write_state(path, sent):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(sorted(sent), f, indent=2)


def announce_new_found(flag):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write("new_found=%s\n" % ('true' if flag else 'false'))


def parse_notif():
    with open('notifications.html', encoding='utf-8', errors='ignore') as f:
        html = f.read()

    rss_by_title = {}
    if os.path.exists('notifications_rss.xml'):
        try:
            for it in ET.parse('notifications_rss.xml').getroot().iter('item'):
                t = it.findtext('title')
                l = it.findtext('link') or ''
                m = re.search(r'Id=(\d+)', unescape(l))
                if t and m:
                    rss_by_title[norm(t).lower()] = m.group(1)
        except ET.ParseError:
            pass

    items, cur_date = [], None
    for tbl in tablebg_tables(html):
        for tr in tbl.find_all('tr'):
            hdr = tr.find('td', class_='tableheader')
            if hdr is not None:
                b = hdr.find('b')
                if b:
                    cur_date = norm(b.get_text())
                continue
            tds = tr.find_all('td', recursive=False)
            if not tds:
                continue
            title = norm(tds[0].get_text(' ', strip=True))
            if not title:
                continue
            a = tds[0].find('a')
            nid = None
            if a is not None and a.get('href'):
                m = re.search(r'Id=(\d+)', a['href'])
                if m:
                    nid = m.group(1)
            pdf = ''
            for aa in tr.find_all('a', href=True):
                if '.PDF' in aa['href'].upper():
                    pdf = aa['href']
                    break
            pdfkey = re.search(r'/(NT\w+|NOTI\w+)\.pdf', pdf, re.I)
            if nid is None:
                nid = rss_by_title.get(norm(title).lower())
            if nid is None:
                key = (pdfkey.group(1).upper() if pdfkey else 'h' + sha(title))
                url = pdf or ''
            else:
                key = nid
                url = 'https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=%s&Mode=0' % nid
            items.append({'key': key, 'id': nid, 'title': title, 'url': url,
                          'pdf': pdf, 'date': cur_date})
    return items, count_data_rows(html)


def parse_md():
    with open('master_directions.html', encoding='utf-8', errors='ignore') as f:
        html = f.read()
    items, cur_date = [], None
    for tbl in tablebg_tables(html):
        for tr in tbl.find_all('tr'):
            hdr = tr.find('td', class_='tableheader')
            if hdr is not None:
                b = hdr.find('b')
                if b:
                    txt = norm(b.get_text())
                    cur_date = txt if re.match(r'\w+ \d{1,2}, \d{4}$', txt) else None
                continue
            tds = tr.find_all('td', recursive=False)
            if not tds:
                continue
            a = tds[0].find('a', href=True)
            if a is None:
                continue
            m = re.search(r'BS_ViewMasDirections\.aspx\?id=(\d+)', a['href'])
            if not m:
                continue
            nid = m.group(1)
            title = norm(a.get_text(' ', strip=True)) or norm(tds[0].get_text(' ', strip=True))
            pdf = ''
            for aa in tr.find_all('a', href=True):
                if '.PDF' in aa['href'].upper():
                    pdf = aa['href']
                    break
            items.append({'key': nid, 'id': nid, 'title': title,
                          'url': 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=%s' % nid,
                          'pdf': pdf, 'date': cur_date})

    if not items:
        # ponytail: safety net for a page variant where rows only live in __VIEWSTATE
        vm = re.search(r'__VIEWSTATE[^>]*value="([^"]+)"', html)
        if vm:
            try:
                dec = base64.b64decode(vm.group(1)).decode('utf-8', 'ignore')
                for m in re.finditer(r'href=BS_ViewMasDirections\.aspx\?id=(\d+)>\s*([^<]+)', dec):
                    nid, title = m.group(1), norm(m.group(2))
                    items.append({'key': nid, 'id': nid, 'title': title,
                                  'url': 'https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=%s' % nid,
                                  'pdf': '', 'date': None})
            except Exception:
                pass
    return items, len(items)


def parse_sebi_source(path):
    items = []
    if not os.path.exists(path):
        return items
    with open(path, encoding='utf-8', errors='ignore') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    for tr in soup.find_all('tr'):
        tds = tr.find_all('td', recursive=False)
        if not tds:
            continue
        a = None
        for td in tds:
            a = td.find('a', href=True)
            if a is not None and re.search(r'_(\d+)\.html', a.get('href', '')):
                break
        if a is None:
            continue
        m = re.search(r'_(\d+)\.html', a['href'])
        if not m:
            continue
        title = norm(a.get_text(' ', strip=True)) or norm(a.get('title'))
        if not title:
            continue
        date_ = norm(tds[0].get_text(' ', strip=True)) if tds[0] else None
        items.append({'key': None, 'id': m.group(1), 'title': title, 'url': a['href'],
                      'pdf': '', 'date': date_})
    return items


def parse_certin():
    with open('certin_guidelines.html', encoding='utf-8', errors='ignore') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    items, pending, cur_date = [], [], None
    for tr in soup.find_all('tr'):
        a = tr.find('a', href=lambda v: v and 'refcode=CISG-' in v)
        if a is not None:
            m = re.search(r'refcode=(CISG-\d{4}-\d+)', a['href'])
            if m:
                ref = m.group(1)
                b = a.find('b')
                title = norm(b.get_text()) if b else norm(a.get_text(' ', strip=True))
                item = {'key': ref, 'id': ref, 'title': title,
                        'url': 'https://www.cert-in.org.in/s2cMainServlet?pageid=GUIDLNVIEW02&refcode=%s' % ref,
                        'pdf': '', 'date': cur_date}
                items.append(item)
                pending.append(item)
            continue
        dc = tr.find('span', class_='DateContent')
        if dc is not None:
            m = re.match(r'\(?\s*([A-Za-z]+ \d{1,2}, \d{4})', dc.get_text(strip=True))
            if m:
                cur_date = m.group(1)
                for it in pending:
                    it['date'] = cur_date
                pending = []
    return items, None  # page duplicates refcodes in sidebar links; parity check not meaningful


def run_simple(cmd):
    if cmd == 'notif':
        items, parity = parse_notif()
        out, state = 'new_notifications.json', STATE_FILES['notif']
    elif cmd == 'md':
        items, parity = parse_md()
        out, state = 'new_master_directions.json', STATE_FILES['md']
    else:  # certin
        items, parity = parse_certin()
        out, state = 'new_certin_guidelines.json', STATE_FILES['certin']

    items = dedupe(items)
    if parity is not None and len(items) != parity:
        print('WARNING: parsed %d rows but page has %d data rows (potential miss!)' % (len(items), parity))

    sent = load_sent(state)
    if sent is None:
        sent = {i['key'] for i in items}
        if cmd == 'notif':
            sent.discard(MISSED_13652['key'])
            if not any(i.get('id') == '13652' for i in items):
                items.append(MISSED_13652)
            print('MIGRATION: sent-set seeded from page, excluding known-missed 13652')
        else:
            print('MIGRATION: sent-set seeded from current page')

    new = []
    for i in items:
        if i['key'] not in sent:
            sent.add(i['key'])
            new.append(i)

    with open(out, 'w') as f:
        json.dump(new, f, indent=2)
    write_state(state, sent)
    print('%s: %d on page, %d new' % (cmd, len(items), len(new)))
    for i in new:
        print('  [%s] %s' % (i['id'], i['title']))
    announce_new_found(bool(new))
    return new


def run_sebi():
    sent = load_sent(STATE_FILES['sebi'])
    migrated = sent is None
    if migrated:
        sent = set()
    all_new = []
    for path, tag, out in SEBI_SOURCES:
        items = parse_sebi_source(path)
        for i in items:
            i['tag'] = tag
            i['key'] = '%s/%s' % (tag, i['id'])
        items = dedupe(items)
        if migrated:
            sent |= {i['key'] for i in items}
        new = []
        for i in items:
            if i['key'] not in sent:
                sent.add(i['key'])
                new.append(i)
        all_new += new
        with open(out, 'w') as f:
            json.dump(new, f, indent=2)
        print('%s: %d on page, %d new' % (path, len(items), len(new)))
        for i in new:
            print('  [%s] %s' % (i['id'], i['title']))
    write_state(STATE_FILES['sebi'], sent)
    if migrated:
        print('MIGRATION: SEBI sent-set seeded from current pages')
    announce_new_found(bool(all_new))
    return all_new


def selftest(d):
    if d:
        os.chdir(d)
    ok = True
    notif_items, notif_parity = parse_notif()
    checks = [
        ('notif', len(notif_items) > 0 and len(notif_items) == notif_parity,
         '%d items, %d data rows' % (len(notif_items), notif_parity)),
        ('md', len(parse_md()[0]) > 0, '%d items' % len(parse_md()[0])),
        ('sebi', sum(len(parse_sebi_source(p)) for p, _, _ in SEBI_SOURCES) > 0,
         '%d items' % sum(len(parse_sebi_source(p)) for p, _, _ in SEBI_SOURCES)),
        ('certin', len(parse_certin()[0]) > 0, '%d items' % len(parse_certin()[0])),
    ]
    for name, passed, detail in checks:
        print('%s  %-6s %s' % ('PASS' if passed else 'FAIL', name, detail))
        ok = ok and passed
    return 0 if ok else 1


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == '--selftest':
        sys.exit(selftest(args[1] if len(args) > 1 else None))
    if not args or args[0] not in ('notif', 'md', 'sebi', 'certin'):
        sys.exit(__doc__)
    run_simple(args[0]) if args[0] != 'sebi' else run_sebi()
