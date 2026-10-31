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
    monkeypatch.setattr(b, 'owner_menu_set', True)
    for state in _SESSION_STATES:
        monkeypatch.setattr(bot_module, state, {})
    tmp_db.create_access_request(FRIEND, 'friend', 'Друг')
    tmp_db.set_user_status(FRIEND, 'approved')
    yield sent
    for chat_id in (STRANGER, FRIEND, bot_module.OWNER_ID):
        b.clear_step_handler_by_chat_id(chat_id)


_SESSION_STATES = ('pending_model_selection', 'pending_article_links', 'pending_reassign',
                   'pending_assign_list', 'pending_md_generation')


def _buttons(markup) -> list[str]:
    return [btn.callback_data for row in markup.keyboard for btn in row]


def _sent_to(sent: list, chat_id: int) -> list:
    return [s for s in sent if s[0] == 'send_message' and s[1][0] == chat_id]


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
    assert _buttons(tg[0][2]['reply_markup']) == [bot_module.access.REQUEST_CALLBACK]


def test_pending_user_gets_status_only(tg, tmp_db):
    tmp_db.create_access_request(STRANGER, 's', 'S')
    bot_module.bot.process_new_updates([_message_update(STRANGER, 'https://habr.com/ru/articles/1/')])
    assert tg == [('send_message', (STRANGER, bot_module.MSG_ACCESS_PENDING), {})]


def test_owner_revokes_approved_user(tg, tmp_db):
    owner = bot_module.OWNER_ID
    calls: list = []
    _register_next_step(FRIEND, calls)
    bot_module.pending_model_selection[FRIEND] = {'purpose': 'summary', 'provider': 'polza'}

    bot_module.bot.process_new_updates([_callback_update(owner, f'access:reject:{FRIEND}')])
    assert tmp_db.get_user(FRIEND)['status'] == 'rejected'
    assert ('send_message', (FRIEND, bot_module.MSG_ACCESS_REJECTED), {}) in tg
    assert FRIEND not in bot_module.pending_model_selection

    # После отзыва: next-step не срабатывает, бот молчит
    tg.clear()
    bot_module.bot.process_new_updates([_message_update(FRIEND, 'hello', update_id=5)])
    assert calls == []
    assert tg == []

    # Next-step, оставшийся от гонки с отзывом, сбрасывается и при повторном одобрении
    _register_next_step(FRIEND, calls)
    bot_module.bot.process_new_updates([_callback_update(owner, f'access:approve:{FRIEND}', 6)])
    assert tmp_db.get_user(FRIEND)['status'] == 'approved'
    bot_module.bot.process_new_updates([_message_update(FRIEND, 'after', update_id=7)])
    assert calls == []


def test_users_command_owner_only(tg):
    bot_module.bot.process_new_updates([_message_update(FRIEND, '/users')])
    assert [s[0] for s in tg] == ['reply_to']
    assert tg[0][1][1] == bot_module.MSG_ACCESS_OWNER_ONLY


def test_users_command_lists_with_buttons(tg, tmp_db):
    owner = bot_module.OWNER_ID
    tmp_db.create_access_request(STRANGER, 's', 'S')
    bot_module.bot.process_new_updates([_message_update(owner, '/users')])
    msgs = _sent_to(tg, owner)
    assert msgs[0][1][1] == bot_module.MSG_USERS_TITLE.format(count=2, pending=1, approved=1, rejected=0)
    by_text = {m[1][1]: _buttons(m[2]['reply_markup']) for m in msgs[1:]}
    access = bot_module.access
    assert by_text == {
        f'id {FRIEND}: @friend «Друг» — ✅ одобрен': [access.decision_callback(FRIEND, False)],
        f'id {STRANGER}: @s «S» — ⏳ ждёт решения': [
            access.decision_callback(STRANGER, True), access.decision_callback(STRANGER, False),
        ],
    }


def test_owner_menu_set_once(tg, monkeypatch):
    b = bot_module.bot
    monkeypatch.setattr(b, 'owner_menu_set', False)
    owner = bot_module.OWNER_ID
    b.process_new_updates([_message_update(owner, 'x', update_id=7)])
    b.process_new_updates([_message_update(owner, 'y', update_id=8)])
    menu_calls = [s for s in tg if s[0] == 'set_my_commands']
    assert len(menu_calls) == 1
    assert menu_calls[0][2]['scope'].chat_id == owner
    assert 'users' in [c.command for c in menu_calls[0][1][0]]


