"""Secret lookup by name.

On GitHub Actions the workflow passes every repository secret as one JSON env var
(SECRETS_JSON = toJSON(secrets)), so a new brand only needs new secrets, never a
workflow edit. Locally, plain environment variables with the same names work too.
Values are never logged.
"""

from __future__ import annotations

import json
import os


class MissingSecret(Exception):
    def __init__(self, name: str):
        super().__init__(f"secret {name} is not set (add it under GitHub → Settings → Secrets → Actions)")
        self.name = name


class Secrets:
    def __init__(self, values: dict[str, str] | None = None):
        if values is None:
            values = {}
            raw = os.environ.get("SECRETS_JSON")
            if raw:
                try:
                    values = {k: v for k, v in json.loads(raw).items() if isinstance(v, str)}
                except json.JSONDecodeError as exc:
                    raise RuntimeError("SECRETS_JSON is not valid JSON") from exc
        self._values = values

    def has(self, name: str) -> bool:
        return bool(self._values.get(name) or os.environ.get(name))

    def get(self, name: str) -> str:
        value = self._values.get(name) or os.environ.get(name)
        if not value:
            raise MissingSecret(name)
        return value

    def __repr__(self) -> str:  # never print values
        return f"Secrets({len(self._values)} values)"
