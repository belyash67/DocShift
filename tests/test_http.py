"""Основные пользовательские маршруты через настоящий локальный HTTP-сервер."""
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

import local_server as app
from create_test_database import create_demo


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.original = {name: getattr(app, name) for name in ('DATA', 'UPLOADS', 'GENERATED', 'FOLDER_RUNS', 'APP_DB')}
        app.DATA = cls.root / 'database'
        app.UPLOADS = cls.root / 'uploads'
        app.GENERATED = app.DATA / 'generated'
        app.FOLDER_RUNS = app.DATA / 'folder_runs'
        app.APP_DB = app.DATA / 'app.db'
        for directory in (app.DATA, app.UPLOADS, app.GENERATED, app.FOLDER_RUNS):
            directory.mkdir(parents=True, exist_ok=True)
        app.init_db()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.DocShiftHandler)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        for name, value in cls.original.items():
            setattr(app, name, value)
        cls.temp.cleanup()

    def request(self, path, data=None, content_type=None):
        headers = {'X-CSRF-Token': app.CSRF} if data is not None else {}
        if content_type:
            headers['Content-Type'] = content_type
        with urlopen(Request(self.url + path, data=data, headers=headers), timeout=15) as response:
            return response.read(), response.geturl()

    def upload(self, path, field, filename, content):
        boundary = 'DocShiftHttpTestBoundary'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{app.CSRF}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        return self.request(path, body, f'multipart/form-data; boundary={boundary}')

    def test_sqlite_flow_and_persistent_history(self):
        source = create_demo(self.root / 'test_legacy.db')
        health, _ = self.request('/health')
        self.assertEqual(json.loads(health)['status'], 'ok')
        page, url = self.upload('/migrations/upload', 'database', 'legacy.db', source.read_bytes())
        self.assertTrue(url.endswith('/projects/1/preview'))
        self.assertIn('old_documents'.encode(), page)
        selected = urlencode({'csrf': app.CSRF, 'tables': ['old_departments', 'old_users', 'old_documents']}, doseq=True).encode()
        mapping, url = self.request('/projects/1/select', selected, 'application/x-www-form-urlencoded')
        self.assertTrue(url.endswith('/projects/1/mapping'))
        self.assertIn(b'legacy_id', mapping)
        self.request('/api/projects/1/start', b'', 'application/x-www-form-urlencoded')
        for _ in range(100):
            state = json.loads(self.request('/api/projects/1/status')[0])
            if state['status'] in ('done', 'failed'):
                break
            time.sleep(.05)
        self.assertEqual(state['status'], 'done', state)
        db_bytes, _ = self.request('/projects/1/download')
        self.assertEqual(db_bytes[:16], b'SQLite format 3\x00')
        result = app.GENERATED / '1' / 'cloud.db'
        with closing(sqlite3.connect(result)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 487)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        errors, _ = self.request('/errors?migration=1')
        self.assertIn('Не перенесено'.encode(), errors)
        self.assertIn('Время'.encode(), errors)
        app.init_db()  # имитируем повторный запуск приложения
        history, _ = self.request('/history')
        self.assertIn('legacy.db'.encode(), history)

    def test_zip_folder_and_unsafe_archive(self):
        sample = Path(app.__file__).resolve().parent / 'samples' / 'Папка_для_теста.zip'
        page, url = self.upload('/folders/import-zip', 'archive', 'source.zip', sample.read_bytes())
        self.assertIn('/folders/', url)
        self.assertIn('Структура и файлы проверены'.encode(), page)
        dashboard, _ = self.request('/')
        self.assertIn('Последние переносы папок'.encode(), dashboard)
        self.assertIn('Пример_папки'.encode(), dashboard)
        result_zip, _ = self.request(url.removeprefix(self.url) + '/zip')
        with ZipFile(io.BytesIO(result_zip)) as archive:
            self.assertIn('Пример_папки/пустой_каталог/', archive.namelist())
            self.assertIn('Пример_папки/cloud.db', archive.namelist())
            self.assertNotIn('Пример_папки/legacy.db', archive.namelist())
        bad = io.BytesIO()
        with ZipFile(bad, 'w') as archive:
            archive.writestr('../escape.txt', 'unsafe')
        with self.assertRaises(HTTPError) as response:
            self.upload('/folders/import-zip', 'archive', 'bad.zip', bad.getvalue())
        response.exception.close()


if __name__ == '__main__':
    unittest.main()
