"""Assemble the run result from scoped, recorded gateway responses."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from enterprise_ops.tools.actions import ApprovalResult, CommitArgs, PreviewResult, Receipt, TicketResult
from enterprise_ops.tools.contracts import Id, Page, PageArgs, Revision, StrictModel, ToolMeta, ToolResponse
from enterprise_ops.tools.gateway import TOOLS
from enterprise_ops.tools.reads import (
    AssessmentResult, AssessmentStatus, Currency, CustomerHit, CustomerSearchArgs, ExcerptResult, InvoiceResult,
    InvoiceSearchArgs, IsoDate, IsoInstant, MinorUnits,
)
from enterprise_ops.storage.audit import canonical
from enterprise_ops.tools.session import Observation

ActionType = Literal["create_escalation_ticket", "request_approval"]
ActionStatus = Literal[
    "completed", "already_active", "pending_approval", "blocked", "not_attempted",
    "committed_unverified", "commit_unknown",
]
ACTION_TYPES: dict[str, ActionType] = {
    "ticket.create": "create_escalation_ticket",
    "approval.request": "request_approval",
}
ACTIVE_TICKET_STATES = {"open", "in_progress"}
REVIEW_MESSAGES = {
    "AMBIGUOUS_CUSTOMER": "Provide a stable customer ID to resolve the matching accounts.",
    "CUSTOMER_NOT_FOUND": "No exact customer name matched the requested search.",
    "CUSTOMER_UNRESOLVED": "Confirm the customer ID or supply the exact legal name.",
    "CUSTOMER_SEARCH_INCOMPLETE": "Customer search coverage is incomplete.",
    "WRITE_SCOPE_REQUIRED": "Provide authorization for the requested escalation.",
    "CLARIFICATION_REQUIRED": "Clarify the requested operation or scope.",
    "MISSING_INFORMATION": "The request needs additional information before work can proceed.",
    "UNSUPPORTED_REQUEST": "The requested operation requires human review.",
}
ALLOWED_REVIEW_REQUESTS = {
    "MISSING_INFORMATION", "CLARIFICATION_REQUIRED", "UNSUPPORTED_REQUEST", "WRITE_SCOPE_REQUIRED",
}
INCOMPLETE_ACTIONS = {
    "pending_approval", "blocked", "not_attempted", "committed_unverified", "commit_unknown",
}


class Coverage(StrictModel):
    customer_id: Id | None
    state_revision: Revision | None
    invoice_search_exhausted: bool
    observed_invoice_ids: list[Id]
    pending_invoice_ids: list[Id]


class Decision(StrictModel):
    invoice_id: Id
    decision: AssessmentStatus
    reason_codes: list[str]
    effective_due_date: IsoDate | None
    stated_due_date: IsoDate | None
    stated_due_date_differs: bool | None
    days_past_due: int | None
    outstanding_minor: MinorUnits | None
    currency: Currency | None
    evidence_ids: list[Id]


class Action(StrictModel):
    type: ActionType
    invoice_id: Id
    status: ActionStatus
    result_id: Id | None
    audit_id: Id | None
    confirmed_by_readback: bool
    reason_codes: list[str]
    evidence_ids: list[Id]


class MissingInformation(StrictModel):
    code: str
    subject_id: Id | None
    description: Annotated[str, Field(max_length=500)]
    evidence_ids: list[Id]


class Evidence(StrictModel):
    id: Id
    source_type: str
    source_id: Id
    source_version: int
    locator: str
    observed_call_id: Id
    state_revision: Revision


class RunOutput(StrictModel):
    run_id: Id
    task_id: Id
    as_of: IsoInstant
    business_date: IsoDate
    status: Literal["resolved", "partial", "needs_review", "failed"]
    summary: Annotated[str, Field(max_length=500)]
    review_required: bool
    coverage: Coverage
    decisions: list[Decision]
    actions: list[Action]
    missing_information: list[MissingInformation]
    evidence: list[Evidence]


@dataclass(frozen=True)
class _Call:
    args: StrictModel
    data: StrictModel
    meta: ToolMeta
    revision: int

    @property
    def name(self) -> str:
        return self.meta.tool_name


@dataclass
class _Pages:
    revision: int
    arguments: dict
    next_cursor: str | None
    calls: list[_Call] = field(default_factory=list)

    @classmethod
    def from_calls(cls, calls: Iterable[_Call]) -> "_Pages | None":
        chain = None
        for call in calls:
            if not isinstance(call.data, Page) or not isinstance(call.args, PageArgs):
                continue
            args = call.args.model_dump(exclude={"cursor"})
            if call.args.cursor is None:
                chain = cls(call.revision, args, call.data.next_cursor, [call])
            elif (
                chain is not None and call.revision == chain.revision and args == chain.arguments
                and call.args.cursor == chain.next_cursor
            ):
                chain.calls.append(call)
                chain.next_cursor = call.data.next_cursor
        return chain


@dataclass(frozen=True)
class _Failure:
    uncertain: bool
    reason: str


class _Evidence:
    def __init__(self, calls: list[_Call], lookup: Callable[[str], Observation | None]) -> None:
        self.calls = {call.meta.call_id: call for call in calls}
        self.lookup = lookup
        self.entries: dict[str, Evidence] = {}

    def from_call(
        self, call: _Call, source_type: str, *,
        subject: str | None = None, version: int | None = None,
        invoice_id: str | None = None, ids: Iterable[str] | None = None, locator: str | None = None,
    ) -> list[str]:
        found = []
        for identifier in dict.fromkeys(call.meta.evidence_ids if ids is None else ids):
            if identifier not in call.meta.evidence_ids:
                continue
            observation = self.lookup(identifier)
            if observation is None or observation.id != identifier:
                continue
            origin = self.calls.get(observation.call_id)
            if (
                origin is None or identifier not in origin.meta.evidence_ids
                or observation.revision != origin.revision
                or call.meta.sandbox_id != origin.meta.sandbox_id
                or observation.source_type != source_type
                or (subject is not None and observation.source_id != subject)
                or (version is not None and observation.source_version != version)
                or (invoice_id is not None and observation.invoice_id != invoice_id)
                or (locator is not None and observation.locator != locator)
                or (source_type not in {"document_span", "audit_event"} and origin.revision != call.revision)
                or (source_type == "document_span" and not self._span_observed(identifier, origin, observation))
            ):
                continue
            self.entries[identifier] = Evidence(
                id=identifier, source_type=observation.source_type, source_id=observation.source_id,
                source_version=observation.source_version, locator=observation.locator,
                observed_call_id=observation.call_id, state_revision=observation.revision,
            )
            found.append(identifier)
        return found

    @staticmethod
    def _span_observed(identifier: str, origin: _Call, observation: Observation) -> bool:
        if isinstance(origin.data, AssessmentResult):
            return identifier in origin.data.terms.evidence_ids
        if isinstance(origin.data, ExcerptResult):
            excerpt = origin.data
            return (
                excerpt.evidence_id == identifier and observation.source_id == excerpt.document_id
                and observation.source_version == excerpt.document_version
                and observation.locator == canonical([
                    excerpt.span_id, excerpt.content_hash, excerpt.start_offset, excerpt.end_offset,
                ])
            )
        return False

    def query(self, call: _Call, subject: str, invoice_id: str | None = None) -> list[str]:
        # The empty-page fault identifies its query by tool name in the observation.
        subjects = [subject]
        if not call.data.items and call.data.next_cursor is not None:
            subjects.append(call.name)
        return [
            identifier for source_id in subjects
            for identifier in self.from_call(
                call, "query", subject=source_id, version=0, invoice_id=invoice_id,
            )
        ]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _missing(
    code: str, description: str, subject: str | None = None, evidence_ids: list[str] | None = None,
) -> MissingInformation:
    return MissingInformation(code=code, subject_id=subject, description=description, evidence_ids=evidence_ids or [])


def _action(
    kind: ActionType, invoice_id: str, status: ActionStatus, *,
    result_id: str | None = None, audit_id: str | None = None, confirmed: bool = False,
    reasons: Iterable[str] = (), evidence_ids: Iterable[str] = (),
) -> Action:
    return Action(
        type=kind, invoice_id=invoice_id, status=status, result_id=result_id, audit_id=audit_id,
        confirmed_by_readback=confirmed, reason_codes=list(dict.fromkeys(reasons)),
        evidence_ids=list(dict.fromkeys(evidence_ids)),
    )


class Ledger:
    def __init__(
        self, operation: str, customer_query: str | None = None, customer_id: str | None = None,
        invoice_ids: frozenset[str] | None = None,
    ) -> None:
        if operation not in {"list_invoices", "review_invoices", "escalate"}:
            raise ValueError("Unknown run operation")
        self.operation = operation
        self.customer_query = _normalize(customer_query) if customer_query else None
        self.customer_id = customer_id
        self.invoice_ids = invoice_ids
        self._calls: list[_Call] = []
        self._failures: dict[tuple[str, str, CommitArgs | None], _Failure] = {}
        self._failed_command: tuple[str, CommitArgs] | None = None
        self._sandbox_id: str | None = None
        self._observed_revision: int | None = None

    def record(self, name: str, args: dict | None, response: ToolResponse) -> None:
        self._failed_command = None
        definition = TOOLS.get(name)
        revision = response.meta.state_revision
        if (
            definition is None or name != response.meta.tool_name
            or (self._sandbox_id is not None and response.meta.sandbox_id != self._sandbox_id)
        ):
            return
        if not response.ok:
            # An authorized lookup can observe a newer revision even when its record is absent.
            if revision is not None and self._sandbox_id is not None:
                self._observed_revision = max(revision, self._observed_revision or 0)
            if name in ACTION_TYPES:
                try:
                    self._failed_command = (name, CommitArgs.model_validate(args))
                except ValidationError:
                    pass
            return
        if revision is None or not isinstance(response.data, definition.result):
            return
        arguments = definition.arguments.model_validate(args)
        self._sandbox_id = response.meta.sandbox_id
        self._observed_revision = max(revision, self._observed_revision or 0)
        self._calls.append(_Call(arguments, response.data, response.meta, revision))
        if isinstance(response.data, Receipt) and isinstance(arguments, CommitArgs):
            # A receipt for a different command cannot settle an uncertain earlier attempt.
            self._failures.pop((response.data.invoice_id, name, arguments), None)

    def record_failure(self, invoice_id: str, tool: str, *, uncertain: bool, reason: str) -> None:
        if tool not in ACTION_TYPES:
            return
        normalized = reason if reason.isascii() and reason.isupper() and reason.replace("_", "").isalnum() else None
        command = None
        if self._failed_command is not None and self._failed_command[0] == tool:
            command = self._failed_command[1]
        key = (invoice_id, tool, command)
        earlier = self._failures.get(key)
        self._failures[key] = _Failure(
            uncertain or (earlier is not None and earlier.uncertain),
            normalized or "ACTION_FAILED",
        )

    def _compatible_revisions(self) -> set[int]:
        if self._observed_revision is None:
            return set()
        # Ticket and approval creation leave customer/invoice membership, terms and balances intact.
        # Only a recorded write that advances the revision by one can bridge a revision boundary.
        own_changes = {
            call.revision for call in self._calls
            if isinstance(call.data, Receipt) and isinstance(call.args, CommitArgs)
            and call.data.state_revision == call.revision
            and call.args.expected_tenant_revision + 1 == call.revision
            and (call.data.replayed or call.data.creating_call_id == call.meta.call_id)
        }
        revisions = {self._observed_revision}
        revision = self._observed_revision
        while revision in own_changes and revision > 0:
            revision -= 1
            revisions.add(revision)
        return revisions

    def _identity(
        self, selected: str | None, current: list[_Call], evidence: _Evidence,
    ) -> tuple[str | None, MissingInformation | None]:
        if self.customer_id is not None:
            return self.customer_id, None
        code = "CUSTOMER_SEARCH_INCOMPLETE" if self.customer_query else "CUSTOMER_UNRESOLVED"
        chain = _Pages.from_calls(
            call for call in current if isinstance(call.args, CustomerSearchArgs)
            and self.customer_query is not None and _normalize(call.args.query) == self.customer_query
        )
        ids: list[str] = []
        if chain is not None and chain.next_cursor is None:
            for page in chain.calls:
                ids.extend(evidence.query(page, "customers"))
            if len(ids) != len(chain.calls):
                return None, _missing(code, REVIEW_MESSAGES[code], evidence_ids=ids)
            exact = {
                hit.id for page in chain.calls for hit in page.data.items
                if isinstance(hit, CustomerHit) and hit.match_kind == "exact"
                and _normalize(hit.legal_name) == self.customer_query
            }
            if len(exact) == 1:
                identity = next(iter(exact))
                if selected is None or selected == identity:
                    return identity, None
            code = "CUSTOMER_UNRESOLVED"
            if len(exact) > 1 or (not exact and sum(len(page.data.items) for page in chain.calls) > 1):
                code = "AMBIGUOUS_CUSTOMER"
            elif not exact and not any(page.data.items for page in chain.calls):
                code = "CUSTOMER_NOT_FOUND"
        return None, _missing(code, REVIEW_MESSAGES[code], evidence_ids=ids)

    def _coverage(
        self, customer_id: str | None, current: list[_Call], evidence: _Evidence,
    ) -> tuple[set[str], int | None, bool]:
        if customer_id is None:
            return set(), None, False
        calls = [
            call for call in current
            if isinstance(call.args, InvoiceSearchArgs) and call.args.customer_id == customer_id
        ]
        chain = _Pages.from_calls(call for call in calls if call.args.statuses is None)
        observed: set[str] = set()
        exhausted = False
        if chain is not None:
            exhausted = chain.next_cursor is None
            for page in chain.calls:
                if not evidence.query(page, customer_id):
                    exhausted = False
                    continue
                observed.update(
                    row.id for row in page.data.items
                    if isinstance(row, InvoiceResult) and row.customer_id == customer_id
                )
        if not exhausted or self.invoice_ids is not None:
            for page in calls:
                if page.args.statuses is not None and evidence.query(page, customer_id):
                    observed.update(
                        row.id for row in page.data.items
                        if isinstance(row, InvoiceResult) and row.customer_id == customer_id
                    )
            for call in current:
                if (
                    isinstance(call.data, InvoiceResult) and call.name == "invoice.get"
                    and call.data.customer_id == customer_id
                    and (self.invoice_ids is None or call.data.id in self.invoice_ids)
                    and evidence.from_call(
                        call, "invoice", subject=call.data.id, version=call.data.version, invoice_id=call.data.id,
                        locator="record",
                    )
                ):
                    observed.add(call.data.id)
        if self.invoice_ids is not None:
            if self.operation == "escalate" and customer_id == self.customer_id:
                # The action tools bind these observations to the host's authorized write scope.
                observed.update(
                    call.data.invoice_id for call in current
                    if isinstance(call.data, (PreviewResult, Receipt))
                )
            observed.intersection_update(self.invoice_ids)
        return observed, chain.revision if chain is not None else None, exhausted

    def _decisions(
        self, invoice_ids: set[str], current: list[_Call], business_date: str, evidence: _Evidence,
    ) -> dict[str, tuple[AssessmentResult, Decision]]:
        decisions = {}
        for call in reversed(current):
            data = call.data
            if (
                not isinstance(data, AssessmentResult) or data.invoice_id not in invoice_ids
                or data.invoice_id in decisions or data.business_date != business_date
                or call.args.invoice_id != data.invoice_id
            ):
                continue
            ids = evidence.from_call(
                call, "invoice_assessment", subject=data.invoice_id, version=data.invoice_version,
                invoice_id=data.invoice_id, ids=data.evidence_ids,
            )
            if not ids:
                continue
            stated_date_ids = evidence.from_call(
                call, "invoice", subject=data.invoice_id, version=data.invoice_version,
                invoice_id=data.invoice_id, ids=data.terms.evidence_ids, locator="stated_due_date",
            )
            ids.extend(stated_date_ids)
            ids.extend(evidence.from_call(call, "document_span", ids=data.terms.evidence_ids))
            decisions[data.invoice_id] = (
                data,
                Decision(
                    invoice_id=data.invoice_id, decision=data.status, reason_codes=data.reason_codes,
                    effective_due_date=data.effective_due_date,
                    stated_due_date=data.terms.stated_due_date if stated_date_ids else None,
                    stated_due_date_differs=data.terms.stated_due_date_differs if stated_date_ids else None,
                    days_past_due=data.days_past_due,
                    outstanding_minor=data.outstanding_minor, currency=data.currency, evidence_ids=ids,
                ),
            )
        return decisions

    def _receipt_actions(self, calls: list[_Call], evidence: _Evidence) -> list[Action]:
        actions = []
        seen = set()
        for index, call in enumerate(calls):
            receipt = call.data
            if not isinstance(receipt, Receipt) or call.name not in ACTION_TYPES:
                continue
            if self.invoice_ids is not None and receipt.invoice_id not in self.invoice_ids:
                continue
            key = (call.name, receipt.invoice_id, receipt.audit_id)
            if key in seen:
                continue
            seen.add(key)
            audit_ids = evidence.from_call(
                call, "audit_event", subject=receipt.audit_id, version=receipt.state_revision,
                invoice_id=receipt.invoice_id, locator=call.name,
            )
            read_type = TicketResult if call.name == "ticket.create" else ApprovalResult
            record_source = "ticket" if call.name == "ticket.create" else "approval"
            readback = next((
                later for later in reversed(calls[index + 1 :])
                if later.name == f"{record_source}.get" and isinstance(later.data, read_type)
                and later.data.id == receipt.result_id and later.data.invoice_id == receipt.invoice_id
                and later.data.creation_audit_id == receipt.audit_id
                and later.data.creating_call_id == receipt.creating_call_id
                and later.revision >= receipt.state_revision
            ), None)
            read_ids = (
                evidence.from_call(
                    readback, record_source, subject=receipt.result_id, version=readback.data.version,
                    invoice_id=receipt.invoice_id, locator="record",
                )
                if readback is not None else []
            )
            confirmed = (
                bool(audit_ids and readback and read_ids)
                and (receipt.replayed or receipt.creating_call_id == call.meta.call_id)
            )
            status: ActionStatus = "committed_unverified"
            reasons: list[str] = ["READBACK_NOT_CONFIRMED"]
            if confirmed:
                if isinstance(readback.data, TicketResult):
                    status, reasons = "completed", [readback.data.reason_code]
                elif readback.data.status == "pending":
                    status, reasons = "pending_approval", ["APPROVAL_PENDING"]
                elif readback.data.status == "approved":
                    status, reasons = "completed", ["APPROVAL_APPROVED"]
                else:
                    status, reasons = "blocked", [f"APPROVAL_{readback.data.status.upper()}"]
            actions.append(_action(
                ACTION_TYPES[call.name], receipt.invoice_id, status,
                result_id=receipt.result_id, audit_id=receipt.audit_id, confirmed=confirmed, reasons=reasons,
                evidence_ids=[*audit_ids, *read_ids] if confirmed else audit_ids,
            ))
        return actions

    def _existing_ticket(
        self, invoice_id: str, current: list[_Call], evidence: _Evidence,
    ) -> Action | None:
        # Positive observations suffice for existence. A later observation of the same record
        # or a later exhausted query that covers both active states can supersede one.
        active_filter = ACTIVE_TICKET_STATES
        search = _Pages.from_calls(
            call for call in current if call.name == "ticket.search" and call.args.invoice_id == invoice_id
            and (call.args.statuses is None or set(call.args.statuses) == active_filter)
        )
        negative_calls = {
            page.meta.call_id for page in search.calls
        } if (
            search is not None and search.next_cursor is None
            and not any(
                row.invoice_id == invoice_id and row.status in active_filter
                for page in search.calls for row in page.data.items
            )
        ) else set()
        for call in reversed(current):
            data = call.data
            if call.meta.call_id in negative_calls:
                return None
            if call.name == "ticket.search" and call.args.invoice_id == invoice_id:
                for row in data.items:
                    if row.invoice_id != invoice_id or row.status not in active_filter:
                        continue
                    ids = evidence.query(call, invoice_id, invoice_id=invoice_id)
                    if ids:
                        return _action(
                            ACTION_TYPES["ticket.create"], invoice_id, "already_active", result_id=row.id,
                            reasons=["ACTIVE_TICKET_EXISTS"], evidence_ids=ids,
                        )
            if (
                isinstance(data, PreviewResult) and data.invoice_id == invoice_id
                and data.decision == "already_active" and data.existing_ticket_id is not None
            ):
                return _action(
                    ACTION_TYPES["ticket.create"], invoice_id, "already_active", result_id=data.existing_ticket_id,
                    reasons=["ACTIVE_TICKET_EXISTS"],
                )
        return None

    def _assessment_actions(
        self, decisions: dict[str, tuple[AssessmentResult, Decision]], current: list[_Call],
        existing: list[Action], evidence: _Evidence,
    ) -> list[Action]:
        actions = []
        for invoice_id, (assessment, decision) in sorted(decisions.items()):
            recorded = [action for action in existing if action.invoice_id == invoice_id]
            if any(
                action.status in {"commit_unknown", "committed_unverified", "pending_approval"}
                or (action.type == ACTION_TYPES["ticket.create"] and action.status == "completed")
                for action in recorded
            ):
                continue
            matching = self._existing_ticket(invoice_id, current, evidence)
            if assessment.status == "eligible" and matching is not None:
                actions.append(matching)
                continue
            if any(action.status == "blocked" for action in recorded):
                continue
            if assessment.status == "eligible":
                approval_granted = any(
                    action.type == ACTION_TYPES["approval.request"] and action.status == "completed"
                    for action in recorded
                )
                approval_required = assessment.requires_approval and not approval_granted
                kind = ACTION_TYPES["approval.request" if approval_required else "ticket.create"]
                actions.append(_action(
                    kind, invoice_id, "not_attempted", reasons=["ACTION_NOT_ATTEMPTED"],
                    evidence_ids=decision.evidence_ids,
                ))
            elif any(
                isinstance(call.data, PreviewResult) and call.data.invoice_id == invoice_id
                and call.data.decision in {"blocked", "needs_review"}
                for call in current
            ):
                actions.append(_action(
                    ACTION_TYPES["ticket.create"], invoice_id, "blocked", reasons=decision.reason_codes,
                    evidence_ids=decision.evidence_ids,
                ))
        return actions

    def finish(
        self, *, run_id: str, task_id: str, as_of: str, business_date: str,
        lookup: Callable[[str], Observation | None], selected_customer_id: str | None,
        review_reason: str | None, stop_reason: str,
    ) -> RunOutput:
        evidence = _Evidence(self._calls, lookup)
        compatible = self._compatible_revisions()
        current = [call for call in self._calls if call.revision in compatible]
        customer_id, identity_missing = self._identity(selected_customer_id, current, evidence)
        observed, revision, exhausted = self._coverage(customer_id, current, evidence)
        decisions = self._decisions(observed, current, business_date, evidence)
        expected = self.invoice_ids if self.invoice_ids is not None else observed
        complete_set = customer_id is not None and (
            expected <= observed if self.invoice_ids is not None else exhausted
        )
        pending = set(expected - observed)
        missing: list[MissingInformation] = []
        requires_review = False
        useful = bool(observed) if self.operation == "list_invoices" else False

        if identity_missing is not None:
            missing.append(identity_missing)
            requires_review = identity_missing.code != "CUSTOMER_SEARCH_INCOMPLETE"
        elif not complete_set:
            missing.append(_missing(
                "INVOICE_SEARCH_INCOMPLETE",
                "Invoice coverage is incomplete for the selected customer.", customer_id,
            ))

        if self.operation != "list_invoices":
            unassessed = sorted(observed - decisions.keys())
            pending.update(unassessed)
            for invoice_id in unassessed:
                missing.append(_missing(
                    "ASSESSMENT_MISSING", "A current invoice assessment is required.", invoice_id,
                ))
            for invoice_id, (_, decision) in sorted(decisions.items()):
                if decision.decision == "needs_review":
                    missing.append(_missing(
                        "ASSESSMENT_NEEDS_REVIEW", "The assessment requires human review.",
                        invoice_id, decision.evidence_ids,
                    ))
                    pending.add(invoice_id)
                    requires_review = True
                elif self.operation == "review_invoices" or decision.decision == "ineligible":
                    useful = True

        actions = self._receipt_actions(self._calls, evidence)
        action_pairs = {(action.invoice_id, action.type) for action in actions}
        for (invoice_id, tool, _), failure in self._failures.items():
            kind = ACTION_TYPES[tool]
            if self.invoice_ids is not None and invoice_id not in self.invoice_ids:
                continue
            if (invoice_id, kind) not in action_pairs or failure.uncertain:
                actions.append(_action(
                    kind, invoice_id, "commit_unknown" if failure.uncertain else "blocked",
                    reasons=[failure.reason],
                ))
        if self.operation == "escalate":
            actions.extend(self._assessment_actions(decisions, current, actions, evidence))

        unresolved_mutation = False
        fulfilled = {
            action.invoice_id for action in actions if action.type == ACTION_TYPES["ticket.create"]
            and action.status in {"completed", "already_active"}
        }
        for action in actions:
            if action.status in {"completed", "already_active"}:
                useful = True
            if action.status in {"commit_unknown", "committed_unverified"}:
                unresolved_mutation = True
                requires_review = True
            if action.status not in INCOMPLETE_ACTIONS:
                continue
            if (
                action.status not in {"commit_unknown", "committed_unverified"}
                and (action.invoice_id in fulfilled or (self.operation != "escalate" and action.status == "blocked"))
            ):
                continue
            decision = decisions.get(action.invoice_id)
            if (
                action.status == "blocked" and decision is not None
                and decision[1].decision == "ineligible" and not action.confirmed_by_readback
            ):
                continue
            pending.add(action.invoice_id)
            code, description = {
                "pending_approval": ("APPROVAL_PENDING", "The verified approval request awaits a decision."),
                "blocked": ("ACTION_BLOCKED", "The requested action could not proceed."),
                "not_attempted": ("ACTION_NOT_ATTEMPTED", "The required action has not been attempted."),
                "committed_unverified": ("READBACK_NOT_CONFIRMED", "The committed action needs matching read-back."),
                "commit_unknown": ("COMMIT_OUTCOME_UNKNOWN", "The mutation outcome could not be confirmed."),
            }[action.status]
            missing.append(_missing(code, description, action.invoice_id, action.evidence_ids))
            if action.status == "pending_approval":
                requires_review = True

        complete = complete_set and not pending and not unresolved_mutation
        if not complete and review_reason in ALLOWED_REVIEW_REQUESTS and not any(
            item.code == review_reason for item in missing
        ):
            missing.append(_missing(review_reason, REVIEW_MESSAGES[review_reason], customer_id))
            requires_review = True

        if unresolved_mutation:
            status = "partial"
        elif complete:
            status = "resolved"
        elif useful or (observed and not requires_review):
            status = "partial"
        elif requires_review:
            status = "needs_review"
        else:
            status = "failed"

        if stop_reason in {"tool_budget", "model_budget", "token_budget", "deadline"} and not complete:
            missing.append(_missing("RUN_BUDGET_EXHAUSTED", "The run reached its execution limit."))
        elif status == "failed" and not missing:
            missing.append(_missing("RUN_INCOMPLETE", "The run stopped before completing the request."))
        summary = {
            "resolved": "The requested work is complete.",
            "partial": "The run produced results with work still open.",
            "needs_review": "The request requires human review before it can be completed.",
            "failed": "The run ended before it could complete the requested work.",
        }[status]
        return RunOutput(
            run_id=run_id, task_id=task_id, as_of=as_of, business_date=business_date,
            status=status, summary=summary, review_required=requires_review,
            coverage=Coverage(
                customer_id=customer_id, state_revision=revision, invoice_search_exhausted=exhausted,
                observed_invoice_ids=sorted(observed), pending_invoice_ids=sorted(pending),
            ),
            decisions=[decisions[key][1] for key in sorted(decisions)],
            actions=sorted(actions, key=lambda action: (action.invoice_id, action.type, action.result_id or "")),
            missing_information=missing, evidence=[evidence.entries[key] for key in sorted(evidence.entries)],
        )
