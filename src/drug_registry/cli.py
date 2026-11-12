from __future__ import annotations
import argparse
from .pipeline import DrugRegistryPipeline
from .sources import JsonNoticeSource
from .storage import DrugStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="drug-registry.sqlite3")
    args = parser.parse_args()
    store = DrugStore(args.database)
    try:
        changes = DrugRegistryPipeline(JsonNoticeSource(args.fixture), store).run()
        print({"changes": len(changes), "database": args.database})
    finally:
        store.close()


if __name__ == "__main__":
    main()
