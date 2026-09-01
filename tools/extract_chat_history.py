"""Extract site-log rows from a WhatsApp group export.

Deterministic: no LLM. Captions in this group follow
    <Main Location> (<sub>): <description>
so we split on the first colon, pull any parenthetical into sub_location,
and flatten bullet lists into one sentence.
"""
import re, json, sys
from datetime import date

HDR = re.compile(r'^\[(\d{1,2})/(\d{1,2})/(\d{2}), (\d{1,2}:\d{2}:\d{2}\s?[AP]M)\] ([^:]{1,60}): ?(.*)$')
MEDIA = re.compile(r'(image|video|audio|sticker|document|GIF|Contact card) omitted', re.I)
SYSTEM = re.compile(r'(was added|left$|joined|changed the group|changed their phone|'
                    r'You\'re now an admin|added|removed|pinned a message|deleted this message|'
                    r'end-to-end encrypted|created group|changed the subject)', re.I)
BOT = re.compile(r'^(✅|⚠️|📊|📋|🏗️|/(help|daily|excel|excel2|dwall|ask|setorder|reorder|confirm|delete))')

# A main location looks like B2-10, CCW1, P46, S1-1, CCE ramp, GL28-32, Zone 3 ...
LOC = re.compile(r'^(?=.*[0-9A-Z])[A-Za-z0-9][A-Za-z0-9 ,/&\-\.\'"()#\+]{0,58}$')
HAS_TOKEN = re.compile(r'([A-Z]{1,4}[\-\s]?\d|\d)')


def parse_messages(path):
    raw = open(path, encoding='utf-8', errors='replace').read()
    raw = raw.replace('\u200e', '').replace('\r', '')
    out, cur = [], None
    for line in raw.split('\n'):
        m = HDR.match(line)
        if m:
            if cur:
                out.append(cur)
            d, mo, y, t, sender, body = m.groups()
            cur = {'date': date(2000 + int(y), int(mo), int(d)), 'time': t,
                   'sender': sender.strip(), 'body': body}
        elif cur is not None:
            cur['body'] += '\n' + line
    if cur:
        out.append(cur)
    return out


def clean_body(body):
    body = MEDIA.sub('', body)
    # normalise bullets and the U+2060-ish leading chars WhatsApp injects
    body = re.sub(r'^[\s\-\u2022\u2060]+', '', body, flags=re.M)
    return body.strip()


def flatten(desc):
    lines = [l.strip(' .') for l in desc.split('\n') if l.strip(' .-\u2022')]
    joined = ', '.join(lines)
    joined = re.sub(r'\s+', ' ', joined).strip(' .,')
    if joined:
        joined = joined[0].upper() + joined[1:]
    return joined


def split_location(loc):
    """'B2-17 (North side)' -> ('B2-17', 'North side'); 'B2 GL28-32' -> ('B2','GL28-32')"""
    sub = ''
    m = re.search(r'\(([^)]*)\)', loc)
    if m:
        sub = m.group(1).strip()
        loc = (loc[:m.start()] + loc[m.end():]).strip()
    # trailing grid-line reference becomes the sub-location
    m = re.match(r'^(.*?)\s+(GL[\w\-/ ]+|[NSEW]\w*\s?side)$', loc, re.I)
    if m and m.group(1).strip():
        loc, extra = m.group(1).strip(), m.group(2).strip()
        sub = f'{sub}, {extra}' if sub else extra
    loc = re.sub(r'^(opening at|at|for)\s+', '', loc, flags=re.I).strip(' ,-')
    return loc, sub


def extract(messages):
    rows = []
    for m in messages:
        body = clean_body(m['body'])
        if not body or SYSTEM.search(body) or BOT.match(body):
            continue
        head, sep, rest = body.partition(':')
        if not sep:
            continue
        head = head.strip()
        rest = rest.strip()
        if not rest or len(head) > 60 or '\n' in head:
            continue
        if not LOC.match(head) or not HAS_TOKEN.search(head):
            continue
        loc, sub = split_location(head)
        desc = flatten(rest)
        if not loc or not desc or len(desc) < 4:
            continue
        rows.append({'log_date': m['date'].isoformat(), 'logged_at': m['time'],
                     'sender_name': m['sender'], 'main_location': loc,
                     'sub_location': sub, 'description': desc, 'manpower': ''})
    rows.sort(key=lambda r: (r['log_date'], r['logged_at']))
    return rows


if __name__ == '__main__':
    msgs = parse_messages(sys.argv[1])
    rows = extract(msgs)
    print(f'messages={len(msgs)} extracted={len(rows)}', file=sys.stderr)
    json.dump(rows, open(sys.argv[2], 'w'), indent=1)
