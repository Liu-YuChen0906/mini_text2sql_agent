"""Container-only data/result protocol. This is not a security boundary."""

import json
from pathlib import Path

import pandas as pd

INPUT = Path("/input")
OUTPUT = Path("/output")


def load_dataset(identity):
    manifest = json.loads((INPUT / "manifest.json").read_text())
    if identity not in manifest:
        raise ValueError("dataset_not_authorized")
    data = json.loads((INPUT / manifest[identity]).read_text())
    frame = pd.DataFrame(data["rows"], columns=data["columns"])
    for col, kind in data.get("column_types", {}).items():
        if kind == "Decimal":
            from decimal import Decimal

            frame[col] = frame[col].map(lambda v: Decimal(v) if v is not None else None)
        elif kind in ("date", "datetime"):
            frame[col] = pd.to_datetime(frame[col])
        elif kind == "bytes":
            frame[col] = frame[col].map(
                lambda v: bytes.fromhex(v[2:]) if v is not None else None
            )
    return frame


def emit_result(result):
    if not isinstance(result, dict):
        raise TypeError("emit_result_requires_dict")
    (OUTPUT / "result.json").write_text(json.dumps(result, allow_nan=False))


def _name(name, suffix):
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name in (".", "..")
    ):
        raise ValueError("invalid_logical_name")
    return OUTPUT / (name if name.endswith(suffix) else name + suffix)


def save_dataset(df, name, units=None):
    target = _name(name, ".dataset.json")
    # Split orientation preserves columns even for an empty frame.
    data = json.loads(df.to_json(orient="split", date_format="iso"))
    target.write_text(
        json.dumps(
            {"columns": data["columns"], "rows": data["data"], "units": units or {}},
            allow_nan=False,
        )
    )


def save_figure(fig, name):
    fig.savefig(_name(name, ".png"), format="png", dpi=120)
