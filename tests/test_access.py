import pytest

import access

OWNER = 1000
ALICE = {'user_id': 2000, 'username': 'alice', 'first_name': 'Алиса'}


def _request(user=ALICE):
    return access.request_access(user['user_id'], user['username'], user['first_name'], OWNER)


def test_owner_always_allowed(tmp_db):
    assert access.gate(OWNER, 'private', None, OWNER) == access.Gate.PASS
    assert access.gate(OWNER, 'private', 'view_idea:1', OWNER) == access.Gate.PASS
    assert tmp_db.get_user(OWNER) is None  # без записи в таблице


def test_unknown_user_denied(tmp_db):
    assert access.gate(ALICE['user_id'], 'private', None, OWNER) == access.Gate.OFFER
    assert access.gate(ALICE['user_id'], 'private', 'view_idea:1', OWNER) == access.Gate.DENY_CALLBACK


def test_unknown_user_may_press_request_button(tmp_db):
    assert access.gate(ALICE['user_id'], 'private', access.REQUEST_CALLBACK, OWNER) == access.Gate.PASS


def test_pending_and_rejected_denied(tmp_db):
    uid = ALICE['user_id']
    _request()
    assert access.gate(uid, 'private', None, OWNER) == access.Gate.PENDING
    assert access.gate(uid, 'private', 'cache:show:1', OWNER) == access.Gate.DENY_CALLBACK

    access.decide(uid, approve=True, by_user_id=OWNER, owner_id=OWNER)
    assert access.gate(uid, 'private', None, OWNER) == access.Gate.PASS
    assert access.gate(uid, 'private', 'cache:show:1', OWNER) == access.Gate.PASS

    access.decide(uid, approve=False, by_user_id=OWNER, owner_id=OWNER)
    assert access.gate(uid, 'private', None, OWNER) == access.Gate.IGNORE
    # Отклонённый не может даже повторно запросить доступ — молчим
    assert access.gate(uid, 'private', access.REQUEST_CALLBACK, OWNER) == access.Gate.IGNORE


def test_request_access_idempotent(tmp_db):
    assert _request() == access.RequestOutcome.REQUESTED
    first = tmp_db.get_user(ALICE['user_id'])
    assert _request() == access.RequestOutcome.ALREADY_PENDING
    second = tmp_db.get_user(ALICE['user_id'])
    assert second['requested_at'] == first['requested_at']
    assert len(tmp_db.list_users()) == 1


def test_owner_request_is_noop(tmp_db):
    assert access.request_access(OWNER, 'owner', 'Owner', OWNER) == access.RequestOutcome.ALREADY_ALLOWED
    assert tmp_db.get_user(OWNER) is None


@pytest.mark.parametrize('chat_type', ['group', 'supergroup', 'channel', None])
def test_group_chat_ignored(tmp_db, chat_type):
    assert access.gate(OWNER, chat_type, None, OWNER) == access.Gate.IGNORE
    assert access.gate(OWNER, chat_type, 'view_idea:1', OWNER) == access.Gate.IGNORE


def test_update_without_user_ignored(tmp_db):
    assert access.gate(None, 'private', None, OWNER) == access.Gate.IGNORE


def test_decide_requires_owner(tmp_db):
    bob = {'user_id': 3000, 'username': None, 'first_name': 'Боб'}
    _request()
    _request(bob)
    access.decide(bob['user_id'], approve=True, by_user_id=OWNER, owner_id=OWNER)

    # Одобренный, но не владелец, присылает подделанный callback
    result = access.decide(ALICE['user_id'], approve=True, by_user_id=bob['user_id'], owner_id=OWNER)
    assert result.outcome == access.DecideOutcome.NOT_OWNER
    assert tmp_db.get_user(ALICE['user_id'])['status'] == 'pending'


def test_decide_unknown_user(tmp_db):
    result = access.decide(4242, approve=True, by_user_id=OWNER, owner_id=OWNER)
    assert result.outcome == access.DecideOutcome.UNKNOWN_USER
    assert tmp_db.get_user(4242) is None


def test_decide_repeat_is_idempotent(tmp_db):
    _request()
    first = access.decide(ALICE['user_id'], approve=True, by_user_id=OWNER, owner_id=OWNER)
    second = access.decide(ALICE['user_id'], approve=True, by_user_id=OWNER, owner_id=OWNER)
    assert first.outcome == access.DecideOutcome.DECIDED
    assert second.outcome == access.DecideOutcome.UNCHANGED
    assert second.record['decided_at'] == first.record['decided_at']


def test_parse_decision_callback():
    assert access.parse_decision_callback('access:approve:2000') == (True, 2000)
    assert access.parse_decision_callback('access:reject:2000') == (False, 2000)
    for bad in ['access:approve:', 'access:approve:abc', 'access:maybe:1', 'access:approve:1:2', 'x']:
        assert access.parse_decision_callback(bad) is None


@pytest.mark.parametrize('raw', [None, '', '  ', 'abc', '12.5', '-5', '0'])
def test_parse_owner_id_rejects_invalid(raw):
    with pytest.raises(ValueError):
        access.parse_owner_id(raw)


def test_parse_owner_id_ok():
    assert access.parse_owner_id(' 123456 ') == 123456


def test_describe_user():
    assert access.describe_user({'user_id': 5, 'username': 'ivan', 'first_name': 'Иван'}) == '@ivan (Иван), id 5'
    assert access.describe_user({'user_id': 5, 'username': None, 'first_name': 'Иван'}) == 'Иван, id 5'
    assert access.describe_user({'user_id': 5, 'username': None, 'first_name': None}) == 'без имени, id 5'
