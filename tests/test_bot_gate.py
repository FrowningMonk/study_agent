"""
Фильтр доступа в bot.AccessControlledBot на реальной диспетчеризации telebot.

Сеть не трогаем: методы отправки бота подменены записью вызовов.
"""

import os

import pytest
from telebot import types

# bot.py падает при импорте без токена и OWNER_ID; .env (если есть) их переопределит
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:TEST')
os.environ.setdefault('OWNER_ID', '1000')

import bot as bot_module  # noqa: E402

STRANGER = 2000
FRIEND = 3000


@pytest.fixture
def tg(tmp_db, monkeypatch):
    """Бот без сети и без потоков; возвращает список отправленного."""
    sent: list[tuple[str, tuple, dict]] = []
    b = bot_module.bot
    for name in ('send_message', 'answer_callback_query', 'edit_message_text', 'reply_to', 'set_my_commands'):
        monkeypatch.setattr(b, name, lambda *a, _n=name, **kw: sent.append((_n, a, kw)))
    monkeypatch.setattr(b, 'threaded', False)
    b.owner_menu_set = True
    tmp_db.create_access_request(FRIEND, 'friend', 'Друг')
    tmp_db.set_user_status(FRIEND, 'approved')
    yield sent
    for chat_id in (STRANGER, FRIEND, bot_module.OWNER_ID):
        b.clear_step_handler_by_chat_id(chat_id)


def _message_update(user_id: int, text: str, chat_type: str = 'private', update_id: int = 1) -> types.Update:
    return types.Update.de_json({
        'update_id': update_id,
        'message': {
            'message_id': 10,
            'date': 0,
            'chat': {'id': user_id, 'type': chat_type},
            'from': {'id': user_id, 'is_bot': False, 'first_name': 'U'},
            'text': text,
        },
    })


def _callback_update(user_id: int, data: str, update_id: int = 2) -> types.Update:
    return types.Update.de_json({
        'update_id': update_id,
        'callback_query': {
            'id': 'cb1',
            'from': {'id': user_id, 'is_bot': False, 'first_name': 'U', 'username': 'u'},
            'chat_instance': 'x',
            'data': data,
            'message': {
                'message_id': 11,
                'date': 0,
                'chat': {'id': user_id, 'type': 'private'},
                'text': 'old',
            },
        },
    })


def _register_next_step(chat_id: int, calls: list) -> None:
    bot_module.bot.register_next_step_handler_by_chat_id(chat_id, lambda m: calls.append(m.text))


def test_stranger_gets_offer_and_no_handler(tg):
    bot_module.bot.process_new_updates([_message_update(STRANGER, '/model')])
    assert [s[0] for s in tg] == ['send_message']
    assert tg[0][1][1] == bot_module.MSG_ACCESS_OFFER


def test_next_step_handler_blocked_for_revoked_user(tg, tmp_db):
    calls: list = []
    _register_next_step(FRIEND, calls)
    tmp_db.set_user_status(FRIEND, 'rejected')
    bot_module.bot.process_new_updates([_message_update(FRIEND, 'deepseek/x')])
    assert calls == []
    assert tg == []  # отклонённому — тишина


def test_next_step_handler_runs_for_approved(tg):
    calls: list = []
    _register_next_step(FRIEND, calls)
    bot_module.bot.process_new_updates([_message_update(FRIEND, 'hello')])
    assert calls == ['hello']


def test_group_message_from_owner_ignored(tg):
    calls: list = []
    _register_next_step(bot_module.OWNER_ID, calls)
    bot_module.bot.process_new_updates([_message_update(bot_module.OWNER_ID, '/start', chat_type='group')])
    assert calls == []
    assert tg == []


def test_forged_callback_from_stranger_denied(tg):
    bot_module.bot.process_new_updates([_callback_update(STRANGER, 'view_idea:1')])
    assert [s[0] for s in tg] == ['answer_callback_query']
    assert tg[0][2].get('show_alert') is True


def test_forged_decision_from_approved_non_owner(tg, tmp_db):
    tmp_db.create_access_request(STRANGER, 's', 'S')
    data = f'access:approve:{STRANGER}'
    bot_module.bot.process_new_updates([_callback_update(FRIEND, data)])
    assert tmp_db.get_user(STRANGER)['status'] == 'pending'


def test_request_then_owner_approves(tg, tmp_db):
    owner = bot_module.OWNER_ID
    bot_module.bot.process_new_updates([_callback_update(STRANGER, bot_module.access.REQUEST_CALLBACK)])
    to_owner = [s for s in tg if s[0] == 'send_message' and s[1][0] == owner]
    assert len(to_owner) == 1
    assert tmp_db.get_user(STRANGER)['status'] == 'pending'

    # Повторное нажатие — владельца не беспокоим
    tg.clear()
    bot_module.bot.process_new_updates([_callback_update(STRANGER, bot_module.access.REQUEST_CALLBACK, 3)])
    assert not [s for s in tg if s[0] == 'send_message' and s[1][0] == owner]

    tg.clear()
    bot_module.bot.process_new_updates([_callback_update(owner, f'access:approve:{STRANGER}', 4)])
    assert tmp_db.get_user(STRANGER)['status'] == 'approved'
    assert ('send_message', (STRANGER, bot_module.MSG_ACCESS_GRANTED), {}) in tg


def test_non_message_updates_dropped(tg):
    update = types.Update.de_json({
        'update_id': 9,
        'edited_message': {
            'message_id': 10, 'date': 0, 'text': 'x',
            'chat': {'id': bot_module.OWNER_ID, 'type': 'private'},
            'from': {'id': bot_module.OWNER_ID, 'is_bot': False, 'first_name': 'O'},
        },
    })
    assert bot_module._admit_update(update) is False
