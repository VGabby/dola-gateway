"""Safe, credential-free account inventory for local Dola profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class AccountEntry:
    name: str
    email: str = ""
    enabled: bool = True
    note: str = ""


def load_accounts(path: str | Path) -> list[AccountEntry]:
    """Load account labels only. Passwords, cookies, and TOTP are forbidden."""
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    rows = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValueError("account config must contain an 'accounts' list")

    accounts: list[AccountEntry] = []
    seen: set[str] = set()
    forbidden = {"password", "passwd", "totp", "totp_secret", "cookie", "cookies", "sessionid"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"accounts[{index}] must be an object")
        unsafe = forbidden.intersection(k.lower() for k in row)
        if unsafe:
            raise ValueError(
                f"accounts[{index}] contains credential fields: {', '.join(sorted(unsafe))}"
            )
        name = str(row.get("name", "")).strip()
        if not ACCOUNT_NAME_RE.fullmatch(name):
            raise ValueError(f"accounts[{index}].name is invalid")
        if name in seen:
            raise ValueError(f"duplicate account name: {name}")
        seen.add(name)
        accounts.append(AccountEntry(
            name=name,
            email=str(row.get("email", "")).strip(),
            enabled=bool(row.get("enabled", True)),
            note=str(row.get("note", "")).strip(),
        ))
    return accounts
