"""Локальный HTTP-сервер DocShift без Flask и установки зависимостей.

HTML-шаблоны обрабатывает Jinja2, включённая в vendor/ как чистый Python.
Маршруты доступны только на 127.0.0.1, без доступа из сети колледжа.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import traceback
import webbrowser
from contextlib import closing, contextmanager
from email import policy
from email.parser import BytesParser
from email.utils import encode_rfc2231
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from uuid import uuid4

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'vendor'))
from jinja2 import Environment, FileSystemLoader, select_autoescape

from create_test_database import create_demo
from migration.analyzer import inspect_database
from migration.mapper import SCHEMAS, now_iso
from migration.migrator import run_migration
from migration.folders import write_uploaded_tree, write_uploaded_zip, convert_tree, valid_path, MAX_FILES

DATA = BASE / 'database'
UPLOADS = BASE / 'uploads'
GENERATED = DATA / 'generated'
FOLDER_RUNS = DATA / 'folder_runs'
APP_DB = DATA / 'app.db'
CSRF = secrets.token_hex(20)
MAX_UPLOAD = 160 * 1024 * 1024
SQLITE_LIMIT = 20 * 1024 * 1024
VALID_SUFFIXES = {'.db', '.sqlite', '.sqlite3'}

for location in (DATA, UPLOADS, GENERATED, FOLDER_RUNS):
    location.mkdir(parents=True, exist_ok=True)

TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(BASE / 'templates'),
    autoescape=select_autoescape(['html', 'xml']),
)


def url_for(endpoint, **kwargs):
    """Маршруты, используемые существующими HTML-шаблонами."""
    names = {
        'dashboard': '/', 'new_migration': '/migrations/new',
        'upload': '/migrations/upload', 'demo': '/migrations/demo',
        'history': '/history', 'all_errors': '/errors', 'about': '/about',
        'folder_sample': '/folders/sample/download',
    }
    if endpoint == 'static':
        return '/static/' + quote(kwargs.pop('filename'), safe='/')
    elif endpoint in names:
        result = names[endpoint]
    elif endpoint in ('preview','select_tables','mapping','process','result','download',
                      'project_errors','project_logs','start','status_api'):
        id = int(kwargs.pop('id'))
        suffixes = {
            'preview': f'/projects/{id}/preview',
            'select_tables': f'/projects/{id}/select',
            'mapping': f'/projects/{id}/mapping',
            'process': f'/projects/{id}/process',
            'result': f'/projects/{id}/result',
            'download': f'/projects/{id}/download',
            'project_errors': f'/projects/{id}/errors',
            'project_logs': f'/projects/{id}/logs',
            'start': f'/api/projects/{id}/start',
            'status_api': f'/api/projects/{id}/status',
        }
        result = suffixes[endpoint]
    elif endpoint in ('folder_result','folder_manifest','folder_file','folder_zip'):
        job_id = kwargs.pop('job_id')
        if not re.fullmatch('[a-f0-9]{32}', str(job_id)):
            raise ValueError('Неверный идентификатор папки')
        result = '/folders/' + job_id
        if endpoint == 'folder_manifest':
            result += '/manifest'
        elif endpoint == 'folder_file':
            result += '/files/' + quote(str(kwargs.pop('relative')), safe='/')
        elif endpoint == 'folder_zip':
            result += '/zip'
    else:
        raise ValueError('Неизвестный адрес: ' + endpoint)
    if kwargs:
        result += '?' + urlencode(kwargs)
    return result


def render(template, **context):
    context.setdefault('csrf', CSRF)
    context.setdefault('SCHEMAS', SCHEMAS)
    context.setdefault('get_flashed_messages', lambda with_categories=False: [])
    context['url_for'] = url_for
    return TEMPLATE_ENV.get_template(template).render(**context).encode('utf-8')


@contextmanager
def db_connection():
    conn = sqlite3.connect(APP_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_connection() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS folder_jobs (
            id TEXT PRIMARY KEY, source_name TEXT NOT NULL, status TEXT NOT NULL,
            file_count INTEGER NOT NULL, source_bytes INTEGER NOT NULL,
            output_files INTEGER DEFAULT 0, has_database INTEGER DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}', error_message TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, source_filename TEXT NOT NULL, source_path TEXT NOT NULL,
            target_path TEXT, selected_tables TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending', progress INTEGER NOT NULL DEFAULT 0,
            total_records INTEGER NOT NULL DEFAULT 0, migrated_records INTEGER NOT NULL DEFAULT 0,
            failed_records INTEGER NOT NULL DEFAULT 0, migrated_documents INTEGER NOT NULL DEFAULT 0,
            progress_phase TEXT NOT NULL DEFAULT 'Ожидание', duration REAL,
            integrity_ok INTEGER NOT NULL DEFAULT 0, details_json TEXT NOT NULL DEFAULT '{}',
            failure_message TEXT, created_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS migration_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES migration_projects(id),
            logged_at TEXT NOT NULL, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES migration_projects(id),
            table_name TEXT NOT NULL, legacy_id TEXT, message TEXT NOT NULL,
            detected_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Не перенесено'
        );
        ''')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(migration_errors)')}
        if 'detected_at' not in columns:
            conn.execute("ALTER TABLE migration_errors ADD COLUMN detected_at TEXT NOT NULL DEFAULT ''")
        if 'status' not in columns:
            conn.execute("ALTER TABLE migration_errors ADD COLUMN status TEXT NOT NULL DEFAULT 'Не перенесено'")
        conn.execute('''UPDATE migration_projects SET status='failed',
            failure_message='Перенос был прерван при закрытии программы',
            finished_at=? WHERE status='running' ''', (now_iso(),))


def project_or_none(id):
    with db_connection() as conn:
        p = conn.execute('SELECT * FROM migration_projects WHERE id=?', (id,)).fetchone()
    return dict(p) if p else None


def folder_or_none(job_id):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        return None
    with db_connection() as conn:
        p = conn.execute('SELECT * FROM folder_jobs WHERE id=?', (job_id,)).fetchone()
    return dict(p) if p else None


def document_comparison(old_path, new_path):
    """Один и тот же документ в старой и новой SQLite-базах для страницы результата."""
    try:
        with closing(sqlite3.connect(Path(new_path).resolve().as_uri() + '?mode=ro', uri=True)) as new_db:
            migrated = new_db.execute('''SELECT legacy_id, created_at, status
                FROM documents ORDER BY legacy_id LIMIT 1''').fetchone()
        if migrated is None:
            return None
        with closing(sqlite3.connect(Path(old_path).resolve().as_uri() + '?mode=ro', uri=True)) as old_db:
            original = old_db.execute('''SELECT doc_id, doc_date, status_code
                FROM old_documents WHERE doc_id=?''', (migrated[0],)).fetchone()
        if original is None:
            return None
        return {'legacy_id': migrated[0], 'old_date': original[1],
                'new_date': migrated[1], 'old_status': original[2],
                'new_status': migrated[2]}
    except (OSError, sqlite3.Error):
        return None


def accept_source(path, filename):
    try:
        tables = inspect_database(path)
        if not any(t['supported'] and not t['missing'] for t in tables):
            raise ValueError('Нет поддерживаемых таблиц old_documents, old_users или old_departments')
        with db_connection() as conn:
            cursor = conn.execute('''INSERT INTO migration_projects
                (name,source_filename,source_path,created_at) VALUES (?,?,?,?)''',
                (Path(filename).stem, filename, str(path), now_iso()))
            return cursor.lastrowid
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def update_progress(id, done, total, phase):
    with db_connection() as conn:
        conn.execute('''UPDATE migration_projects
            SET progress=?,total_records=?,progress_phase=? WHERE id=?''',
            (done,total,phase,id))


def worker(id, source, dest, selected):
    try:
        info = run_migration(id, source, dest, selected,
                             lambda n,total,phase: update_progress(id,n,total,phase))
        with db_connection() as conn:
            conn.executemany('''INSERT INTO migration_errors(project_id,table_name,legacy_id,message,detected_at)
                                VALUES(?,?,?,?,?)''', [(id,*r,now_iso()) for r in info['errors']])
            conn.executemany('''INSERT INTO migration_logs(project_id,logged_at,message)
                                VALUES(?,?,?)''', [(id,*r) for r in info['logs']])
            conn.execute('''UPDATE migration_projects SET status='done', progress=?,
               total_records=?, migrated_records=?, failed_records=?, migrated_documents=?,
               duration=?, integrity_ok=?, details_json=?, progress_phase='Завершено',
               finished_at=? WHERE id=?''',
                (info['total'],info['total'],info['success'],info['failed'],info['documents'],
                 info['duration'],int(info['integrity']),json.dumps(info['details'],ensure_ascii=False),
                 now_iso(),id))
    except Exception as exc:
        traceback.print_exc()
        with db_connection() as conn:
            conn.execute('''UPDATE migration_projects SET status='failed', failure_message=?,
               progress_phase='Ошибка',finished_at=? WHERE id=?''', (str(exc),now_iso(),id))
            conn.execute('INSERT INTO migration_logs(project_id,logged_at,message) VALUES(?,?,?)',
                         (id,now_iso(),'Сбой миграции: '+str(exc)))


class Uploaded:
    """Файл из multipart формы в формате, совместимом с migration.folders."""
    def __init__(self, name, content):
        self.filename = name
        self.stream = io.BytesIO(content)


class Form:
    def __init__(self):
        self.values = {}
        self.files = {}

    def add_value(self, name, value):
        self.values.setdefault(name, []).append(value)

    def add_file(self, name, file):
        self.files.setdefault(name, []).append(file)

    def get(self, name, default=''):
        return (self.values.get(name) or [default])[0]

    def getlist(self, name):
        return self.values.get(name, [])

    def file(self, name):
        return (self.files.get(name) or [None])[0]

    def files_list(self, name):
        return self.files.get(name, [])


class DocShiftHandler(BaseHTTPRequestHandler):
    server_version = 'DocShift/2.0'

    def log_message(self, fmt, *args):
        print('[HTTP]', self.address_string(), fmt % args, flush=True)

    def reply(self, content, status=200, content_type='text/html; charset=utf-8', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def html(self, template, status=200, notice='', kind='error', **ctx):
        messages = [(kind,notice)] if notice else []
        self.reply(render(template,get_flashed_messages=lambda with_categories=False: messages,
                          **ctx),status)

    def json(self, data, status=200):
        self.reply(json.dumps(data,ensure_ascii=False).encode('utf-8'), status,
                   'application/json; charset=utf-8')

    def redirect(self, destination):
        self.reply(b'',302,extra={'Location':destination})

    def error_page(self, message, status=400):
        self.html('message.html',status,title='Ошибка',message=message,page='')

    def send_disk_file(self, path, filename=None):
        path = Path(path)
        if not path.is_file():
            return self.error_page('Файл не найден', 404)
        from mimetypes import guess_type
        mime = guess_type(path.name)[0] or 'application/octet-stream'
        name = filename or path.name
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(name))
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        with path.open('rb') as f:
            shutil.copyfileobj(f,self.wfile,1024*1024)

    def parse_form(self):
        content_type = self.headers.get('Content-Type','')
        length = int(self.headers.get('Content-Length','0'))
        if length > MAX_UPLOAD:
            raise ValueError('Файлы слишком большие: максимум 150 МБ.')
        form = Form()
        if length <= 0:
            return form
        data = self.rfile.read(length)
        if content_type.startswith('application/x-www-form-urlencoded'):
            for k, values in parse_qs(data.decode('utf-8'),keep_blank_values=True).items():
                for val in values:
                    form.add_value(k,val)
        elif content_type.startswith('multipart/form-data'):
            # Парсер стандартной библиотеки Python; установка Flask/Werkzeug не нужна.
            blob = b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii') + b'\r\n\r\n' + data
            msg = BytesParser(policy=policy.default).parsebytes(blob)
            if not msg.is_multipart():
                raise ValueError('Не удалось разобрать данные формы.')
            parts = msg.iter_parts()
            for index, part in enumerate(parts):
                if index >= 6000:
                    raise ValueError('Слишком много элементов формы')
                name = part.get_param('name',header='Content-Disposition')
                if not name:
                    continue
                content = part.get_payload(decode=True) or b''
                filename = part.get_filename()
                if filename is not None:
                    form.add_file(name,Uploaded(filename,content))
                else:
                    form.add_value(name,content.decode('utf-8',errors='replace'))
        else:
            raise ValueError('Неподдерживаемый формат отправки')
        return form

    def do_GET(self):
        try:
            self.get_route()
        except (ValueError, sqlite3.Error, OSError) as exc:
            self.error_page(str(exc))
        except Exception:
            traceback.print_exc()
            self.error_page('Внутренняя ошибка. Посмотрите журнал в терминале.',500)

    def do_POST(self):
        try:
            form = self.parse_form()
            token = form.get('csrf') or self.headers.get('X-CSRF-Token')
            if token != CSRF:
                return self.error_page('Неверный токен. Перезагрузите страницу и повторите.',403)
            self.post_route(form)
        except (ValueError, sqlite3.Error, OSError) as exc:
            if urlsplit(self.path).path == '/folders/import':
                self.json({'error':str(exc)},400)
            else:
                self.error_page(str(exc))
        except Exception:
            traceback.print_exc()
            if urlsplit(self.path).path == '/folders/import':
                self.json({'error':'Ошибка обработки папки. Смотрите терминал VS Code.'},500)
            else:
                self.error_page('Внутренняя ошибка. Посмотрите терминал VS Code.',500)

    def get_route(self):
        parsed = urlsplit(self.path)
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        if path == '/health':
            return self.json({'service':'DocShift','status':'ok'})
        if path == '/':
            with db_connection() as conn:
                stats = dict(conn.execute('''SELECT COUNT(*) total,
                    COALESCE(SUM(status='done'),0) done,
                    COALESCE(SUM(status='done' AND failed_records>0),0) with_errors,
                    COALESCE(SUM(migrated_records),0) records
                    FROM migration_projects''').fetchone())
                folder_stats = conn.execute('''SELECT COUNT(*) total,
                    COALESCE(SUM(status='done'),0) done,
                    COALESCE(SUM(status='failed'),0) with_errors,
                    COALESCE(SUM(CASE WHEN status='done' THEN output_files ELSE 0 END),0) records
                    FROM folder_jobs''').fetchone()
                for key in stats:
                    stats[key] += folder_stats[key]
                recent = [dict(r) for r in conn.execute('SELECT * FROM migration_projects ORDER BY id DESC LIMIT 5')]
                recent_folders = [dict(r) for r in conn.execute(
                    'SELECT * FROM folder_jobs ORDER BY created_at DESC LIMIT 5')]
            return self.html('dashboard.html',page='dashboard',stats=stats,recent=recent,
                             recent_folders=recent_folders)
        if path == '/migrations/new':
            return self.html('migration_new.html',page='new')
        if path == '/folders/sample/download':
            return self.send_disk_file(BASE/'samples'/'Папка_для_теста.zip')
        if path == '/history':
            with db_connection() as conn:
                projects = [dict(r) for r in conn.execute('SELECT * FROM migration_projects ORDER BY id DESC')]
                folders = [dict(r) for r in conn.execute('SELECT * FROM folder_jobs ORDER BY created_at DESC LIMIT 100')]
            return self.html('history.html',page='history',projects=projects,folders=folders)
        if path == '/errors':
            selected = (query.get('migration') or [''])[0]
            if selected and not selected.isdecimal():
                return self.error_page('Неверный номер миграции')
            with db_connection() as conn:
                sql = '''SELECT e.*,p.source_filename,p.created_at
                    FROM migration_errors e JOIN migration_projects p ON p.id=e.project_id
                    ''' + ('WHERE e.project_id=? ' if selected else '') + 'ORDER BY e.id DESC LIMIT 500'
                errors = [dict(r) for r in conn.execute(sql, (int(selected),) if selected else ())]
                projects = [dict(r) for r in conn.execute('SELECT id,source_filename FROM migration_projects ORDER BY id DESC')]
            return self.html('errors.html',page='errors',project=None,errors=errors,
                             projects=projects,selected=selected)
        if path == '/about':
            return self.html('about.html',page='about')
        if path.startswith('/static/'):
            relative = path[len('/static/'):]
            safe = valid_path(relative)
            root = (BASE/'static').resolve()
            resolved = (root/safe).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                return self.error_page('Файл не найден',404)
            from mimetypes import guess_type
            mime = guess_type(resolved.name)[0] or 'application/octet-stream'
            return self.reply(resolved.read_bytes(), content_type=mime)
        m = re.fullmatch(r'/projects/(\d+)/(preview|mapping|process|result|download|errors|logs)', path)
        if m:
            id, action = int(m.group(1)), m.group(2)
            p = project_or_none(id)
            if p is None:
                return self.error_page('Миграция не найдена',404)
            if action == 'preview':
                if p['status'] != 'pending':
                    return self.redirect(url_for('result',id=id))
                tables = inspect_database(p['source_path'])
                active = (query.get('table') or [None])[0]
                if not any(t['name']==active for t in tables):
                    active = next((t['name'] for t in tables if t['supported']), tables[0]['name'] if tables else None)
                return self.html('migration_preview.html',page='new',project=p,tables=tables,active=active)
            if action == 'mapping':
                if p['status'] != 'pending':
                    return self.redirect(url_for('result',id=id))
                selected = json.loads(p['selected_tables'])
                if not selected:
                    return self.redirect(url_for('preview',id=id))
                tables = {t['name']:t for t in inspect_database(p['source_path'])}
                return self.html('mapping.html',page='new',project=p,
                                 selected=selected,tables=tables,total=sum(tables[n]['count'] for n in selected))
            if action == 'process':
                if not json.loads(p['selected_tables']):
                    return self.redirect(url_for('preview',id=id))
                if p['status'] in ('done','failed'):
                    return self.redirect(url_for('result',id=id))
                return self.html('migration_process.html',page='new',project=p)
            if action == 'result':
                if p['status'] in ('pending','running'):
                    return self.redirect(url_for('process',id=id))
                return self.html('migration_result.html',page='history',project=p,
                                 details=json.loads(p['details_json']))
            if action == 'download':
                if p['status']!='done':
                    return self.error_page('База ещё не готова',404)
                return self.send_disk_file(GENERATED/str(id)/'cloud.db','cloud.db')
            with db_connection() as conn:
                if action == 'errors':
                    entries = [dict(r) for r in conn.execute('''SELECT e.*,p.source_filename,p.created_at
                        FROM migration_errors e JOIN migration_projects p ON p.id=e.project_id
                        WHERE e.project_id=? ORDER BY e.id DESC LIMIT 500''',(id,))]
                    return self.html('errors.html',page='errors',project=p,errors=entries,projects=[],selected=str(id))
                entries = [dict(r) for r in conn.execute('''SELECT * FROM migration_logs
                    WHERE project_id=? ORDER BY id''',(id,))]
            return self.html('logs.html',page='history',project=p,logs=entries)
        m = re.fullmatch(r'/api/projects/(\d+)/status',path)
        if m:
            p=project_or_none(int(m.group(1)))
            if not p:
                return self.json({'error':'Миграция не найдена'},404)
            total=p['total_records']
            return self.json({'status':p['status'], 'progress':p['progress'], 'total':total,
                'percent':round(100*p['progress']/total) if total else (100 if p['status']=='done' else 0),
                'phase':p['progress_phase'],'error':p['failure_message'],
                'result_url':url_for('result',id=p['id'])})
        m = re.fullmatch(r'/folders/([a-f0-9]{32})(?:/(manifest|zip|files/(.+)))?',path)
        if m:
            job_id, action, relative=m.groups()
            job=folder_or_none(job_id)
            if not job:
                return self.error_page('Перенос папки не найден',404)
            details=json.loads(job['details_json'])
            if action is None:
                sample = None
                if job['status'] == 'done' and details.get('db_source') and details.get('db_target'):
                    root = FOLDER_RUNS/job_id
                    sample = document_comparison(root/'input'/valid_path(details['db_source']),
                                                 root/'output'/valid_path(details['db_target']))
                return self.html('folder_result.html',page='history',job=job,details=details,
                                 sample=sample)
            if job['status'] != 'done':
                return self.error_page('Результат переноса недоступен',404)
            if action == 'manifest':
                return self.json({'dirs':details['dirs'],'files':details['files'],'root_name':details['root_name']})
            if action == 'zip':
                return self.send_disk_file(FOLDER_RUNS/job_id/'result.zip','DocShift_результат.zip')
            if action.startswith('files/'):
                safe=valid_path(relative)
                root=(FOLDER_RUNS/job_id/'output').resolve()
                full=(root/safe).resolve()
                if not full.is_relative_to(root):
                    return self.error_page('Недопустимый путь',404)
                return self.send_disk_file(full)
        self.error_page('Страница не найдена',404)

    def post_route(self,form):
        path=urlsplit(self.path).path
        if path=='/migrations/demo':
            demo=BASE/'legacy.db'
            if not demo.exists():
                create_demo(demo)
            uploaded=UPLOADS/(uuid4().hex+'.db')
            shutil.copyfile(demo,uploaded)
            return self.redirect(url_for('preview',id=accept_source(uploaded,'legacy.db')))
        if path=='/migrations/upload':
            item=form.file('database')
            if not item or not item.filename:
                return self.error_page('Выберите SQLite-файл')
            filename=Path(item.filename.replace('\\','/')).name
            suffix=Path(filename).suffix.lower()
            if suffix not in VALID_SUFFIXES:
                return self.error_page('Поддерживаются .db, .sqlite, .sqlite3')
            contents=item.stream.getvalue()
            if len(contents)>SQLITE_LIMIT:
                return self.error_page('Максимальный размер SQLite: 20 МБ')
            dest=UPLOADS/(uuid4().hex+suffix)
            dest.write_bytes(contents)
            return self.redirect(url_for('preview',id=accept_source(dest,filename)))
        m=re.fullmatch(r'/projects/(\d+)/select',path)
        if m:
            id=int(m.group(1))
            p=project_or_none(id)
            if not p or p['status']!='pending':
                return self.error_page('Миграция недоступна',409)
            choices=set(form.getlist('tables'))
            tables={t['name']:t for t in inspect_database(p['source_path'])}
            allowed={n for n,t in tables.items() if t['supported'] and not t['missing']}
            if not choices or not choices<=allowed:
                return self.error_page('Выберите доступные таблицы',400)
            selected=[n for n in SCHEMAS if n in choices]
            with db_connection() as conn:
                conn.execute('UPDATE migration_projects SET selected_tables=? WHERE id=?',
                             (json.dumps(selected),id))
            return self.redirect(url_for('mapping',id=id))
        m=re.fullmatch(r'/api/projects/(\d+)/start',path)
        if m:
            id=int(m.group(1))
            p=project_or_none(id)
            if not p or not json.loads(p['selected_tables']):
                return self.json({'error':'Нет выбранных таблиц'},409)
            dest=GENERATED/str(id)/'cloud.db'
            with db_connection() as conn:
                changed=conn.execute('''UPDATE migration_projects
                   SET status='running',progress_phase='Подготовка',target_path=?
                   WHERE id=? AND status='pending' ''',(str(dest),id)).rowcount
            if changed:
                threading.Thread(target=worker,args=(id,p['source_path'],str(dest),
                    json.loads(p['selected_tables'])),daemon=True).start()
            return self.json({'ok':True,'status':'started' if changed else project_or_none(id)['status']})
        if path in ('/folders/import', '/folders/import-zip'):
            archive_mode = path.endswith('-zip')
            files=form.files_list('files')
            paths=form.getlist('paths')
            dirs=form.getlist('dirs')
            if not archive_mode and (len(files)!=len(paths) or len(files)>MAX_FILES):
                return self.json({'error':'Неправильный список файлов папки'},400)
            archive=form.file('archive') if archive_mode else None
            if archive_mode and (not archive or not archive.filename.lower().endswith('.zip')):
                return self.error_page('Выберите ZIP-архив с папкой')
            job_id=uuid4().hex
            root=FOLDER_RUNS/job_id
            stats=None
            try:
                stats=(write_uploaded_zip(archive,root/'input') if archive_mode else
                       write_uploaded_tree(list(zip(paths,files)),dirs,root/'input'))
                info=convert_tree(root/'input',root/'output',root/'result.zip')
                with db_connection() as conn:
                    conn.execute('''INSERT INTO folder_jobs
                     (id,source_name,status,file_count,source_bytes,output_files,
                      has_database,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                      (job_id,stats['root_name'],'done',stats['file_count'],stats['size_bytes'],
                       info['files_count'],int(info['migration'] is not None),
                       json.dumps(info,ensure_ascii=False),now_iso()))
            except Exception:
                with db_connection() as conn:
                    conn.execute('''INSERT INTO folder_jobs
                     (id,source_name,status,file_count,source_bytes,error_message,created_at)
                     VALUES(?,?,?,?,?,?,?)''',
                     (job_id,stats['root_name'] if stats else (archive.filename if archive_mode else 'Папка'),
                      'failed',stats['file_count'] if stats else 0,stats['size_bytes'] if stats else 0,
                      str(sys.exc_info()[1]),now_iso()))
                shutil.rmtree(root,ignore_errors=True)
                raise
            result_url=url_for('folder_result',job_id=job_id)
            return self.redirect(result_url) if archive_mode else self.json({'ok':True,'result_url':result_url})
        self.error_page('Страница не найдена',404)


def main():
    init_db()
    preferred=int(os.environ.get('DOCSHIFT_PORT','5000'))
    if not 1<=preferred<=65535:
        raise ValueError('Некорректный порт сервера')
    server=None
    for port in [preferred]+list(range(5001,5026)):
        try:
            server=ThreadingHTTPServer(('127.0.0.1',port),DocShiftHandler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError('Не найден свободный локальный порт. Закройте другие серверы.')
    url=f'http://127.0.0.1:{server.server_port}/'
    print('\n'+'='*54,flush=True)
    print(' DocShift запущен БЕЗ Flask, pip и прав администратора',flush=True)
    print(' Адрес сайта: '+url,flush=True)
    print(' Для остановки нажмите Ctrl+C',flush=True)
    print('='*54+'\n',flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        print('\nDocShift остановлен',flush=True)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
