from __future__ import annotations
import argparse

from .matching import DEFAULT_RULE_VERSION, available_rule_versions
from .pipeline import DrugRegistryPipeline
from .sources import JsonNoticeSource
from .storage import DrugStore


def main() -> None:
    parser = argparse.ArgumentParser(description="药品说明书版本变更导入与章节匹配")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="drug-registry.sqlite3")
    parser.add_argument(
        "--rule-version",
        default=DEFAULT_RULE_VERSION,
        choices=available_rule_versions(),
        help="章节匹配规则版本，结论会随版本号留痕，可切换后重跑对照",
    )
    args = parser.parse_args()
    store = DrugStore(args.database)
    try:
        results = DrugRegistryPipeline(
            JsonNoticeSource(args.fixture), store, rule_version=args.rule_version
        ).run()
        summary = {
            "rule_version": args.rule_version,
            "pairs": len(results),
            "changes": sum(len(item.changes) for item in results),
            "candidates": sum(
                1 for item in results for group in item.groups if group.status == "candidate"
            ),
            "database": args.database,
        }
        print(summary)
    finally:
        store.close()


if __name__ == "__main__":
    main()
