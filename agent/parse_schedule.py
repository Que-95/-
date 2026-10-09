"""Разбор PDF с расписанием колледжа: находит группу (например 65е) и обе её бригады.

Работает по координатам слов, а не по линиям таблицы. Тот же алгоритм используется
в браузерной версии сайта, поэтому результаты совпадают.
"""
import re

DAY_KEYS = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
TIME_RE = re.compile(r'^(\d{1,2})[.:](\d{2})\s*[-–—]\s*(\d{1,2})[.:](\d{2})$')
DATE_RE = re.compile(r'(\d{2})\.(\d{2})\.(\d{4})')
TEACH_RE = re.compile(r'^[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?$')
LAT, CYR = 'aceopxykmhtb', 'асеорхукмнтв'


def norm(s):
    """Нижний регистр и замена латинских букв-двойников на кириллические (65e -> 65е)."""
    return ''.join(CYR[LAT.index(c)] if c in LAT else c for c in str(s).lower())


def group_lines(items, tol):
    out = []
    for o in sorted(items, key=lambda o: (o['y'], o['x'])):
        if out and o['y'] - out[-1]['y'] <= tol:
            out[-1]['items'].append(o)
        else:
            out.append({'y': o['y'], 'items': [o]})
    for line in out:
        line['items'].sort(key=lambda o: o['x'])
    return out


def to_lesson(lines, aud_lines, n, time):
    L = list(lines)
    teacher = typ = ''
    if L and TEACH_RE.match(L[-1]):
        teacher = L.pop()
    for i, line in enumerate(L):
        if re.match(r'^\(.+\)$', line):
            typ = line[1:-1]
            del L[i]
            break
    room = ''
    for i, line in enumerate(aud_lines):
        room += (' ' if i and not re.match(r'^[а-яё]', line) else '') + line
    return {
        'n': n,
        'time': time,
        'subject': re.sub(r'\s+', ' ', ' '.join(L)).strip(),
        'type': typ,
        'teacher': teacher,
        'room': room.replace('_', ' ').strip(),
    }


def parse_table(it, y0, y1):
    R = [o for o in it if y0 <= o['y'] < y1]

    # заголовки дней недели -> горизонтальные границы колонок
    days = []
    for o in R:
        m = re.match(r'^(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)', o['s'], re.I)
        if not m:
            continue
        idx = DAY_KEYS.index(m.group(1).lower())
        if any(d['idx'] == idx for d in days):
            continue
        right = o['x'] + o['w']
        d = DATE_RE.search(o['s'])
        if not d:
            near = sorted((p for p in R if abs(p['y'] - o['y']) < 2.5 and p['x'] >= o['x'] and DATE_RE.search(p['s'])),
                          key=lambda p: p['x'])
            if near:
                d = DATE_RE.search(near[0]['s'])
                right = near[0]['x'] + near[0]['w']
        days.append({'idx': idx, 'cx': (o['x'] + right) / 2, 'y': o['y'], 'date': d.group(0) if d else ''})
    if not days:
        return None
    days.sort(key=lambda d: d['cx'])
    hy = min(d['y'] for d in days)
    half = (days[-1]['cx'] - days[0]['cx']) / (len(days) - 1) / 2 if len(days) > 1 else 130
    for k, d in enumerate(days):
        d['l'] = (days[k - 1]['cx'] + d['cx']) / 2 if k else d['cx'] - half
        d['r'] = (d['cx'] + days[k + 1]['cx']) / 2 if k < len(days) - 1 else d['cx'] + half

    # колонки «Ауд.»
    auds = [o['x'] + o['w'] / 2 for o in R if o['s'].startswith('Ауд') and hy < o['y'] < hy + 35]
    for d in days:
        a = next((c for c in auds if d['l'] <= c < d['r']), None)
        d['ac'] = a if a is not None else d['r'] - 31

    # строки таблицы по подписям времени
    labels = sorted((o for o in R if TIME_RE.match(o['s']) and o['x'] + o['w'] / 2 < days[0]['l'] and o['y'] > hy),
                    key=lambda o: o['y'])
    if not labels:
        return None
    sp = (labels[-1]['y'] - labels[0]['y']) / (len(labels) - 1) if len(labels) > 1 else 62

    cells = {}
    for o in R:
        if o['y'] < labels[0]['y'] - 4:
            continue
        c = o['x'] + o['w'] / 2
        if c < days[0]['l'] - 3 or c > days[-1]['r'] + 3:
            continue
        k = next((i for i, d in enumerate(days) if d['l'] <= c < d['r']), -1)
        if k < 0:
            continue
        row = -1
        for i, lb in enumerate(labels):
            if lb['y'] - 4 <= o['y']:
                row = i
        if row < 0 or o['y'] >= labels[-1]['y'] + sp - 4:
            continue
        cell = cells.setdefault((row, k), {'d': [], 'a': []})
        (cell['a'] if o['x'] >= days[k]['ac'] - 42 else cell['d']).append(o)

    out_days = [[] for _ in range(7)]

    def txt(arr):
        return [' '.join(o['s'] for o in line['items']) for line in group_lines(arr, 3)]

    for (row, k) in sorted(cells):
        c = cells[(row, k)]
        if not c['d']:
            continue
        m = TIME_RE.match(labels[row]['s'])
        time = '%02d:%s–%02d:%s' % (int(m.group(1)), m.group(2), int(m.group(3)), m.group(4))
        out_days[days[k]['idx']].append(to_lesson(txt(c['d']), txt(c['a']), row + 1, time))

    dates = [''] * 7
    for d in days:
        dates[d['idx']] = d['date']
    return {'days': out_days, 'dates': dates}


def parse_document(pages, target):
    """pages: список страниц, каждая — список {'str','x','y','w'}.
    Возвращает бригады группы target: {'brigades': {'1': [...7 дней], '2': [...]}, 'dates', 'week', 'found'}."""
    t = norm(target).strip()
    res = {'brigades': {}, 'dates': [''] * 7, 'week': '', 'found': []}
    for page in pages:
        it = [{'s': str(o['str']).strip(), 'x': o['x'], 'y': o['y'], 'w': o.get('w', 0)} for o in page]
        it = [o for o in it if o['s']]
        heads = []
        for line in group_lines(it, 2.5):
            text = ' '.join(o['s'] for o in line['items'])
            m = re.match(r'^Группа\s*-\s*(\S+)', text)
            w = re.search(r'\d+-я\s+неделя', text, re.I)
            if w and not res['week']:
                res['week'] = w.group(0)
            if m:
                heads.append({'y': line['y'], 'name': norm(m.group(1))})
                res['found'].append(m.group(1))
        heads.sort(key=lambda h: h['y'])
        for i, h in enumerate(heads):
            m = re.match(r'^(.+?)(?:-(\d+))?$', h['name'])
            if not m or m.group(1) != t:
                continue
            y1 = heads[i + 1]['y'] - 1 if i + 1 < len(heads) else float('inf')
            tb = parse_table(it, h['y'] - 1, y1)
            if not tb:
                continue
            res['brigades'][m.group(2) or '1'] = tb['days']
            for j, d in enumerate(tb['dates']):
                if d and not res['dates'][j]:
                    res['dates'][j] = d
    return res


def pdf_to_pages(path):
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            pages.append([{'str': w['text'], 'x': w['x0'], 'y': w['bottom'], 'w': w['x1'] - w['x0']}
                          for w in p.extract_words()])
    return pages
