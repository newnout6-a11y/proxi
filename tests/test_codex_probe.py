"""The installed-client probe must fail on retries or parsing failures."""
import pytest

from scripts.probe_codex_responses import MARKER, assess_case


@pytest.mark.parametrize('count', [None, 0, 7])
def test_one_completed_synthetic_response_is_accepted(count):
    assert assess_case(count, 0, 1, MARKER, '')['passed']


@pytest.mark.parametrize('exit_code,requests,stdout,stderr', [
    (1, 1, MARKER, ''),
    (0, 2, MARKER, ''),
    (0, 0, MARKER, ''),
    (0, 1, '', ''),
    (0, 1, MARKER, 'failed to parse ResponseCompleted: missing field'),
    (0, 1, MARKER + ' failed to parse ResponseCompleted', ''),
])
def test_success_text_does_not_hide_a_failed_or_repeated_response(exit_code, requests, stdout, stderr):
    assert not assess_case(None, exit_code, requests, stdout, stderr)['passed']
