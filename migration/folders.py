"""Папка → папка: сохранение дерева файлов и миграция legacy.db → cloud.db.

Все пути приходят из браузера как относительные пути выбранной папки.
Никогда не используем их напрямую без проверки: браузер — недоверенный источник.
"""
import shutil
import time
import hashlib
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from .analyzer import inspect_database
from .mapper import SCHEMAS
from .migrator import run_migration

MAX_FILES = 2500
MAX_TOTAL_BYTES = 150 * 1024 * 1024
MAX_COMPONENTS = 24


def valid_path(raw):
    """Путь папки вида ИмяПапки/архив/2019/файл.txt, без обхода директорий."""
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw:
        raise ValueError('Недопустимый путь файла или папки')
    parts = raw.split('/')
    if (len(parts) > MAX_COMPONENTS or any(not p or p in ('.', '..') or
            ':' in p or len(p) > 180 or p.endswith(('.', ' ')) for p in parts)):
        raise ValueError(f'Небезопасный путь: {raw[:120]}')
    return Path(*parts)


def check_tree(paths, dirs):
    """Проверяет дерево до записи; возвращает имя общей корневой папки."""
    if not paths:
        raise ValueError('В выбранной папке нет файлов. Добавьте хотя бы один файл.')
    if len(paths) > MAX_FILES:
        raise ValueError('Слишком много файлов (максимум 2500).')
    file_paths = [valid_path(p) for p in paths]
    dir_paths = [valid_path(p) for p in dirs]
    # Входные пути содержат имя выбранной папки.
    roots = {p.parts[0] for p in file_paths + dir_paths}
    if len(roots) != 1:
        raise ValueError('Выберите одну исходную папку, а не несколько.')
    if any(len(p.parts) < 2 for p in file_paths):
        raise ValueError('У каждого файла должен быть путь внутри выбранной папки.')
    lowered_files = [p.as_posix().casefold() for p in file_paths]
    if len(lowered_files) != len(set(lowered_files)):
        raise ValueError('В папке обнаружены одинаковые имена файлов.')
    for p in dir_paths:
        if p.as_posix().casefold() in set(lowered_files):
            raise ValueError(f'Одинаковое имя у файла и папки: {p}')
    return next(iter(roots))


def write_uploaded_tree(uploaded, dirs, destination):
    """Принимает [(путь, FileStorage)], записывает безопасную копию дерева."""
    if len(uploaded) > MAX_FILES:
        raise ValueError('Слишком много файлов')
    paths = [name for name, _ in uploaded]
    root_name = check_tree(paths, dirs)
    dest = Path(destination)
    if dest.exists():
        raise ValueError('Папка временной загрузки уже существует')
    dest.mkdir(parents=True)
    size = 0
    try:
        for raw in dirs:
            (dest / valid_path(raw)).mkdir(parents=True, exist_ok=True)
        for raw, item in uploaded:
            path = dest / valid_path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Читаем порциями: нельзя доверять Content-Length отдельных частей.
            with path.open('wb') as out:
                while True:
                    chunk = item.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_TOTAL_BYTES:
                        raise ValueError('Размер папки превышает 150 МБ.')
                    out.write(chunk)
        return {'root_name': root_name, 'file_count': len(uploaded), 'size_bytes': size}
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def write_uploaded_zip(item, destination):
    """Распаковать один архив с общей корневой папкой, сохранив пустые каталоги."""
    dest = Path(destination)
    if dest.exists():
        raise ValueError('Папка временной загрузки уже существует')
    try:
        with ZipFile(item.stream) as archive:
            entries = archive.infolist()
            files = [entry for entry in entries if not entry.is_dir()]
            dirs = [entry.filename.rstrip('/') for entry in entries if entry.is_dir()]
            paths = [entry.filename for entry in files]
            root_name = check_tree(paths, dirs)
            if any(stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK for entry in entries):
                raise ValueError('Архив содержит символическую ссылку')
            if sum(entry.file_size for entry in files) > MAX_TOTAL_BYTES:
                raise ValueError('Размер файлов в архиве превышает 150 МБ')
            dest.mkdir(parents=True)
            for raw in dirs:
                (dest / valid_path(raw)).mkdir(parents=True, exist_ok=True)
            size = 0
            for entry in files:
                path = dest / valid_path(entry.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, path.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_TOTAL_BYTES:
                            raise ValueError('Размер файлов в архиве превышает 150 МБ')
                        output.write(chunk)
            return {'root_name': root_name, 'file_count': len(files), 'size_bytes': size}
    except (BadZipFile, RuntimeError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError('Некорректный ZIP-архив') from exc
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def verify_tree(source, target, old_db=None, new_db=None):
    """Проверить дерево, содержимое файлов и ссылки документов до выдачи результата."""
    source_dirs = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_dir()}
    target_dirs = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_dir()}
    if source_dirs != target_dirs:
        raise RuntimeError('Проверка каталогов не пройдена')
    source_files = {p.relative_to(source).as_posix(): p for p in source.rglob('*') if p.is_file()}
    target_files = {p.relative_to(target).as_posix(): p for p in target.rglob('*') if p.is_file()}
    expected = set(source_files)
    if old_db is not None:
        expected.remove(old_db.as_posix())
        expected.add(new_db.as_posix())
    if expected != set(target_files):
        raise RuntimeError('Список файлов результата не совпадает с источником')
    for relative, original in source_files.items():
        if old_db is not None and relative == old_db.as_posix():
            continue
        copied = target_files[relative]
        if original.stat().st_size != copied.stat().st_size or file_digest(original) != file_digest(copied):
            raise RuntimeError(f'Файл изменился при переносе: {relative}')
    if new_db is not None:
        db_path = target / new_db
        with closing(sqlite3.connect(db_path)) as db:
            root = db_path.parent.resolve()
            for (raw,) in db.execute('SELECT file_path FROM documents WHERE file_path IS NOT NULL AND trim(file_path) != ""'):
                relative = valid_path(raw)
                linked = (root / relative).resolve()
                if not linked.is_relative_to(root) or not linked.is_file():
                    raise RuntimeError(f'Файл документа не найден: {raw}')
    return True


