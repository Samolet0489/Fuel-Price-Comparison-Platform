"""Publish a quote snapshot atomically for the local worker."""
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_snapshot(path, source, quotes):
    data = {
        "source": source,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "quotes": [quote.model_dump(mode="json") for quote in quotes],
    }
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary_path = Path(output.name)
        try:
            json.dump(data, output)
            output.close()
            # The worker reads this file as an unprivileged container user.
            temporary_path.chmod(0o644)
            temporary_path.replace(path)
        finally:
            output.close()
            temporary_path.unlink(missing_ok=True)
