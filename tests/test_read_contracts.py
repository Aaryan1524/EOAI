from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from enterprise_ops.storage.repository import DocumentExcerpt
from enterprise_ops.tools import reads
from enterprise_ops.tools.contracts import ToolFailure


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("clock.now", {"as_of": "2026-11-19T00:00:00Z"}),
        ("customer.search", {"query": "  \t "}),
        ("customer.search", {"query": "acme", "limit": "2"}),
        ("invoice.search", {"customer_id": "acme", "statuses": ["paid"]}),
        ("invoice.search", {"customer_id": "acme", "stated_due_date": "2026-10-01"}),
        ("invoice.get", {"invoice_id": "INV-104", "tenant_id": "northstar_property"}),
        ("document.get_excerpt", {"document_id": "D-ACME", "document_version": True, "span_id": "terms"}),
    ],
)
def test_read_tools_reject_out_of_contract_arguments(tool, arguments):
    with pytest.raises(ValidationError):
        reads.READ_TOOLS[tool].arguments.model_validate(arguments)


def test_excerpt_bound_counts_characters_and_rejects_oversized_spans(monkeypatch):
    tool = reads.READ_TOOLS["document.get_excerpt"]
    args = tool.arguments.model_validate({
        "document_id": "D-ACME",
        "document_version": 1,
        "span_id": "terms",
    })
    evidence_calls = []

    def evidence(*coordinates):
        evidence_calls.append(coordinates)
        return "evidence"

    call = SimpleNamespace(connection=None, tenant_id="test", evidence=evidence)
    for length in (reads.MAX_EXCERPT_LENGTH, reads.MAX_EXCERPT_LENGTH + 1):
        span = DocumentExcerpt("é" * length, "f" * 64, 0, length, "contract", "locator")
        monkeypatch.setattr(reads, "load_document_span", lambda *_: span)
        if length == reads.MAX_EXCERPT_LENGTH:
            result = tool.result.model_validate(tool.handler(call, args))
            assert result.excerpt == span.excerpt
            assert result.end_offset == length
            assert result.evidence_id == "evidence"
        else:
            with pytest.raises(ToolFailure) as failure:
                tool.handler(call, args)
            assert failure.value.error.code == "PRECONDITION_FAILED"
    assert evidence_calls == [("document_span", "D-ACME", 1, "locator")]