def convert_tree(source, target, archive_path):
    """Копирует исходное дерево, заменяя legacy.db на cloud.db, если база найдена.

    Содержимое остальных файлов сохраняется без изменений. Пустые папки сохраняются.
    Возвращает статистику и перечень выходных файлов/каталогов.
    """
    start = time.monotonic()
    source, target, archive_path = map(Path, (source, target, archive_path))
    if not source.is_dir() or target.exists() or archive_path.exists():
        raise ValueError('Неверная исходная или целевая папка')
    databases = [p for p in source.rglob('*') if p.is_file() and p.name.lower() == 'legacy.db']
    if len(databases) > 1:
        raise ValueError('Найдено несколько legacy.db. Для одного запуска нужна одна исходная база.')
    if databases:
        # Проверим, что schema подходит, ПЕРЕД копированием.
        tables = inspect_database(databases[0])
        if not all(any(t['name'] == n and t['supported'] and not t['missing'] for t in tables) for n in SCHEMAS):
            raise ValueError('В legacy.db должны быть old_departments, old_users и old_documents с нужными полями')
        converted_relative = databases[0].relative_to(source)
        cloud_relative = converted_relative.with_name('cloud.db')
        if (source / cloud_relative).exists():
            raise ValueError('В папке уже есть cloud.db, перезапись запрещена')
    else:
        converted_relative, cloud_relative = None, None
    shutil.copytree(source, target)
    info = None
    try:
        if databases:
            migrated = target / cloud_relative
            migrated.parent.mkdir(parents=True, exist_ok=True)
            info = run_migration(0, source / converted_relative, migrated,
                                 list(SCHEMAS), lambda *args: None)
            # Только после проверки целостности удаляем копию старой БД из результата.
            (target / converted_relative).unlink()
        verify_tree(source, target, converted_relative, cloud_relative)
        files = sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file())
        dirs = sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_dir())
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(archive_path, 'w', compression=ZIP_DEFLATED) as archive:
            for directory in dirs:
                archive.writestr(directory + '/', b'')
            for filename in files:
                archive.write(target / filename, filename)
        return {'root_name': next(p.name for p in source.iterdir() if p.is_dir()),
                'files': files, 'dirs': dirs,
                'files_count': len(files), 'dirs_count': len(dirs),
                'db_source': converted_relative.as_posix() if converted_relative else None,
                'db_target': cloud_relative.as_posix() if cloud_relative else None,
                'migration': info, 'integrity': True,
                'duration': round(time.monotonic() - start, 2)}
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        raise


def create_test_folder(destination, create_demo):
    """Демо-папка с файлами, соответствующими file_path в SQLite.

    Создаёт текстовые документы вместо фиктивных PDF, а пути в ТЕСТОВОЙ БД
    меняет на .txt, чтобы каждую запись можно было открыть после миграции.
    """
    import sqlite3
    from pathlib import Path
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Пример уже существует')
    destination.mkdir(parents=True)
    db = create_demo(destination / 'legacy.db')
    with closing(sqlite3.connect(db)) as conn, conn:
        documents = conn.execute('SELECT doc_id, doc_name, doc_date, status_code, file_path FROM old_documents').fetchall()
        for doc_id, name, date, status, file_path in documents:
            path = Path(file_path).with_suffix('.txt')
            full = destination / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f'Учебный документ № {doc_id}\nНазвание: {name or "(не задано)"}\nДата: {date}\nСтатус: {status}\n', encoding='utf-8')
            conn.execute('UPDATE old_documents SET file_path=? WHERE doc_id=?', (path.as_posix(), doc_id))
    (destination / 'экспорт').mkdir()
    (destination / 'пустой_каталог').mkdir()
    (destination / 'ПРОЧТИ_МЕНЯ.txt').write_text('Демонстрационные данные для DocShift.\nВсе имена вымышлены.\n', encoding='utf-8')
    return destination