def test_callback_without_data_ignored(tg):
    update = _callback_update(bot_module.OWNER_ID, 'x')
    update.callback_query.data = None
    bot_module.bot.process_new_updates([update])
    assert tg == []


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
    assert tg == [('answer_callback_query', ('cb1', bot_module.MSG_ACCESS_OWNER_ONLY), {'show_alert': True})]


def test_request_then_owner_approves(tg, tmp_db):
    owner = bot_module.OWNER_ID
    access = bot_module.access
    bot_module.bot.process_new_updates([_callback_update(STRANGER, access.REQUEST_CALLBACK)])
    to_owner = _sent_to(tg, owner)
    assert len(to_owner) == 1
    assert to_owner[0][1][1] == bot_module.MSG_ACCESS_OWNER_REQUEST.format(user=f'id {STRANGER}: @u «U»')
    assert _buttons(to_owner[0][2]['reply_markup']) == [
        access.decision_callback(STRANGER, True), access.decision_callback(STRANGER, False),
    ]
    assert ('edit_message_text', (bot_module.MSG_ACCESS_REQUESTED, STRANGER, 11), {}) in tg
    assert tmp_db.get_user(STRANGER)['status'] == 'pending'

    # Повторное нажатие — владельца не беспокоим
    tg.clear()
    bot_module.bot.process_new_updates([_callback_update(STRANGER, access.REQUEST_CALLBACK, 3)])
    assert not _sent_to(tg, owner)

    tg.clear()
    bot_module.bot.process_new_updates([_callback_update(owner, f'access:approve:{STRANGER}', 4)])
    assert tmp_db.get_user(STRANGER)['status'] == 'approved'
    assert _sent_to(tg, STRANGER) == [('send_message', (STRANGER, bot_module.MSG_ACCESS_GRANTED), {})]
    edits = [s for s in tg if s[0] == 'edit_message_text']
    assert len(edits) == 1 and 'reply_markup' not in edits[0][2]  # кнопки убраны

    # Повторное «Одобрить» — идемпотентно, пользователю второе сообщение не уходит
    tg.clear()
    bot_module.bot.process_new_updates([_callback_update(owner, f'access:approve:{STRANGER}', 5)])
    assert not _sent_to(tg, STRANGER)


def test_rejected_updates_are_acknowledged(tg, monkeypatch):
    """Отброшенные апдейты должны сдвигать offset, иначе Telegram пришлёт их снова."""
    b = bot_module.bot
    monkeypatch.setattr(b, 'last_update_id', 0)
    b.process_new_updates([_message_update(STRANGER, 'hi', update_id=42)])
    assert b.last_update_id == 42
    b.process_new_updates([_message_update(bot_module.OWNER_ID, 'x', chat_type='group', update_id=43)])
    assert b.last_update_id == 43


def test_rejected_user_request_button_silent(tg, tmp_db):
    tmp_db.create_access_request(STRANGER, 's', 'S')
    tmp_db.set_user_status(STRANGER, 'rejected')
    bot_module.bot.process_new_updates([_callback_update(STRANGER, bot_module.access.REQUEST_CALLBACK)])
    assert tg == []
    assert tmp_db.get_user(STRANGER)['status'] == 'rejected'


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


def test_token_redacted_in_logs_including_traceback():
    import io
    import logging
    token = bot_module.TELEGRAM_TOKEN
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(bot_module._RedactTokenFormatter(logging.Formatter('%(message)s'), token))
    log = logging.getLogger('test_redaction')
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning('fail: %s', f'https://api/bot{token}/send')
        try:
            raise ConnectionError(f'Max retries exceeded with url: /bot{token}/sendMessage')
        except ConnectionError:
            log.exception('gate error')
    finally:
        log.removeHandler(handler)
    output = stream.getvalue()
    assert token not in output
    assert output.count('<TOKEN>') == 2
    assert 'Traceback' in output


def test_install_redaction_wraps_root_handlers_once():
    import logging
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        bot_module._install_token_redaction(bot_module.TELEGRAM_TOKEN)
        bot_module._install_token_redaction(bot_module.TELEGRAM_TOKEN)
        assert isinstance(handler.formatter, bot_module._RedactTokenFormatter)
        assert not isinstance(handler.formatter._base, bot_module._RedactTokenFormatter)
    finally:
        root.removeHandler(handler)


