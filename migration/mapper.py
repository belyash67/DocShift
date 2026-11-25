"""Соответствия полей старой и новой систем."""
from datetime import datetime, date, timezone

# Ключ словаря — имя исходной таблицы; порядок описывает удобный порядок на экране.
SCHEMAS = {
    'old_departments': {
        'target': 'departments',
        'fields': [('department_id', 'legacy_id'), ('department_name', 'name')],
    },
    'old_users': {
        'target': 'users',
        'fields': [('user_id', 'legacy_id'), ('full_name', 'full_name'), ('email', 'email')],
    },
    'old_documents': {
        'target': 'documents',
        'fields': [('doc_id', 'legacy_id'), ('doc_name', 'title'),
                   ('user_id', 'author_id'), ('department_id', 'department_id'),
                   ('doc_date', 'created_at'), ('status_code', 'status'),
                   ('file_path', 'file_path')],
    },
}

STATUS_CODES = {0: 'draft', 1: 'active', 2: 'archived'}


def normalize_date(value):
    """Пример: 12.05.2019 -> 2019-05-12. Некорректная дата -> ValueError."""
    if value is None or not str(value).strip():
        raise ValueError('Дата документа не заполнена')
    for pattern in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value).strip(), pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f'Недопустимая дата: {value}')


def normalize_status(value):
    try:
        return STATUS_CODES[int(value)] if str(value).strip() == str(int(value)) else STATUS_CODES[value]
    except (TypeError, ValueError, KeyError):
        raise ValueError(f'Неизвестный код статуса: {value}') from None


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
