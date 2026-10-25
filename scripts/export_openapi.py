import argparse
import json
from pathlib import Path

from yureatlas.api import app

OUTPUT = Path(__file__).resolve().parents[1] / "docs/openapi.json"


def main():
    parser = argparse.ArgumentParser(description="Generate or check the API contract without a database")
    parser.add_argument("--check", action="store_true", help="Fail on drift without modifying the contract")
    args = parser.parse_args()
    generated = json.dumps(app.openapi(), indent=2) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != generated:
            raise SystemExit("OpenAPI contract is stale. Run make contract and review the resulting diff.")
        print("OpenAPI contract is current.")
    else:
        OUTPUT.write_text(generated)


if __name__ == "__main__":
    main()
