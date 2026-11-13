"""Write the API's OpenAPI document to a file (used to generate the frontend client).

Usage: python scripts/export_openapi.py <output.json>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    spec = create_app().openapi()
    Path(sys.argv[1]).write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {sys.argv[1]} ({len(spec['paths'])} paths)")
