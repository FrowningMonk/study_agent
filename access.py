"""
Доступ к боту: регистрация пользователей с одобрением владельцем.

Состояния пользователя: нет записи → pending → approved / rejected.
Владелец (OWNER_ID) имеет доступ всегда и в таблице users не хранится.

Здесь только решения и переходы состояний — тексты и кнопки живут в bot.py.
Модуль не зависит от telebot, чтобы тесты не упирались в создание бота.
"""

import re
from dataclasses import dataclass
from enum import Enum

import database

__all__ = [
    'REQUEST_CALLBACK',
    'Gate',
    'RequestOutcome',
    'DecideOutcome',
    'DecideResult',
    'parse_owner_id',
    'parse_decision_callback',
    'decision_callback',
    'gate',
    'request_access',
    'decide',
    'describe_user',
]

# Callback кнопки «Запросить доступ» — единственный, доступный неодобренным
REQUEST_CALLBACK = 'access_request'

# Callback решения владельца: access:approve:{user_id} / access:reject:{user_id}
_DECISION_RE = re.compile(r'^access:(approve|reject):(\d+)$')


class Gate(Enum):
    """Что делать с апдейтом до диспетчеризации обработчикам."""
    PASS = 'pass'                    # пропустить к обработчикам
    IGNORE = 'ignore'                # молча отбросить (группы, rejected, нет отправителя)
    OFFER = 'offer'                  # сообщение от незнакомца: объяснение + кнопка заявки
    PENDING = 'pending'              # сообщение от ожидающего: «заявка на рассмотрении»
    DENY_CALLBACK = 'deny_callback'  # кнопка от неодобренного: отказ во всплывашке


class RequestOutcome(Enum):
    REQUESTED = 'requested'              # заявка создана — уведомить владельца
    ALREADY_PENDING = 'already_pending'  # повторное нажатие, владельца не трогаем
    ALREADY_ALLOWED = 'already_allowed'  # владелец или уже одобрен
    REJECTED = 'rejected'                # отклонён — молчим


class DecideOutcome(Enum):
    DECIDED = 'decided'
    UNCHANGED = 'unchanged'        # повторное нажатие на уже обработанной заявке
    NOT_OWNER = 'not_owner'        # подделанный callback не от владельца
    UNKNOWN_USER = 'unknown_user'  # заявителя нет в таблице


@dataclass(frozen=True)
class DecideResult:
    outcome: DecideOutcome
    record: dict | None = None


def parse_owner_id(raw: str | None) -> int:
    """
    Разбирает OWNER_ID из окружения.

    Raises:
        ValueError: значение пустое или не положительное целое
    """
    value = (raw or '').strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError('OWNER_ID должен быть числовым Telegram ID владельца')
    return int(value)


def decision_callback(user_id: int, approve: bool) -> str:
    """Собирает callback_data кнопки решения владельца."""
    return f"access:{'approve' if approve else 'reject'}:{user_id}"


def parse_decision_callback(data: str) -> tuple[bool, int] | None:
    """Разбирает callback решения владельца → (approve, user_id) или None."""
    match = _DECISION_RE.match(data or '')
    if not match:
        return None
    return match.group(1) == 'approve', int(match.group(2))


def gate(user_id: int | None, chat_type: str | None, callback_data: str | None, owner_id: int) -> Gate:
    """
    Единая проверка доступа для любого апдейта.

    Args:
        user_id: отправитель (from_user.id) или None
        chat_type: тип чата апдейта ('private', 'group', ...) или None
        callback_data: данные кнопки для callback_query, None для сообщений
        owner_id: Telegram ID владельца

    Returns:
        Решение Gate
    """
    if user_id is None or chat_type != 'private':
        return Gate.IGNORE
    if user_id == owner_id:
        return Gate.PASS

    record = database.get_user(user_id)
    status = record['status'] if record else None
    if status == 'approved':
        return Gate.PASS
    if status == 'rejected':
        return Gate.IGNORE

    is_callback = callback_data is not None
    if is_callback and callback_data == REQUEST_CALLBACK:
        return Gate.PASS
    if is_callback:
        return Gate.DENY_CALLBACK
    return Gate.PENDING if status == 'pending' else Gate.OFFER


def request_access(user_id: int, username: str | None, first_name: str | None, owner_id: int) -> RequestOutcome:
    """Обрабатывает нажатие «Запросить доступ». Повторная заявка ничего не меняет."""
    if user_id == owner_id:
        return RequestOutcome.ALREADY_ALLOWED
    if database.create_access_request(user_id, username, first_name):
        return RequestOutcome.REQUESTED
    status = database.get_user(user_id)['status']
    if status == 'approved':
        return RequestOutcome.ALREADY_ALLOWED
    if status == 'rejected':
        return RequestOutcome.REJECTED
    return RequestOutcome.ALREADY_PENDING


def decide(target_id: int, approve: bool, by_user_id: int, owner_id: int) -> DecideResult:
    """
    Решение владельца по пользователю: одобрить или отклонить/отозвать.

    Проверяет, что решает именно владелец, — callback-данные можно подделать.
    """
    if by_user_id != owner_id:
        return DecideResult(DecideOutcome.NOT_OWNER)
    record = database.get_user(target_id)
    if not record:
        return DecideResult(DecideOutcome.UNKNOWN_USER)
    new_status = 'approved' if approve else 'rejected'
    if record['status'] == new_status:
        return DecideResult(DecideOutcome.UNCHANGED, record)
    database.set_user_status(target_id, new_status)
    return DecideResult(DecideOutcome.DECIDED, database.get_user(target_id))


def describe_user(record: dict) -> str:
    """Как пользователь выглядит у владельца: «@ivan (Иван), id 12345» или «Иван, id 12345»."""
    name = record.get('first_name') or 'без имени'
    if record.get('username'):
        return f"@{record['username']} ({name}), id {record['user_id']}"
    return f"{name}, id {record['user_id']}"
