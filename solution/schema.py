"""Workflow schema and disposition catalogue loaders."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

VALID_FIELD_TYPES = {"string", "phone", "date", "decimal", "enum"}


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single column/field in a workflow schema."""
    name: str
    type: str
    required: bool = False
    pii: bool = False
    aliases: list[str] = field(default_factory=list)
    reference: bool = False
    values: list[str] = field(default_factory=list)
    default: str | None = None

    def __post_init__(self) -> None:
        if self.type not in VALID_FIELD_TYPES:
            raise ValueError(f"Invalid field type {self.type!r} for field {self.name!r}")


@dataclass(frozen=True)
class WorkflowSchema:
    """Schema for a workflow containing its fields and reference field."""
    workflow_id: str
    description: str
    fields: list[FieldDef]
    reference_field: FieldDef
    field_by_name: dict[str, FieldDef]


@dataclass(frozen=True)
class DispositionCatalog:
    """Lookup table for workflow dispositions: (workflow_id, code) -> (is_terminal, is_success)."""
    lookup: dict[tuple[str, str], tuple[bool, bool]]

    def get(self, workflow_id: str, code: str) -> tuple[bool, bool] | None:
        """Return (is_terminal, is_success) if known, else None."""
        return self.lookup.get((workflow_id, code))

    def is_known(self, workflow_id: str, code: str) -> bool:
        """Check if disposition code is known for this workflow (case-sensitive exact)."""
        return (workflow_id, code) in self.lookup


def load_workflow_schema(schema_file: Path) -> WorkflowSchema:
    """Load and validate a single workflow schema JSON file."""
    with open(schema_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    workflow_id = data["workflow_id"]
    description = data.get("description", "")
    fields_raw = data.get("fields", [])

    fields: list[FieldDef] = []
    reference_fields: list[FieldDef] = []

    for item in fields_raw:
        fdef = FieldDef(
            name=item["name"],
            type=item["type"],
            required=bool(item.get("required", False)),
            pii=bool(item.get("pii", False)),
            aliases=list(item.get("aliases", [])),
            reference=bool(item.get("reference", False)),
            values=list(item.get("values", [])),
            default=item.get("default"),
        )
        fields.append(fdef)
        if fdef.reference:
            reference_fields.append(fdef)

    if len(reference_fields) != 1:
        raise ValueError(
            f"Schema {workflow_id} must have exactly one field with reference=true, "
            f"found {len(reference_fields)}"
        )

    ref_field = reference_fields[0]
    if ref_field.pii:
        raise ValueError(
            f"Schema {workflow_id} reference field {ref_field.name!r} cannot be marked PII"
        )

    field_by_name = {f.name: f for f in fields}
    return WorkflowSchema(
        workflow_id=workflow_id,
        description=description,
        fields=fields,
        reference_field=ref_field,
        field_by_name=field_by_name,
    )


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    """Load all workflow schemas (*.json) from schema_dir."""
    schemas: dict[str, WorkflowSchema] = {}
    for p in sorted(schema_dir.glob("*.json")):
        schema = load_workflow_schema(p)
        schemas[schema.workflow_id] = schema
    return schemas


def load_dispositions(dispositions_csv: Path) -> DispositionCatalog:
    """Load dispositions.csv into a DispositionCatalog lookup."""
    lookup: dict[tuple[str, str], tuple[bool, bool]] = {}
    with open(dispositions_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wf = row["workflow_id"].strip()
            code = row["code"].strip()
            is_terminal = row["is_terminal"].strip().lower() == "true"
            is_success = row["is_success"].strip().lower() == "true"
            lookup[(wf, code)] = (is_terminal, is_success)
    return DispositionCatalog(lookup=lookup)

