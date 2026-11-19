import json

from enterprise_ops.agent.runner import run_task
from enterprise_ops.fixtures.catalog import load_task
from enterprise_ops.sandbox import seed_snapshot

from test_runner import ScriptModel, _commit, _preview


def _run(path, name, steps, *, writes=False):
    entry = load_task(name, authorize_writes=writes)
    snapshot = seed_snapshot(path / 'snapshots', name, scenario=entry.scenario, profile=entry.profile)
    return run_task(snapshot, entry.task, ScriptModel(steps), run_id=name, runs_root=path / 'runs', faults=entry.faults)


def _trace(result):
    return [json.loads(line) for line in (result.trace_path / 'trace.jsonl').read_text().splitlines()]


def _search(account):
    return ('invoice.search', {'account_ref': account, 'page_size': 3})


def _next(account):
    return ('invoice.search', lambda previous: {
        'account_ref': account, 'page_size': 3, 'page_cursor': previous['next_cursor'],
    })


def _excerpts(*documents):
    return [('document.get_excerpt', {'document_id': name, 'document_version': 1, 'span_id': 'terms'}) for name in documents]


def test_level_three_pages_changed_schema_and_supports_due_dates(tmp_path):
    result = _run(tmp_path, 'acme-paged-v2', [
        ('customer.search', {'query': 'Acme Property Group'}),
        _search('acme'), _next('acme'), _next('acme'),
        ('contract.get', {'contract_id': 'C-202'}),
        ('contract.list_amendments', {'contract_id': 'C-202'}),
        *_excerpts('D-ACME', 'D-ACME-SIGNED'),
        *[('invoice.assess', {'invoice_id': f'INV-{code}'}) for code in range(104, 111)],
        '{"customer_id":"acme","review_reason":null}',
    ])
    output = result.output
    assert output.status == 'resolved' and output.coverage.invoice_search_exhausted
    assert [item.invoice_id for item in output.decisions] == [f'INV-{code}' for code in range(104, 111)]
    assert output.decisions[0].effective_due_date == '2026-10-01'
    assert any(item.source_id == 'D-ACME-SIGNED' for item in output.evidence)
    assert not output.actions
    pages = [event for event in _trace(result) if event.get('tool') == 'invoice.search']
    assert all('account_ref' in event['arguments'] for event in pages)
    assert all(event['response']['meta']['schema_version'] == 'v2' for event in pages)
    assert len(pages) == 3


def test_level_four_retries_pages_leaves_conflict_and_verifies_one_ticket(tmp_path):
    result = _run(tmp_path, 'compound-escalation', [
        ('customer.get', {'customer_id': 'harborline'}),
        _search('harborline'), _next('harborline'),
        ('contract.get', {'contract_id': 'C-H901'}),
        ('contract.list_amendments', {'contract_id': 'C-H901'}),
        *_excerpts('D-HARBORLINE', 'D-HARBORLINE-SUMMER', 'D-HARBORLINE-AUGUST', 'D-HARBORLINE-DRAFT'),
        ('payment.list_for_invoice', {'invoice_id': 'INV-H01'}),
        ('dispute.list_for_invoice', {'invoice_id': 'INV-H04'}),
        ('policy.get_escalation', {}),
        *[('invoice.assess', {'invoice_id': f'INV-H{code:02}'}) for code in range(2, 7)],
        ('ticket.search', {'invoice_id': 'INV-H02'}),
        ('ticket.search', {'invoice_id': 'INV-H01'}),
        ('invoice.assess', {'invoice_id': 'INV-H01'}),
        ('ticket.preview_create', _preview),
        ('ticket.create', _commit),
        '{"customer_id":"harborline","review_reason":null}',
    ], writes=True)
    output = result.output
    assert output.status == 'partial' and output.review_required
    assert output.coverage.invoice_search_exhausted
    assert {item.invoice_id: item.decision for item in output.decisions} == {
        'INV-H01': 'eligible', 'INV-H02': 'eligible', 'INV-H03': 'needs_review',
        'INV-H04': 'ineligible', 'INV-H05': 'ineligible', 'INV-H06': 'ineligible',
    }
    assert output.decisions[0].outstanding_minor == 800_000
    completed, = [action for action in output.actions if action.status == 'completed']
    assert completed.invoice_id == 'INV-H01' and completed.confirmed_by_readback
    assert any(action.status == 'already_active' and action.result_id == 'TCK-H02' for action in output.actions)
    assert [event.get('tool') for event in _trace(result) if event['event'] == 'retry'] == ['invoice.search']
    delta = json.loads((result.trace_path / 'state_delta.json').read_text())
    assert [(event['action'], event['id']) for event in delta] == [('ticket.create', completed.audit_id)]
