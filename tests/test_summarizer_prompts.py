import summarizer
from summarizer import COMMENT_MAX_CHARS, _select_comments, format_comments


def make(number, text='текст', author='user', reply_to=None, is_post_author=False, score=None):
    return {
        'number': number, 'author': author, 'text': text,
        'reply_to': reply_to, 'is_post_author': is_post_author, 'score': score,
    }


# =============================================================================
# _select_comments
# =============================================================================


def test_select_drops_negative_score_and_keeps_original_order():
    comments = [make(1, score=2), make(2, score=-1), make(3, score=10), make(4)]

    assert [c['number'] for c in _select_comments(comments, budget=10_000)] == [1, 3, 4]


def test_select_prefers_post_author_then_score_within_budget():
    comments = [
        make(1, text='a' * 100, score=1),
        make(2, text='b' * 100, score=50),
        make(3, text='c' * 100, is_post_author=True, score=0),
    ]

    selected = _select_comments(comments, budget=200)

    assert [c['number'] for c in selected] == [2, 3]


def test_select_counts_long_comment_as_truncated_size():
    comments = [make(1, text='x' * (COMMENT_MAX_CHARS * 3))]

    assert len(_select_comments(comments, budget=COMMENT_MAX_CHARS)) == 1


def test_select_skips_oversized_but_takes_smaller_ones():
    comments = [make(1, text='a' * 500, score=9), make(2, text='b' * 50, score=1)]

    assert [c['number'] for c in _select_comments(comments, budget=100)] == [2]


# =============================================================================
# format_comments
# =============================================================================


def test_format_empty_or_none_returns_empty_string():
    assert format_comments(None) == ''
    assert format_comments([]) == ''
    assert format_comments([make(1, score=-3)]) == ''


def test_format_line_contains_reply_author_mark_and_score():
    comments = [make(1, author='alice', score=5), make(2, author='bob', reply_to=1, is_post_author=True, score=0)]

    result = format_comments(comments)

    assert result.startswith('<comments shown="2" total="2">\n')
    assert result.endswith('</comments>\n')
    assert '#1 alice (+5): текст' in result
    assert '#2 → #1 bob [автор статьи] (+0): текст' in result


def test_format_truncates_long_comment():
    result = format_comments([make(1, text='x' * (COMMENT_MAX_CHARS + 10))])

    assert 'x' * COMMENT_MAX_CHARS + '...' in result
    assert 'x' * (COMMENT_MAX_CHARS + 1) not in result


def test_format_header_counts_all_comments():
    comments = [make(1), make(2, score=-1)]

    assert '<comments shown="1" total="2">' in format_comments(comments)


# =============================================================================
# Prompts
# =============================================================================


def test_infostart_prompt_wraps_article_and_comments_in_tags():
    article = {'title': 'T', 'author': 'A', 'content': 'C', 'comments': [make(1, author='alice')]}

    system_prompt, user_prompt = summarizer._create_infostart_prompt(article)

    assert '<article>' in user_prompt and '</article>' in user_prompt
    assert user_prompt.index('</article>') < user_prompt.index('<comments')
    assert '#1 alice: текст' in user_prompt
    assert '«В комментариях:»' in system_prompt


def test_habr_prompt_without_comments_has_no_comments_block():
    system_prompt, user_prompt = summarizer._create_habr_prompt({'title': 'T', 'content': 'C'})

    assert '<comments' not in user_prompt
    assert '«В комментариях:»' in system_prompt


def test_summary_prompts_keep_structure_out_of_user_prompt():
    # Output structure lives only in the system prompt: a second copy in the user
    # prompt previously contradicted it (sentence limits, comments paragraph)
    for template in (summarizer.HABR_USER_PROMPT_TEMPLATE, summarizer.INFOSTART_USER_PROMPT_TEMPLATE):
        assert 'В комментариях' not in template
        assert 'предложени' not in template


# =============================================================================
# GitHub prompt
# =============================================================================


def test_github_prompt_passes_activity_metadata():
    repo = {
        'title': 'o/r', 'description': 'd', 'stars': '42', 'language': 'Go',
        'pushed_at': '2026-10-01', 'archived': False, 'license': 'MIT',
        'files': ['README.md'], 'content': 'readme',
    }

    _, user_prompt = summarizer._create_github_prompt(repo)

    assert 'Последний push: 2026-10-01' in user_prompt
    assert 'В архиве: нет' in user_prompt
    assert 'Лицензия: MIT' in user_prompt
    assert '<docs>\nreadme\n</docs>' in user_prompt


def test_github_prompt_marks_missing_metadata_explicitly():
    # HTML fallback: GitHub API unavailable, activity signals unknown
    repo = {'title': 'o/r', 'pushed_at': None, 'archived': None, 'license': None, 'content': 'x'}

    _, user_prompt = summarizer._create_github_prompt(repo)

    no_data = summarizer.GITHUB_NO_DATA
    for field in ('Последний push', 'В архиве', 'Лицензия'):
        assert f'{field}: {no_data}' in user_prompt
