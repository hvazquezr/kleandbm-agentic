"""Record types, enums, and payload schemas for model elements and companion registers.

Each record type has a payload schema describing one revision's content. Fields that point
at other records are listed in ``REFERENCE_FIELDS`` so the changeset service can resolve and
check them.
"""

import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model


class RecordType(enum.StrEnum):
    # Model elements (belong to one data model)
    TABLE = "table"
    ATTRIBUTE = "attribute"
    RELATIONSHIP = "relationship"
    # Companion registers (project level)
    REQUIREMENT = "requirement"
    DEFINITION = "definition"
    QUESTION = "question"
    DECISION = "decision"
    # Validation findings (belong to one data model)
    CHECK = "check"


MODEL_SCOPED_TYPES = frozenset(
    {RecordType.TABLE, RecordType.ATTRIBUTE, RecordType.RELATIONSHIP, RecordType.CHECK}
)

# Human-readable register identifiers, e.g. REQ-001.
CODE_PREFIXES = {
    RecordType.REQUIREMENT: "REQ",
    RecordType.DEFINITION: "DEF",
    RecordType.QUESTION: "Q",
    RecordType.DECISION: "DEC",
    RecordType.CHECK: "CHK",
}


# --- Enums ----------------------------------------------------------------------------------


class ModelState(enum.StrEnum):
    """Lifecycle states (spec §4)."""

    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    ACCEPTED = "accepted"


class TableKind(enum.StrEnum):
    FACT = "fact"
    DIMENSION = "dimension"
    BRIDGE = "bridge"


class ScdType(enum.StrEnum):
    NONE = "none"
    TYPE_1 = "type_1"
    TYPE_2 = "type_2"


