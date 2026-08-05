#!/usr/bin/env python3
"""Regulatory monitor extraction for RBI/SEBI/CERT-In.

Parses the fetched HTML pages into item lists, tracks a per-source
multi-identity state (id / pdf-hash / title-hash) plus a date watermark,
and writes new_*.json for the email step plus the updated state files.

Identity model (A2): each notification is remembered by a *set* of
identifiers - the numeric Id when the row has a link, a hash of the PDF
filename, and a hash of the normalized title. RBI publishes rows without
the Id link and adds it later; an item is only "new" when none of its
identities has been seen before. That makes page mutations idempotent.

Date watermark (B4): items whose page date is older than yesterday are
held back (marked seen, not emailed) so delayed/backfilled rows never
trigger a misleading "new notification" email. Fresh items pass.

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
from datetime import date
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

SEBI_SOURCES = [
    ('sebi_pr.html',                 'SEBI PRESS RELEASE',      'new_sebi_pr.json'),
    ('sebi_public_notice.html',      'SEBI PUBLIC NOTICE',      'new_sebi_public_notice.json'),
    ('sebi_news_clarification.html', 'SEBI NEWS CLARIFICATION', 'new_sebi_news_clarification.json'),
    ('sebi_speeches.html',           'SEBI SPEECH',             'new_sebi_speeches.json'),
]

DATE_RE = re.compile(r'(\w+)\s+(\d{1,2}),?\s+(\d{4})')
MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10,
    'november': 11, 'december': 12,
}


def norm(s):
    s = s or ''
    for dash in ('\u2013', '\u2014', '\u2212'):
        s = s.replace(dash, '-')
    return re.sub(r'\s+', ' ', s).strip()


def sha(t):
    return hashlib.sha1(t.encode('utf-8')).hexdigest()[:16]


def pdf_basename(pdf):
    m = re.search(r'/([A-Za-z0-9]{20,})\.pdf', pdf or '', re.I)
    return m.group(1) if m else None


def parse_date(text):
    m = DATE_RE.match(text) if text else None
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if mon is None:
        return None
    return date(int(m.group(3)), mon, int(m.group(2)))


def cutoff():
    d = date.today()
    if d.day > 1:
        return d.replace(day=d.day - 1)
    if d.month > 1:
        return d.replace(month=d.month - 1, day=28)
    return d.replace(year=d.year - 1, month=12, day=28)


def within_watermark(item_date_text):
    if not item_date_text:
        return True
    d = parse_date(item_date_text)
    return d is None or d >= cutoff()


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


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def write_state(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def announce_new_found(flag):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write("new_found=%s\n" % ('true' if flag else 'false'))


def idens(item):
    """Every stable identifier for an item, as a set of strings."""
    s = {item['key']}
    if item.get('id'):
        s.add(str(item['id']))
    if item.get('pdf'):
        s.add('p' + sha(item['pdf']))
    if item.get('title'):
        s.add('h' + sha(item['title']))
    return s


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
            if nid is None:
                nid = rss_by_title.get(norm(title).lower())
            if nid:
                key = nid
                link = 'https://www.rbi.org.in/scripts/NotificationUser.aspx?Id=%s&Mode=0' % nid
            else:
                key = pdf_basename(pdf) or 'h' + sha(title)
                link = pdf or ''
            items.append({'key': key, 'id': nid, 'title': title, 'url': link,
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
        if not DATE_RE.match(date_ or ''):
            date_ = None
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
    return items, None


def run_simple(cmd):
    if cmd == 'notif':
        items, parity = parse_notif()
        out, state_path = 'new_notifications.json', STATE_FILES['notif']
    elif cmd == 'md':
        items, parity = parse_md()
        out, state_path = 'new_master_directions.json', STATE_FILES['md']
    else:  # certin
        items, parity = parse_certin()
        out, state_path = 'new_certin_guidelines.json', STATE_FILES['certin']

    items = dedupe(items)
    if isinstance(parity, int) and len(items) != parity:
        print('WARNING: parsed %d rows but page has %d data rows (potential miss!)' % (len(items), parity))

    state = load_state(state_path)
    if state is None:
        state = {'idens': [], 'held': {}}
        print('MIGRATION: fresh multi-identity state')
    elif isinstance(state, list):
        state = {'idens': list(state), 'held': {}}
        print('MIGRATION: legacy flat sent-set -> multi-identity state')

    known = set(state.get('idens', []))
    held_map = state.get('held', {})
    new, held = [], []

    for i in items:
        i_idens = idens(i)
        if i_idens & known:
            known |= i_idens
            held_map.pop(i['key'], None)
            continue
        if within_watermark(i.get('date')):
            known |= i_idens
            new.append(i)
            held_map.pop(i['key'], None)
        else:
            d = parse_date(i.get('date') or '')
            held_map[i['key']] = d.isoformat() if d else (i.get('date') or '')
            known |= i_idens

    state['idens'] = sorted(known)
    state['held'] = held_map

    with open(out, 'w') as f:
        json.dump(new, f, indent=2)
    write_state(state_path, state)

    print('%s: %d on page, %d new, %d held (older than watermark)' %
          (cmd, len(items), len(new), len(held)))
    for i in new:
        print('  [%s] %s' % (i.get('id') or i['key'], i.get('title', '')))
    for i in held:
        print('  HELD [%s] %s (%s)' % (i.get('id') or i['key'], i.get('title', ''), i.get('date')))
    announce_new_found(bool(new))
    return new


def run_sebi():
    state = load_state(STATE_FILES['sebi'])
    if state is None:
        state = {'idens': [], 'held': {}}
        print('MIGRATION: fresh SEBI multi-identity state')
    elif isinstance(state, list):
        state = {'idens': list(state), 'held': {}}
        print('MIGRATION: legacy flat SEBI sent-set -> multi-identity state')

    known = set(state.get('idens', []))
    held_map = state.get('held', {})
    all_new = []

    for path, tag, out in SEBI_SOURCES:
        items = parse_sebi_source(path)
        for i in items:
            i['tag'] = tag
            i['key'] = '%s/%s' % (tag, i['id'])
        items = dedupe(items)
        path_new, path_held = [], []
        for i in items:
            i_idens = idens(i)
            if i_idens & known:
                known |= i_idens
                held_map.pop(i['key'], None)
                continue
            if within_watermark(i.get('date')):
                known |= i_idens
                path_new.append(i)
                held_map.pop(i['key'], None)
            else:
                known |= i_idens
                path_held.append(i)
                held_map[i['key']] = i.get('date') or ''
        all_new += path_new
        with open(out, 'w') as f:
            json.dump(path_new, f, indent=2)
        print('%s: %d on page, %d new, %d held' % (path, len(items), len(path_new), len(path_held)))
        for i in path_new:
            print('  [%s] %s' % (i['id'], i['title']))
        for i in path_held:
            print('  HELD [%s] %s (%s)' % (i['id'], i['title'], i.get('date')))

    state['idens'] = sorted(known)
    state['held'] = held_map
    write_state(STATE_FILES['sebi'], state)
    announce_new_found(bool(all_new))
    return all_new


def selftest(d):
    if d:
        os.chdir(d)
    ok = True

    def check(name, passed, detail):
        nonlocal ok
        print('%s  %-10s %s' % ('PASS' if passed else 'FAIL', name, detail))
        ok = ok and passed

    notif_items, notif_parity = parse_notif()
    check('notif', len(notif_items) > 0 and len(notif_items) == notif_parity,
          '%d items, %d data rows' % (len(notif_items), notif_parity))
    check('md', len(parse_md()[0]) > 0, '%d items' % len(parse_md()[0]))
    check('sebi', sum(len(parse_sebi_source(p)) for p, _, _ in SEBI_SOURCES) > 0,
          '%d items' % sum(len(parse_sebi_source(p)) for p, _, _ in SEBI_SOURCES))
    check('certin', len(parse_certin()[0]) > 0, '%d items' % len(parse_certin()[0]))

    check('parse_date', parse_date('Jul 31, 2026') == date(2026, 7, 31), 'Jul 31, 2026')
    check('parse_date_full', parse_date('September 01, 2025') == date(2025, 9, 1), 'September 01, 2025')
    check('watermark_holds_old', not within_watermark('Jul 09, 2026'), 'Jul 09, 2026 is stale')
    check('watermark_passes_fresh', within_watermark(date.today().strftime('%b %d, %Y')), 'today passes')

    item = {'key': 'ABC', 'id': '123', 'title': 'Foo Bar', 'pdf': 'https://x/PDF12345.PDF'}
    idset = idens(item)
    check('idens_cover_all', len(idset) == 4, sorted(idset))
    check('pdf_basename', pdf_basename('https://x/419MD2FBDBDE1A8C948B79579A76A9BC83E21.PDF') ==
          '419MD2FBDBDE1A8C948B79579A76A9BC83E21', 'generalized pdf key')

    return 0 if ok else 1


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == '--selftest':
        sys.exit(selftest(args[1] if len(args) > 1 else None))
    if not args or args[0] not in ('notif', 'md', 'sebi', 'certin'):
        sys.exit(__doc__)
    if args[0] == 'sebi':
        run_sebi()
    else:
        run_simple(args[0])
