#!/usr/bin/env python3
"""Агент ВК: забирает PDF с расписанием из документов сообщества и (по желанию)
сообщения куратора со стены. Складывает данные в docs/data/ для сайта.

Что делает:
  * каждый новый PDF разбирается, и расписание недели сохраняется отдельным файлом
    docs/data/weeks/ГГГГ-ММ-ДД.json (дата понедельника). Старые недели не удаляются;
  * недели, которые админ поправил вручную на сайте (edited: true), агент не перезаписывает;
  * записи стены с хештегом становятся сообщениями; сообщения, добавленные
    админом вручную, агент не трогает.

Настройки — переменные окружения:
  VK_TOKEN       ключ доступа (сообщества или пользователя)            [обязательно]
  VK_GROUP_ID    числовой ID сообщества, без минуса                      [обязательно]
  GROUP_NAME     название группы в PDF                                  [по умолчанию 65е]
  DOC_PATTERN    часть названия файла (регулярное выражение)            [по умолчанию Расписание]
  WALL_TAG       хештег записей куратора на стене, например #куратор    [пусто — стена не читается]
  OUT_DIR        папка сайта                                            [по умолчанию docs]
  FORCE          1 — разобрать все PDF заново (кроме правленых вручную недель)
"""
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_schedule as ps  # noqa: E402

API = 'https://api.vk.com/method/'
API_VERSION = '5.199'
MAX_PER_RUN = 15
HINTS = {
    5: 'ключ недействителен или отозван — создайте новый',
    7: 'у ключа нет права на это действие',
    15: 'доступ запрещён: включите раздел «Документы» в сообществе и дайте ключу доступ к документам',
    27: 'этот метод не работает с ключом сообщества — используйте ключ пользователя',
    100: 'неверный параметр — проверьте VK_GROUP_ID',
    203: 'доступ к сообществу закрыт для этого ключа',
}


class AgentError(Exception):
    pass


class GroupNotFound(AgentError):
    pass