class LogicalType(enum.StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"


class AttributeRole(enum.StrEnum):
    SURROGATE_KEY = "surrogate_key"
    NATURAL_KEY = "natural_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DEGENERATE_DIMENSION = "degenerate_dimension"
    DESCRIPTIVE = "descriptive"
    SCD_EFFECTIVE_FROM = "scd_effective_from"
    SCD_EFFECTIVE_TO = "scd_effective_to"
    SCD_CURRENT_FLAG = "scd_current_flag"


class Additivity(enum.StrEnum):
    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"


class Cardinality(enum.StrEnum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_ONE = "one_to_one"
    MANY_TO_MANY = "many_to_many"


class OriginKind(enum.StrEnum):
    USER_STATEMENT = "user_statement"
    DASHBOARD = "dashboard"
    REPORT = "report"
    SPREADSHEET = "spreadsheet"
    DOCUMENT = "document"
    AGENT_INFERENCE = "agent_inference"
    OTHER = "other"


class ConfirmationStatus(enum.StrEnum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FeasibilityStatus(enum.StrEnum):
    NOT_ASSESSED = "not_assessed"
    FULLY_SUPPORTED = "fully_supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"


class DefinitionKind(enum.StrEnum):
    TERM = "term"
    METRIC = "metric"
    CONCEPT = "concept"


class DefinitionProvenance(enum.StrEnum):
    """Where the meaning came from. Distinct from whether a user has confirmed it."""

    DOCUMENTED = "documented"
    USER_STATED = "user_stated"
    INFERRED = "inferred"


class QuestionKind(enum.StrEnum):
    CLARIFICATION = "clarification"
    CONTRADICTION = "contradiction"
    MISSING_INFORMATION = "missing_information"
    VALIDATION_FINDING = "validation_finding"
    # A decision-point search ended without a selected design; a person accepts or rejects
    # the design as it stands (spec §7).
    DECISION_POINT = "decision_point"


class QuestionStatus(enum.StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    DEFERRED = "deferred"


class DecisionKind(enum.StrEnum):
    DECISION = "decision"
    ASSUMPTION = "assumption"


class DecisionStatus(enum.StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class CheckType(enum.StrEnum):
    COVERAGE = "coverage"
    GRAIN = "grain"
    KEYS_AND_JOINS = "keys_and_joins"
    MEASURE_DUPLICATION = "measure_duplication"
    DEFINITION_CONSISTENCY = "definition_consistency"
    CONVENTIONS = "conventions"
    ASSUMPTIONS = "assumptions"


class CheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class LinkType(enum.StrEnum):
    USES_DEFINITION = "uses_definition"  # requirement/element/check -> definition
    MAPS_TO = "maps_to"  # requirement -> model element
    ALTERNATIVE_FOR = "alternative_for"  # proposed requirement -> original requirement
    CONFLICTS_WITH = "conflicts_with"  # definition <-> definition, requirement <-> requirement
    CONCERNS = "concerns"  # question -> any record
    AFFECTS = "affects"  # decision -> any record
    CHECKS = "checks"  # validation check -> any record


_ELEMENTS = frozenset({RecordType.TABLE, RecordType.ATTRIBUTE, RecordType.RELATIONSHIP})
_ANY = frozenset(RecordType)

# Allowed (from types, to types) for each link type.
LINK_RULES: dict[LinkType, tuple[frozenset[RecordType], frozenset[RecordType]]] = {
    LinkType.USES_DEFINITION: (
        _ELEMENTS | {RecordType.REQUIREMENT, RecordType.CHECK},
        frozenset({RecordType.DEFINITION}),
    ),
    LinkType.MAPS_TO: (frozenset({RecordType.REQUIREMENT}), _ELEMENTS),
    LinkType.ALTERNATIVE_FOR: (
        frozenset({RecordType.REQUIREMENT}),
        frozenset({RecordType.REQUIREMENT}),
    ),
    LinkType.CONFLICTS_WITH: (
        frozenset({RecordType.DEFINITION, RecordType.REQUIREMENT}),
        frozenset({RecordType.DEFINITION, RecordType.REQUIREMENT}),
    ),
    LinkType.CONCERNS: (frozenset({RecordType.QUESTION}), _ANY),
    LinkType.AFFECTS: (frozenset({RecordType.DECISION}), _ANY),
    LinkType.CHECKS: (frozenset({RecordType.CHECK}), _ANY),
}


# --- Payloads -------------------------------------------------------------------------------


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class TableData(_Payload):
    name: str = Field(min_length=1, max_length=255)
    kind: TableKind
    grain: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=4000)
    scd_type: ScdType = ScdType.NONE


class AttributeData(_Payload):
    table_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    logical_type: LogicalType
    role: AttributeRole = AttributeRole.DESCRIPTIVE
    is_nullable: bool = True
    additivity: Additivity | None = None
    description: str | None = Field(default=None, max_length=4000)
    ordinal: int = 0


class RelationshipData(_Payload):
    from_table_id: uuid.UUID
    from_attribute_id: uuid.UUID
    to_table_id: uuid.UUID
    to_attribute_id: uuid.UUID
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    role_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=4000)


class RequirementData(_Payload):
    statement: str = Field(min_length=1, max_length=4000)
    origin_kind: OriginKind
    origin_ref: str | None = Field(default=None, max_length=2000)
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED
    feasibility_status: FeasibilityStatus = FeasibilityStatus.NOT_ASSESSED
    feasibility_notes: str | None = Field(default=None, max_length=4000)
    # A proposed alternative capability is not an agreed requirement until confirmed (spec §5).
    is_proposed_alternative: bool = False


class MetricDetails(_Payload):
    """Unknown details stay None, which means explicitly unresolved (spec §3)."""

    formula: str | None = None
    inclusions: str | None = None
    exclusions: str | None = None
    grain: str | None = None
    time_basis: str | None = None
    aggregation: str | None = None


class DefinitionData(_Payload):
    term: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list)
    kind: DefinitionKind = DefinitionKind.TERM
    definition: str | None = Field(default=None, max_length=8000)
    business_scope: str | None = Field(default=None, max_length=2000)
    metric: MetricDetails | None = None
    provenance: DefinitionProvenance
    origin_ref: str | None = Field(default=None, max_length=2000)
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED


class QuestionData(_Payload):
    kind: QuestionKind
    description: str = Field(min_length=1, max_length=8000)
    impact: str | None = Field(default=None, max_length=4000)
    is_blocking: bool = False
    proposed_action: str | None = Field(default=None, max_length=4000)
    status: QuestionStatus = QuestionStatus.OPEN
    resolution: str | None = Field(default=None, max_length=8000)


class DecisionData(_Payload):
    kind: DecisionKind
    statement: str = Field(min_length=1, max_length=4000)
    rationale: str | None = Field(default=None, max_length=8000)
    alternatives: list[str] = Field(default_factory=list)
    status: DecisionStatus = DecisionStatus.PROPOSED
    # What the decision rests on: [{"kind": "source", "evidence_id", "tool"} | {"kind": "document", "citation"}].
    # An agent's decision about model elements needs some; otherwise it's recorded as an assumption.
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class CheckData(_Payload):
    check_type: CheckType
    status: CheckStatus
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    impact: str | None = Field(default=None, max_length=4000)
    recommended_action: str | None = Field(default=None, max_length=4000)
    # The changeset sequence the check was evaluated against (its dependency watermark).
    based_on_seq: int | None = None
    # A stable name for the issue (e.g. "grain:fact_sales"), so correction attempts are counted
    # per issue across rounds and episodes; renaming an issue doesn't restart its allowance.
    issue_key: str | None = Field(default=None, max_length=200)
    # A blocking finding that failed (or couldn't be verified) keeps the model out of Ready for Review.
    is_blocking: bool = False
    attempts: int = Field(default=0, ge=0)  # correction attempts made for this issue so far
    candidate_id: uuid.UUID | None = None  # the design candidate that was reviewed


PAYLOADS: dict[RecordType, type[_Payload]] = {
    RecordType.TABLE: TableData,
    RecordType.ATTRIBUTE: AttributeData,
    RecordType.RELATIONSHIP: RelationshipData,
    RecordType.REQUIREMENT: RequirementData,
    RecordType.DEFINITION: DefinitionData,
    RecordType.QUESTION: QuestionData,
    RecordType.DECISION: DecisionData,
    RecordType.CHECK: CheckData,
}

# Fields holding record ids, with the record types they must point at.
REFERENCE_FIELDS: dict[RecordType, dict[str, RecordType]] = {
    RecordType.ATTRIBUTE: {"table_id": RecordType.TABLE},
    RecordType.RELATIONSHIP: {
        "from_table_id": RecordType.TABLE,
        "from_attribute_id": RecordType.ATTRIBUTE,
        "to_table_id": RecordType.TABLE,
        "to_attribute_id": RecordType.ATTRIBUTE,
    },
}


def partial_of(model: type[BaseModel]) -> type[BaseModel]:
    """A copy of ``model`` with every field optional, for patch bodies."""
    fields = {name: (f.annotation | None, None) for name, f in model.model_fields.items()}
    return create_model(f"{model.__name__}Patch", __config__=ConfigDict(extra="forbid"), **fields)


PATCHES: dict[RecordType, type[BaseModel]] = {t: partial_of(m) for t, m in PAYLOADS.items()}
