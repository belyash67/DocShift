"""Создаёт пример папки или обновляет готовый ZIP для DocShift.

Запуск: python create_test_folder.py [--zip]
"""
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from create_test_database import create_demo
from migration.folders import create_test_folder

if __name__ == '__main__':
    base = Path(__file__).resolve().parent
    if sys.argv[1:] == ['--zip']:
        with tempfile.TemporaryDirectory() as temporary:
            result = create_test_folder(Path(temporary) / 'Пример_папки', create_demo)
            destination = base / 'samples' / 'Папка_для_теста.zip'
            destination.parent.mkdir(parents=True, exist_ok=True)
            with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
                for path in sorted(result.rglob('*')):
                    relative = path.relative_to(result.parent).as_posix()
                    archive.write(path, relative + ('/' if path.is_dir() else ''))
        print('Готов ZIP:', destination)
    elif not sys.argv[1:]:
        result = create_test_folder(base / 'Пример_папки', create_demo)
        print('Готова папка:', result)
    else:
        raise SystemExit('Использование: python create_test_folder.py [--zip]')
    print('500 текстовых документов, 1 SQLite legacy.db, 1 README, пустые каталоги')