def test_error_text_hides_exception_details():
    leaky = ConnectionError(f'https://api.telegram.org/bot{bot_module.TELEGRAM_TOKEN}/sendMessage')
    assert bot_module._error_text(leaky) == 'ConnectionError'


def test_telebot_logger_handlers_redacted():
    import logging
    handlers = logging.getLogger('TeleBot').handlers
    assert handlers and all(isinstance(h.formatter, bot_module._RedactTokenFormatter) for h in handlers)


def test_blocked_user_notification_failure_reported_to_owner(tg, tmp_db, monkeypatch):
    owner = bot_module.OWNER_ID
    tmp_db.create_access_request(STRANGER, 's', 'S')
    token_url = f'https://api.telegram.org/bot{bot_module.TELEGRAM_TOKEN}/sendMessage'

    def send(chat_id, text, **kw):
        if chat_id == STRANGER:
            raise ConnectionError(token_url)
        tg.append(('send_message', (chat_id, text), kw))
    monkeypatch.setattr(bot_module.bot, 'send_message', send)

    bot_module.bot.process_new_updates([_callback_update(owner, f'access:approve:{STRANGER}')])
    assert tmp_db.get_user(STRANGER)['status'] == 'approved'  # решение в силе
    to_owner = _sent_to(tg, owner)
    assert len(to_owner) == 1
    assert to_owner[0][1][1] == bot_module.MSG_ACCESS_NOTIFY_FAILED.format(
        user=f'id {STRANGER}: @s «S»', error='ConnectionError',
    )
    assert bot_module.TELEGRAM_TOKEN not in to_owner[0][1][1]


def test_decision_edits_owner_message_with_result(tg, tmp_db):
    tmp_db.create_access_request(STRANGER, 's', 'S')
    bot_module.bot.process_new_updates([_callback_update(bot_module.OWNER_ID, f'access:reject:{STRANGER}')])
    edits = [s for s in tg if s[0] == 'edit_message_text']
    assert edits == [('edit_message_text', (f'id {STRANGER}: @s «S» — ❌ отклонён', bot_module.OWNER_ID, 11), {})]


def test_users_command_rejected_user_gets_approve_button(tg, tmp_db):
    owner = bot_module.OWNER_ID
    tmp_db.set_user_status(FRIEND, 'rejected')
    bot_module.bot.process_new_updates([_message_update(owner, '/users')])
    buttons = [_buttons(m[2]['reply_markup']) for m in _sent_to(tg, owner)[1:]]
    assert buttons == [[bot_module.access.decision_callback(FRIEND, True)]]


def test_users_command_empty(tg, tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db.DB_PATH)
    conn.execute('DELETE FROM users')
    conn.commit()
    conn.close()
    bot_module.bot.process_new_updates([_message_update(bot_module.OWNER_ID, '/users')])
    assert tg == [('send_message', (bot_module.OWNER_ID, bot_module.MSG_USERS_EMPTY), {})]


@pytest.mark.parametrize('user_id, expected', [
    (FRIEND, 'MSG_ACCESS_ALREADY'),
    (STRANGER, 'MSG_ACCESS_PENDING'),
])
def test_repeat_request_press_alerts(tg, tmp_db, user_id, expected):
    tmp_db.create_access_request(STRANGER, 's', 'S')
    bot_module.bot.process_new_updates([_callback_update(user_id, bot_module.access.REQUEST_CALLBACK)])
    assert tg == [('answer_callback_query', ('cb1', getattr(bot_module, expected)), {'show_alert': True})]


def test_decision_for_unknown_user_alerts(tg):
    bot_module.bot.process_new_updates([_callback_update(bot_module.OWNER_ID, 'access:approve:777')])
    assert tg == [('answer_callback_query', ('cb1', bot_module.MSG_ACCESS_USER_NOT_FOUND), {'show_alert': True})]


def test_owner_menu_retried_after_failure(tg, monkeypatch):
    b = bot_module.bot
    monkeypatch.setattr(b, 'owner_menu_set', False)
    attempts: list = []

    def flaky(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('network')
    monkeypatch.setattr(b, 'set_my_commands', flaky)
    owner = bot_module.OWNER_ID
    for update_id in (20, 21, 22):
        b.process_new_updates([_message_update(owner, 'x', update_id=update_id)])
    assert len(attempts) == 2
    assert b.owner_menu_set is True
