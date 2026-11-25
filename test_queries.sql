-- Выполняйте запросы в старой базе legacy.db:
SELECT COUNT(*) AS documents FROM old_documents;
SELECT * FROM old_departments;
SELECT user_id, full_name, email FROM old_users LIMIT 10;
SELECT doc_id, doc_name, doc_date, status_code FROM old_documents LIMIT 20;
SELECT * FROM old_documents WHERE doc_id IN (10,45,90,125,160,205,245,280,315,350,390,435,480);
-- После миграции выполняйте в cloud.db:
-- SELECT COUNT(*) FROM documents; -- 487
-- SELECT legacy_id, title, created_at, status FROM documents LIMIT 10;
-- PRAGMA foreign_key_check;
