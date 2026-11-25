"""Создаёт реалистичную учебную БД legacy.db для проверки миграции DocShift.

40 вымышленных сотрудников, 8 отделов, 500 документов, из которых 13
намеренно содержат ошибки. Сторонние зависимости не требуются.
"""
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent

DEPARTMENTS = [
    'Канцелярия', 'Отдел кадров', 'Бухгалтерия', 'Юридический отдел',
    'Отдел информационных технологий', 'Административный отдел', 'Архив', 'Отдел проектов',
]

# Все имена вымышлены, адреса используют зарезервированный домен example.org.
EMPLOYEES = [
    'Александр Соколов', 'Мария Волкова', 'Дмитрий Орлов', 'Елена Морозова', 'Иван Павлов',
    'Анна Кузнецова', 'Павел Фёдоров', 'Ольга Никитина', 'Сергей Беляев', 'Татьяна Миронова',
    'Артём Зайцев', 'Наталья Ершова', 'Максим Фомин', 'Екатерина Алексеева', 'Андрей Крылов',
    'Виктория Лазарева', 'Кирилл Борисов', 'Светлана Комарова', 'Алексей Жуков', 'Ирина Сафонова',
    'Роман Антонов', 'Юлия Осипова', 'Михаил Киселёв', 'Полина Макарова', 'Денис Громов',
    'Дарья Лебедева', 'Константин Назаров', 'Вероника Романова', 'Николай Ефимов', 'Алина Котова',
    'Владислав Соловьёв', 'Анастасия Калинина', 'Пётр Власов', 'Валерия Гусева', 'Олег Баранов',
    'Ксения Тихонова', 'Георгий Фролов', 'Людмила Сидорова', 'Егор Белкин', 'Софья Захарова',
]

# Документы похожи на реальный документооборот, но не относятся к реальной организации.
DOCUMENT_TYPES = [
    ('Приказ', 'о назначении ответственного сотрудника', 'orders'),
    ('Служебная записка', 'о согласовании внутренних работ', 'memos'),
    ('Акт', 'приёма-передачи имущества', 'acts'),
    ('Отчёт', 'о результатах работы отдела', 'reports'),
    ('Заявка', 'на приобретение оборудования', 'requests'),
    ('Протокол', 'рабочего совещания', 'minutes'),
    ('Договор', 'на оказание услуг', 'contracts'),
    ('Письмо', 'о предоставлении информации', 'letters'),
    ('Инструкция', 'по ведению документации', 'instructions'),
    ('Уведомление', 'об изменении графика работы', 'notices'),
    ('Акт', 'проверки архивных материалов', 'archive'),
    ('Заявление', 'на предоставление отпуска', 'applications'),
]

# (ID документа: (индекс поля в old_documents, испорченное значение)).
# 3 пустых имени, 3 даты, 2 неизвестных пользователя, 2 отдела, 3 статуса.
BAD_DOCUMENTS = {
    10: (1, ''),
    45: (4, '31.02.2020'),
    90: (2, 999),
    125: (3, 777),
    160: (5, 99),
    205: (1, None),
    245: (4, 'не-дата'),
    280: (2, 888),
    315: (3, 999),
    350: (5, -5),
    390: (1, '  '),
    435: (4, '30.02.2022'),
    480: (5, 99),
}


def create_demo(path=None):
    """Пересоздать SQLite-базу. Использовать только с тестовым файлом!"""
    path = Path(path) if path is not None else BASE / 'legacy.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()  # только тестовая база, не пользовательские данные

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''
            CREATE TABLE old_departments (
                department_id INTEGER PRIMARY KEY,
                department_name TEXT
            );
            CREATE TABLE old_users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                email TEXT,
                department_id INTEGER
            );
            CREATE TABLE old_documents (
                doc_id INTEGER PRIMARY KEY,
                doc_name TEXT,
                user_id INTEGER,
                department_id INTEGER,
                doc_date TEXT,
                status_code INTEGER,
                file_path TEXT
            );
        ''')
        conn.executemany('INSERT INTO old_departments VALUES (?, ?)',
                         [(index, name) for index, name in enumerate(DEPARTMENTS, 1)])
        conn.executemany('INSERT INTO old_users VALUES (?, ?, ?, ?)', [
            (index, name, f'employee{index:02d}@example.org', (index - 1) // 5 + 1)
            for index, name in enumerate(EMPLOYEES, 1)
        ])

        documents = []
        for document_id in range(1, 501):
            user_id = ((document_id - 1) % len(EMPLOYEES)) + 1
            department_id = (user_id - 1) // 5 + 1
            doc_type, subject, directory = DOCUMENT_TYPES[(document_id - 1) % len(DOCUMENT_TYPES)]
            doc_date = date(2018, 1, 1) + timedelta(days=(document_id * 37) % 2900)
            documents.append([
                document_id,
                f'{doc_type} {subject} № {document_id:04d}',
                user_id,
                department_id,
                doc_date.strftime('%d.%m.%Y'),
                (document_id - 1) % 3,  # 0=draft, 1=active, 2=archived
                f'archive/{doc_date.year}/{directory}/{document_id:04d}.pdf',
            ])

        for document_id, (field_index, invalid_value) in BAD_DOCUMENTS.items():
            documents[document_id - 1][field_index] = invalid_value

        conn.executemany('INSERT INTO old_documents VALUES (?, ?, ?, ?, ?, ?, ?)', documents)
        conn.commit()
    return path


if __name__ == '__main__':
    print('Создана тестовая SQLite-база:', create_demo())
    print('Отделов: 8; сотрудников: 40; документов: 500 (в том числе 13 ошибок)')
