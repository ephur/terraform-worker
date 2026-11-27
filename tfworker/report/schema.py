"""JSON Schema export for the report models, for tools that don't use Python."""

import json
import sys
from pathlib import Path
from typing import Dict, Optional

from .models import RUN_SCHEMA, STATUS_SCHEMA, DefinitionStatus, RunRecord

SCHEMAS = {RUN_SCHEMA: RunRecord, STATUS_SCHEMA: DefinitionStatus}


def json_schemas() -> Dict[str, dict]:
    """Return the JSON Schema of each record type, keyed by schema id."""
    out = {}
    for schema_id, model in SCHEMAS.items():
        schema = model.model_json_schema(by_alias=True, mode="serialization")
        schema["$id"] = schema_id
        out[schema_id] = schema
    return out


def file_name(schema_id: str) -> str:
    return schema_id.replace("tfworker.", "").replace("/", "-") + ".schema.json"


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for schema_id, schema in json_schemas().items():
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        (directory / file_name(schema_id)).write_text(text)


def main(argv: Optional[list] = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    write_schemas(Path(args[0] if args else "."))


if __name__ == "__main__":  # pragma: no cover
    main()
