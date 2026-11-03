"""CLI report display and an actually usable evidence inspection command."""

import argparse
import json
import shlex
import sys
from pathlib import Path

from mini.analysis.store import EvidenceStore


def show_report(report, session, console):
    console.print(f"分析：{report['status']} · {report['report_id']}")
    for i, claim in enumerate(report["claims"], 1):
        console.print(
            f"{i}. [{claim['kind']}/{claim['verification_status']}] {claim['text']}"
        )
        console.print("   证据：" + ", ".join(claim["evidence_refs"]))
        for limitation in claim["limitations"]:
            console.print("   限制：" + limitation)
    for limitation in report["limitations"] + report["validation_errors"]:
        console.print("限制/校验：" + limitation)
    console.print(
        "查看证据："
        + shlex.join(
            [
                sys.executable,
                "-m",
                "mini.analysis.reporting",
                "--session",
                session,
                "--id",
                "EVIDENCE_ID",
            ]
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Inspect saved analysis evidence, datasets and artifacts"
    )
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--session", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--artifact", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prune-days", type=int)
    args = parser.parse_args()
    root = args.store
    if root is None:
        from mini.runtime.resources import CONFIG_PATH, _read_config

        root = (
            Path(
                (_read_config(CONFIG_PATH).get("analysis") or {}).get(
                    "evidence_dir", "var/analysis"
                )
            )
            if CONFIG_PATH.is_file()
            else Path("var/analysis")
        )
        if not root.is_absolute():
            root = CONFIG_PATH.parent / root
    store = EvidenceStore(root)
    if args.prune_days:
        store.prune(args.prune_days)
    if args.artifact:
        meta, content = store.read_artifact(args.session, args.id)
        if args.output:
            args.output.write_bytes(content)
        elif meta["media_type"] in ("text/plain", "application/json"):
            print(content.decode())
        else:
            print(json.dumps(meta, ensure_ascii=False))
            print("使用 --output 保存二进制产物")
    else:
        record = store.get(args.session, args.id)
        if "report_id" in record:
            from mini.analysis.validation import read_report

            record = read_report(store, args.session, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
