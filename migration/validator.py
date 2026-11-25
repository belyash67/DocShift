"""Проверки значений перед переносом."""
from .mapper import normalize_date, normalize_status


def mandatory_int(value, field):
    try:
        v = int(value)
        if v < 1 or str(value).strip() != str(v):
            raise ValueError
        return v
    except (TypeError, ValueError):
        raise ValueError(f'Некорректный идентификатор «{field}»: {value}') from None


def validate_department(row):
    key = mandatory_int(row['department_id'], 'department_id')
    name = (row['department_name'] or '').strip()
    if not name:
        raise ValueError('Отсутствует название отдела')
    return key, name


def validate_user(row):
    key = mandatory_int(row['user_id'], 'user_id')
    name = (row['full_name'] or '').strip()
    if not name:
        raise ValueError('Отсутствует ФИО пользователя')
    email = (row['email'] or '').strip()
    return key, name, email


def validate_document(row, users, departments):
    key = mandatory_int(row['doc_id'], 'doc_id')
    title = (row['doc_name'] or '').strip()
    if not title:
        raise ValueError('Пустое название документа')
    created_at = normalize_date(row['doc_date'])
    status = normalize_status(row['status_code'])
    user_key = mandatory_int(row['user_id'], 'user_id')
    dept_key = mandatory_int(row['department_id'], 'department_id')
    if user_key not in users:
        raise ValueError(f'Пользователь с ID {user_key} не найден среди перенесённых')
    if dept_key not in departments:
        raise ValueError(f'Отдел с ID {dept_key} не найден среди перенесённых')
    return key, title, users[user_key], departments[dept_key], created_at, status, row['file_path']
