"""Безопасное чтение пользовательской SQLite-базы (только чтение)."""
from contextlib import closing
import sqlite3
from pathlib import Path
from urllib.parse import quote
from .mapper import SCHEMAS


def open_source(path):
    absolute = Path(path).resolve()
    # Нельзя модифицировать исходную БД. mode=ro делает соединение readonly.
    conn = sqlite3.connect(f'{absolute.as_uri()}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only = ON')
    return conn


def qid(name):
    return '"' + name.replace('"', '""') + '"'


def inspect_database(path):
    with open(path, 'rb') as file:
        if file.read(16) != b'SQLite format 3\x00':
            raise ValueError('Файл не является базой данных SQLite')
    with closing(open_source(path)) as conn:
        ok = conn.execute('PRAGMA quick_check').fetchone()[0]
        if ok != 'ok':
            raise ValueError('База повреждена: ' + str(ok))
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        result = []
        for item in tables:
            name = item['name']
            columns = [dict(c) for c in conn.execute(f'PRAGMA table_info({qid(name)})')]
            foreign_keys = [dict(k) for k in conn.execute(f'PRAGMA foreign_key_list({qid(name)})')]
            count = conn.execute(f'SELECT COUNT(*) FROM {qid(name)}').fetchone()[0]
            sample = [dict(r) for r in conn.execute(f'SELECT * FROM {qid(name)} LIMIT 5')]
            expected = [old for old, _ in SCHEMAS.get(name, {}).get('fields', [])]
            actual = [c['name'] for c in columns]
            missing = [x for x in expected if x not in actual]
            result.append({'name': name, 'count': count, 'columns': columns, 'foreign_keys': foreign_keys,
                           'sample': sample, 'supported': name in SCHEMAS,
                           'missing': missing,
                           'target': SCHEMAS.get(name, {}).get('target')})
        return result
