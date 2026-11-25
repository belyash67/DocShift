"""Запуск миграции, протоколирование ошибок и проверка результата."""
from contextlib import closing
import sqlite3
import time
from pathlib import Path
from .analyzer import open_source, qid
from .mapper import SCHEMAS, now_iso
from .validator import validate_department, validate_user, validate_document

TARGET_DDL = '''
PRAGMA foreign_keys=ON;
CREATE TABLE departments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 legacy_id INTEGER NOT NULL UNIQUE,
 name TEXT NOT NULL,
 migrated_at TEXT NOT NULL
);
CREATE TABLE users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 legacy_id INTEGER NOT NULL UNIQUE,
 full_name TEXT NOT NULL,
 email TEXT,
 migrated_at TEXT NOT NULL
);
CREATE TABLE documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 legacy_id INTEGER NOT NULL UNIQUE,
 title TEXT NOT NULL,
 author_id INTEGER NOT NULL REFERENCES users(id),
 department_id INTEGER NOT NULL REFERENCES departments(id),
 created_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
 file_path TEXT,
 migrated_at TEXT NOT NULL
);
CREATE INDEX idx_documents_author ON documents(author_id);
CREATE INDEX idx_documents_department ON documents(department_id);
'''


def run_migration(project_id, source_path, destination, selected, update_progress):
    """Создать новую БД и перенести корректные записи. Вызывается рабочим потоком.

    update_progress(done, total, phase) сохраняет реальный прогресс в app.db.
    """
    start = time.monotonic()
    selected = [name for name in SCHEMAS if name in selected]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Целевая база для этого запуска уже существует')
    errors, logs, counts = [], [], {k: 0 for k in selected}
    migrated_ids = {k: set() for k in selected}
    success = 0
    with closing(open_source(source_path)) as src:
        totals = {name: src.execute(f'SELECT COUNT(*) FROM {qid(name)}').fetchone()[0] for name in selected}
        total = sum(totals.values())
        update_progress(0, total, 'Создание новой структуры')
        dst = sqlite3.connect(destination)
        try:
            dst.executescript(TARGET_DDL)
            dst.execute('BEGIN')
            now = now_iso()
            users, departments = {}, {}
            processed = 0
            for name in selected:
                logs.append((now_iso(), f'Начата обработка {name}: {totals[name]} строк'))
                update_progress(processed, total, 'Обработка таблицы ' + name)
                seen = set()
                # fetchmany: не загружаем всю таблицу в оперативную память.
                cur = src.execute(f'SELECT * FROM {qid(name)}')
                while True:
                    rows = cur.fetchmany(100)
                    if not rows:
                        break
                    for row in rows:
                        processed += 1
                        try:
                            source_id = row[SCHEMAS[name]['fields'][0][0]]
                            if source_id in seen:
                                raise ValueError(f'Повторяющийся ID: {source_id}')
                            seen.add(source_id)
                            if name == 'old_departments':
                                key, value = validate_department(row)
                                dst.execute('INSERT INTO departments(legacy_id,name,migrated_at) VALUES(?,?,?)', (key, value, now))
                                departments[key] = dst.execute('SELECT last_insert_rowid()').fetchone()[0]
                            elif name == 'old_users':
                                key, value, email = validate_user(row)
                                dst.execute('INSERT INTO users(legacy_id,full_name,email,migrated_at) VALUES(?,?,?,?)', (key, value, email, now))
                                users[key] = dst.execute('SELECT last_insert_rowid()').fetchone()[0]
                            else:
                                data = validate_document(row, users, departments)
                                key = data[0]
                                dst.execute('''INSERT INTO documents
                                    (legacy_id,title,author_id,department_id,created_at,status,file_path,migrated_at)
                                    VALUES(?,?,?,?,?,?,?,?)''', (*data, now))
                            success += 1
                            counts[name] += 1
                            migrated_ids[name].add(key)
                        except (ValueError, sqlite3.IntegrityError, TypeError) as ex:
                            id_column = SCHEMAS[name]['fields'][0][0]
                            old_id = str(row[id_column]) if id_column in row.keys() else 'неизвестно'
                            errors.append((name, old_id, str(ex)))
                        if processed % 15 == 0 or processed == total:
                            update_progress(processed, total, 'Обработка таблицы ' + name)
                logs.append((now_iso(), f'{name}: успешно {counts[name]}, ошибок {totals[name] - counts[name]}'))
            update_progress(total, total, 'Проверка целостности')
            dst.commit()
            actual = {SCHEMAS[name]['target']: dst.execute(f'SELECT COUNT(*) FROM {SCHEMAS[name]["target"]}').fetchone()[0] for name in selected}
            fk_errors = dst.execute('PRAGMA foreign_key_check').fetchall()
            integrity = dst.execute('PRAGMA integrity_check').fetchone()[0]
            count_match = all(actual[SCHEMAS[name]['target']] == counts[name] for name in selected)
            ids_match = all(
                {row[0] for row in dst.execute(f'SELECT legacy_id FROM {SCHEMAS[name]["target"]}')}
                == migrated_ids[name] for name in selected
            )
            passed = (success + len(errors) == total and count_match and ids_match
                      and not fk_errors and integrity == 'ok')
            logs.append((now_iso(), 'Проверка целостности: ' + ('успешно' if passed else 'ОШИБКА')))
            if not passed:
                raise RuntimeError('Результат не прошёл проверку целостности')
            return {'total': total, 'success': success, 'failed': len(errors), 'documents': counts.get('old_documents', 0),
                    'duration': round(time.monotonic() - start, 2), 'integrity': True,
                    'details': {name: {'source': totals[name], 'migrated': counts[name], 'failed': totals[name] - counts[name]} for name in selected},
                    'errors': errors, 'logs': logs}
        finally:
            dst.close()
