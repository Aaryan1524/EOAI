import json

import pytest

from experiments.guard.roles import RoleModel
from experiments.guard.semantic import grade_criterion
from test_guard_roles import _DIGEST, role_relay


_EVIDENCE = {"output.coverage": {"customer_id": "acme"}, "trace.5": {"has_next_page": True}}
_PAYLOAD = {
    "request": "List every Acme invoice.",
    "public_context": {},
    "tool_names": ["invoice.search"],
    "target": {"id": "req_1", "description": "Identify the correct account."},
    "evidence": _EVIDENCE,
}
_PASS = {"verdict": "PASS", "citation": "output.coverage", "finding": "SUPPORTS"}
_FAIL = {"verdict": "FAIL", "citation": "trace.5", "finding": "CONTRADICTS"}
_UNKNOWN = {"verdict": "UNKNOWN", "citation": "NONE", "finding": "INDETERMINATE"}


@pytest.mark.parametrize(("reply", "expected", "key", "criterion"), [
    (_PASS, True, "output.coverage", "Identify the correct account."),
    (_FAIL, False, "trace.5", "Exhaust all invoice pages."),
])
def test_grounded_grade_resolves_the_reference_to_observed_evidence(reply, expected, key, criterion):
    payload = dict(_PAYLOAD, target={"id": "req_1", "description": criterion})
    grade = grade_criterion(payload, lambda *_: reply)
    assert grade.passed is expected
    assert "Observed:" in grade.why and key in grade.why
    assert grade.evidence == (key,)
    attempt, = grade.attempts
    assert json.loads(attempt.raw_reply) == reply
    assert attempt.error is None


@pytest.mark.parametrize(("reply", "error"), [
    ("Yes", "Invalid semantic verdict fields"),
    ({"passed": True, "why": "Yes", "evidence": ["output.coverage"]}, "Invalid semantic verdict fields"),
    (dict(_PASS, verdict=True), "Invalid semantic verdict label"),
    (dict(_PASS, citation=None), "Unknown or empty semantic evidence key"),
    (dict(_PASS, citation="output.missing"), "Unknown or empty semantic evidence key"),
    (dict(_PASS, finding="CONTRADICTS"), "conflicts with verdict"),
    (dict(_FAIL, finding="SUPPORTS"), "conflicts with verdict"),
    (dict(_UNKNOWN, citation="trace.5"), "UNKNOWN verdict contains a decisive citation"),
    (_UNKNOWN, "Reviewer returned UNKNOWN"),
])
def test_an_unresolved_or_unsupported_reply_remains_ungraded(reply, error):
    calls = []

    def complete(role, purpose, instruction, payload, temperature):
        assert (role, purpose, temperature) == ("reviewer", "criterion", 0.0)
        assert payload == _PAYLOAD
        payload["evidence"].clear()
        calls.append(instruction)
        return reply

    grade = grade_criterion(_PAYLOAD, complete)
    assert grade.passed is None and grade.evidence == ()
    assert len(calls) == len(grade.attempts) == 2
    assert calls[0] != calls[1]
    assert all(error in attempt.error for attempt in grade.attempts)
    assert all(attempt.raw_reply == (reply if isinstance(reply, str) else json.dumps(reply))
               for attempt in grade.attempts)


def test_invalid_wire_reply_retains_exact_text_and_retries(role_relay, monkeypatch):
    calls, _, replies = role_relay
    role_model = RoleModel(_DIGEST)
    malformed = ' { "verdict" : "UNKNOWN", "verdict": "PASS" }\n'
    valid = json.dumps(_PASS, indent=2) + "\n"
    wire = RoleModel.__call__

    def sequence(self, *args, **kwargs):
        replies["chat"]["message"]["content"] = malformed if not calls else valid
        return wire(self, *args, **kwargs)

    monkeypatch.setattr(RoleModel, "__call__", sequence)
    grade = grade_criterion(_PAYLOAD, role_model)
    first, second = grade.attempts
    assert grade.passed is True
    assert first.raw_reply == malformed and first.error == "Invalid JSON in role worker"
    assert second.raw_reply == valid and second.error is None
    assert [row["raw_reply"] for row in role_model.metrics] == [malformed, valid]
    assert role_model.metrics[0]["response_parse_error"] == first.error
    assert [row["status"] for row in role_model.metrics] == ["invalid_response", "ok"]
    sent = [body for _, _, body in calls if body is not None]
    assert sent[0]["messages"][1] == sent[1]["messages"][1]
    assert sent[0]["messages"][0] != sent[1]["messages"][0]
    assert sent[0]["format"]["properties"]["verdict"]["enum"] == ["PASS", "FAIL", "UNKNOWN"]


def test_reviewer_infrastructure_failure_still_propagates():
    def unavailable(*_):
        raise RuntimeError("Review provider unavailable")

    with pytest.raises(RuntimeError, match="Review provider unavailable"):
        grade_criterion(_PAYLOAD, unavailable)
