"""Тестируем перенос дерева без Flask и без доступа к компьютеру пользователя."""
import hashlib
import io
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from create_test_database import create_demo
from migration.folders import (check_tree, write_uploaded_tree,
                               convert_tree, create_test_folder, valid_path)


class Uploaded:
    def __init__(self, content):
        self.stream = io.BytesIO(content)


class FolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_rejects_unsafe_paths(self):
        for raw in ['/tmp/file.txt', '../etc', 'source/../escape',
                    'source\\evil.txt', 'C:/x', 'source//file', 'source/file.']:
            with self.subTest(path=raw), self.assertRaises(ValueError):
                valid_path(raw)
        with self.assertRaises(ValueError):
            check_tree(['A/readme.txt', 'B/file.txt'], [])
        with self.assertRaises(ValueError):
            check_tree(['A/X.txt', 'A/x.txt'], [])

    def test_file_copy_without_legacy_db_and_empty_directories(self):
        source = self.base / 'input'
        uploaded = [('МояПапка/archive/2020/paper.txt', Uploaded('привет'.encode())),
                    ('МояПапка/misc/bin.dat', Uploaded(b'\x00\x11\xfe'))]
        stats = write_uploaded_tree(uploaded, ['МояПапка/empty', 'МояПапка/archive/2020'], source)
        self.assertEqual(stats['root_name'], 'МояПапка')
        original = (source / 'МояПапка/misc/bin.dat').read_bytes()
        target = self.base / 'output'
        archive = self.base / 'result.zip'
        data = convert_tree(source, target, archive)
        self.assertIsNone(data['migration'])
        self.assertIn('МояПапка/empty', data['dirs'])
        self.assertEqual((target / 'МояПапка/misc/bin.dat').read_bytes(), original)
        with ZipFile(archive) as z:
            self.assertIn('МояПапка/empty/', z.namelist())
            self.assertEqual(z.read('МояПапка/archive/2020/paper.txt'), 'привет'.encode())
        self.assertEqual((source / 'МояПапка/misc/bin.dat').read_bytes(), original)

    def test_sqlite_and_physical_documents_are_migrated_together(self):
        source = self.base / 'input'
        demo = create_test_folder(source / 'Пример_папки', create_demo)
        target = self.base / 'output'
        archive = self.base / 'result.zip'
        original_db = hashlib.sha256((demo / 'legacy.db').read_bytes()).hexdigest()
        src_doc = next(demo.glob('archive/**/*.txt'))
        doc_data = src_doc.read_bytes()
        info = convert_tree(source, target, archive)
        self.assertEqual(info['root_name'], 'Пример_папки')
        self.assertEqual((info['migration']['total'], info['migration']['success'], info['migration']['failed']), (548,535,13))
        self.assertEqual(info['db_target'], 'Пример_папки/cloud.db')
        self.assertEqual(info['files_count'], 502) # 500 документов + cloud.db + readme
        self.assertFalse((target / 'Пример_папки/legacy.db').exists())
        self.assertTrue((target / 'Пример_папки/cloud.db').exists())
        self.assertEqual((target / src_doc.relative_to(source)).read_bytes(), doc_data)
        self.assertEqual(hashlib.sha256((demo / 'legacy.db').read_bytes()).hexdigest(), original_db)
        self.assertTrue((target / 'Пример_папки/пустой_каталог').is_dir())
        with closing(sqlite3.connect(target / 'Пример_папки/cloud.db')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 487)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            file_path = db.execute('SELECT file_path FROM documents WHERE legacy_id=1').fetchone()[0]
            self.assertTrue((target / 'Пример_папки' / file_path).is_file())
        with ZipFile(archive) as z:
            self.assertIn('Пример_папки/пустой_каталог/', z.namelist())
            self.assertIn('Пример_папки/cloud.db', z.namelist())
            self.assertNotIn('Пример_папки/legacy.db', z.namelist())

    def test_bad_sqlite_is_not_hidden(self):
        source = self.base / 'input' / 'Legacy'
        source.mkdir(parents=True)
        (source / 'legacy.db').write_text('broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'SQLite'):
            convert_tree(source.parent, self.base / 'out', self.base / 'out.zip')
        self.assertFalse((self.base / 'out').exists())

    def test_existing_cloud_db_is_not_overwritten(self):
        source = self.base / 'input' / 'Legacy'
        source.mkdir(parents=True)
        create_demo(source / 'legacy.db')
        (source / 'cloud.db').write_bytes(b'important')
        with self.assertRaisesRegex(ValueError, 'cloud.db'):
            convert_tree(source.parent, self.base / 'out', self.base / 'out.zip')
        self.assertEqual((source / 'cloud.db').read_bytes(), b'important')


if __name__ == '__main__':
    unittest.main()
