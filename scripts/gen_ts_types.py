"""Generate web-next/types/api.ts from shared/types.py.

Run: python3.12 scripts/gen_ts_types.py
  or .venv/bin/python3 scripts/gen_ts_types.py (if pip is available in venv)

Requires: pydantic-to-typescript (install via: pip install pydantic-to-typescript==2.0.0)
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "web-next" / "types" / "api.ts"


def main() -> None:
    # Ensure shared/ is importable
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    try:
        from pydantic2ts import generate_typescript_defs
    except ImportError:
        print(
            "ERROR: pydantic-to-typescript not installed.\n"
            "Run: pip install pydantic-to-typescript==2.0.0",
            file=sys.stderr,
        )
        sys.exit(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)

    generate_typescript_defs(
        "shared.types",
        str(OUT),
        json2ts_cmd="npx --yes json-schema-to-typescript",
    )

    line_count = len(OUT.read_text().splitlines())
    print(f"Wrote {OUT} ({line_count} lines)")


if __name__ == "__main__":
    main()
