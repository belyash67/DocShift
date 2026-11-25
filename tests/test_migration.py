"""Проверки учебного ETL и структуры результата (стандартный unittest)."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from create_test_database import create_demo
from migration.mapper import normalize_date, normalize_status
from migration.analyzer import inspect_database
from migration.migrator import run_migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = create_demo(self.root / 'legacy.db')
        self.dst = self.root / 'cloud.db'

    def test_transformations(self):
        self.assertEqual(normalize_date('12.05.2019'), '2019-05-12')
        self.assertEqual(normalize_date('2019-05-12'), '2019-05-12')
        self.assertEqual(normalize_status(0), 'draft')
        self.assertEqual(normalize_status(1), 'active')
        self.assertEqual(normalize_status(2), 'archived')
        with self.assertRaises(ValueError):
            normalize_date('31.02.2020')
        with self.assertRaises(ValueError):
            normalize_status(99)

    def test_analyzer_counts(self):
        tables = {t['name']: t for t in inspect_database(self.src)}
        self.assertEqual(tables['old_documents']['count'], 500)
        self.assertEqual(tables['old_users']['count'], 40)
        self.assertEqual(tables['old_departments']['count'], 8)
        self.assertFalse(tables['old_documents']['missing'])
        self.assertEqual(len(tables['old_documents']['sample']), 5)

    def test_full_migration_with_intentional_errors(self):
        progress = []
        result = run_migration(1, self.src, self.dst, list(('old_departments', 'old_users', 'old_documents')),
                               lambda done, total, phase: progress.append((done,total,phase)))
        self.assertEqual((result['total'], result['success'], result['failed'], result['documents']),
                         (548,535,13,487))
        self.assertTrue(result['integrity'])
        self.assertTrue(self.dst.exists())
        self.assertEqual(progress[-1][0], 548)
        with closing(sqlite3.connect(self.dst)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 487)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 40)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM departments').fetchone()[0], 8)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            doc = db.execute('SELECT id,legacy_id,title,created_at,status,author_id,department_id FROM documents WHERE legacy_id=1').fetchone()
            self.assertEqual(doc[1], 1)
            self.assertRegex(doc[3], r'^\d{4}-\d{2}-\d{2}$')
            self.assertIn(doc[4], ['draft','active','archived'])
            self.assertNotEqual(doc[5], 0)
            self.assertNotEqual(doc[6], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents WHERE legacy_id=10').fetchone()[0], 0)
        self.assertTrue(any('Пустое название' in reason for _, _, reason in result['errors']))
        self.assertTrue(any('не найден' in reason for _, _, reason in result['errors']))

    def test_duplicate_legacy_ids_are_logged(self):
        with closing(sqlite3.connect(self.src)) as db, db:
            db.executescript("""
                CREATE TABLE duplicated_documents AS SELECT * FROM old_documents;
                DROP TABLE old_documents;
                ALTER TABLE duplicated_documents RENAME TO old_documents;
                INSERT INTO old_documents SELECT * FROM old_documents WHERE doc_id = 1;
            """)
        info = run_migration(1, self.src, self.dst,
                             ['old_departments','old_users','old_documents'], lambda *args: None)
        self.assertEqual(info['total'], 549)
        self.assertEqual(info['success'], 535)
        self.assertEqual(info['failed'], 14)
        self.assertTrue(any('Повторяющийся ID: 1' in reason for _, _, reason in info['errors']))

    def test_input_database_is_not_modified(self):
        before = self.src.read_bytes()
        run_migration(2, self.src, self.dst, list(('old_departments', 'old_users', 'old_documents')),
                      lambda *args: None)
        self.assertEqual(self.src.read_bytes(), before)

    def test_select_only_departments(self):
        info = run_migration(1, self.src, self.dst, ['old_departments'], lambda *args: None)
        self.assertEqual((info['total'],info['success'],info['failed']), (8,8,0))

    def test_bad_database_not_sqlite(self):
        not_db = self.root / 'fake.db'
        not_db.write_text('not a database')
        with self.assertRaises(ValueError):
            inspect_database(not_db)


if __name__ == '__main__':
    unittest.main()