def vk_call(method, params, token):
    q = dict(params, access_token=token, v=API_VERSION)
    req = urllib.request.Request(API + method + '?' + urllib.parse.urlencode(q),
                                 headers={'User-Agent': 'schedule-agent/2.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError) as e:
        raise AgentError('не удалось связаться с ВК: %s' % e)
    if 'error' in data:
        e = data['error']
        code = e.get('error_code')
        raise AgentError('ВК вернул ошибку %s: %s%s' % (code, e.get('error_msg'),
                                                       ' — ' + HINTS[code] if code in HINTS else ''))
    return data['response']


def download(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 schedule-agent/2.0'})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        f.write(r.read())
    with open(dest, 'rb') as f:
        if f.read(5) != b'%PDF-':
            raise AgentError('скачан не PDF (ссылка устарела или файл недоступен)')


def matching_docs(items, pattern):
    rx = re.compile(pattern, re.I)
    docs = [d for d in items if str(d.get('ext', '')).lower() == 'pdf' and rx.search(d.get('title', ''))]
    docs.sort(key=lambda d: d.get('date', 0))
    return docs


def doc_key(d):
    return '%s_%s_%s' % (d.get('owner_id'), d.get('id'), d.get('date'))


def week_id(dates):
    """Дата понедельника недели (ГГГГ-ММ-ДД) по датам из заголовков таблицы."""
    for s in dates:
        m = ps.DATE_RE.search(s or '')
        if m:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return (d - timedelta(days=d.weekday())).isoformat()
    return ''


def wall_messages(posts, tag):
    out = []
    t = tag.lower()
    for p in posts:
        text = p.get('text') or ''
        if t not in text.lower():
            continue
        clean = re.sub(re.escape(tag), '', text, flags=re.I).strip()
        clean = re.sub(r'[ \t]+\n', '\n', clean)
        if not clean:
            continue
        at = datetime.fromtimestamp(p['date'], tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        out.append({'id': 'vk%s' % p['id'], 'at': at, 'title': '', 'text': clean[:1500],
                    'pin': bool(p.get('is_pinned')), 'src': 'vk'})
    return out


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default


def save_json(path, obj):
    """Пишет файл, только если содержимое изменилось. Возвращает True, если записал."""
    text = json.dumps(obj, ensure_ascii=False, indent=1) + '\n'
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return True


def core(w):
    return {k: w.get(k) for k in ('group', 'week', 'dates', 'brigades')}


def run(env, call=vk_call, fetch=download, log=print):
    token = env.get('VK_TOKEN')
    gid = (env.get('VK_GROUP_ID') or '').lstrip('-')
    if not token or not gid.isdigit():
        raise AgentError('задайте VK_TOKEN и числовой VK_GROUP_ID')
    group = env.get('GROUP_NAME') or '65е'
    pattern = env.get('DOC_PATTERN') or 'Расписание'
    tag = (env.get('WALL_TAG') or '').strip()
    out = env.get('OUT_DIR') or 'docs'
    force = env.get('FORCE') == '1'
    data_dir = os.path.join(out, 'data')
    idx_path = os.path.join(data_dir, 'index.json')
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    changed = False

    idx = load_json(idx_path, {'weeks': [], 'done': []})
    done = set(idx.get('done', []))
    weeks = set(idx.get('weeks', []))
    warnings = []

    # --- расписание из PDF ---
    items = call('docs.get', {'owner_id': -int(gid), 'count': 200}, token).get('items', [])
    docs = matching_docs(items, pattern)
    if not docs:
        raise AgentError('в документах сообщества нет PDF с названием по шаблону «%s» (всего документов: %d)'
                         % (pattern, len(items)))
    todo = [d for d in docs if force or doc_key(d) not in done][-MAX_PER_RUN:]
    if not todo:
        log('Новых файлов расписания нет.')
    for doc in todo:
        key = doc_key(doc)
        try:
            log('Разбираю «%s»…' % doc['title'])
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'schedule.pdf')
                fetch(doc['url'], path)
                r = ps.parse_document(ps.pdf_to_pages(path), group)
            if not r['brigades']:
                found = ', '.join(sorted(set(r['found']))[:12])
                raise GroupNotFound('группа «%s» не найдена. В файле есть: %s' % (group, found or 'ничего похожего'))
            wid = week_id(r['dates'])
            if not wid:
                raise GroupNotFound('в таблице не нашлись даты')
        except GroupNotFound as e:
            warnings.append('«%s»: %s' % (doc['title'], e))
            log('Пропускаю «%s»: %s' % (doc['title'], e))
            done.add(key)
            continue
        except AgentError as e:
            warnings.append('«%s»: %s' % (doc['title'], e))
            log('Не удалось обработать «%s»: %s (попробую в следующий раз)' % (doc['title'], e))
            continue

        wpath = os.path.join(data_dir, 'weeks', wid + '.json')
        old = load_json(wpath, None)
        done.add(key)
        weeks.add(wid)
        if old and old.get('edited'):
            log('Неделя %s правилась вручную на сайте — файл не трогаю.' % wid)
            continue
        new = {'id': wid, 'group': group, 'week': r['week'], 'dates': r['dates'], 'brigades': r['brigades'],
               'source': doc['title'], 'docKey': key, 'edited': False,
               'updatedAt': now}
        if old and core(old) == core(new):
            new['updatedAt'] = old.get('updatedAt', now)
            log('Неделя %s: расписание то же самое.' % wid)
        else:
            log('Неделя %s (%s): %s, бригады: %s.' % (wid, r['week'] or 'номер не указан',
                                                       'обновлена' if old else 'добавлена в архив',
                                                       ', '.join(sorted(r['brigades']))))
        if save_json(wpath, new):
            changed = True

    if not weeks:
        raise AgentError('не удалось разобрать ни одного файла. ' + ' '.join(warnings))

    idx = {'weeks': sorted(weeks), 'done': sorted(done)[-300:]}
    if save_json(idx_path, idx):
        changed = True

    # --- сообщения куратора со стены ---
    if tag:
        posts = call('wall.get', {'owner_id': -int(gid), 'count': 50}, token).get('items', [])
        mpath = os.path.join(data_dir, 'messages.json')
        cur = load_json(mpath, {'items': [], 'hidden': []})
        manual = [m for m in cur.get('items', []) if m.get('src') != 'vk']
        merged = sorted(manual + wall_messages(posts, tag), key=lambda m: m['at'], reverse=True)[:100]
        new_msgs = {'items': merged, 'hidden': cur.get('hidden', [])}
        if save_json(mpath, new_msgs):
            changed = True
            log('Сообщения обновлены: %d.' % len(merged))

    log('Готово: есть изменения.' if changed else 'Изменений нет.')
    return changed


if __name__ == '__main__':
    try:
        run(os.environ)
    except AgentError as e:
        print('Ошибка: %s' % e, file=sys.stderr)
        sys.exit(1)
