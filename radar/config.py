"""Local configuration; never execute env files as shell scripts."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def load_config_file(path: Path | None = None):
    path = path or ROOT / ".env.phase1"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not (key.startswith("RADAR_") or key.startswith("POSTGRES_")):
            raise ValueError("Invalid Phase 1 configuration file")
        if value[:1] in {"'", '"'}:
            if value[-1:] != value[:1] or len(value) < 2:
                raise ValueError("Invalid Phase 1 configuration quoting")
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    username: str
    password: str
    host: str = "127.0.0.1"
    port: int = 8000
    interval_seconds: int = 0
    lookback_hours: int = 72
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")

    @classmethod
    def from_env(cls):
        load_config_file()
        dsn = os.environ.get("RADAR_DATABASE_URL", "")
        user = os.environ.get("RADAR_USERNAME", "owner")
        password = os.environ.get("RADAR_PASSWORD", "")
        if not dsn.startswith(("postgresql://", "postgres://")) or not user or len(password) < 12:
            raise ValueError("Configure RADAR_DATABASE_URL and RADAR_PASSWORD (at least 12 characters) in .env.phase1")
        port = int(os.environ.get("RADAR_PORT", "8000"))
        interval = int(os.environ.get("RADAR_INTERVAL_SECONDS", "0"))
        lookback = int(os.environ.get("RADAR_LOOKBACK_HOURS", "72"))
        if not 1 <= port <= 65535 or not (interval == 0 or interval >= 900) or not 1 <= lookback <= 720:
            raise ValueError("Invalid port, interval (0 or >=900 seconds), or lookback (1..720 hours)")
        hosts = tuple(x.strip() for x in os.environ.get("RADAR_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",") if x.strip())
        return cls(dsn, user, password, os.environ.get("RADAR_HOST", "127.0.0.1"), port, interval, lookback, hosts)


def init_config(path: Path | None = None):
    path = path or ROOT / ".env.phase1"
    db_port = int(os.environ.get("RADAR_DB_PORT", "54329"))
    if not 1 <= db_port <= 65535:
        raise ValueError("RADAR_DB_PORT must be a valid port")
    db_password = secrets.token_urlsafe(32)
    content = ("# Local secrets. Never commit or paste this file into chat.\n"
               f"RADAR_DB_PASSWORD={db_password}\n"
               f"RADAR_DB_PORT={db_port}\n"
               f"RADAR_DATABASE_URL=postgresql://radar:{db_password}@127.0.0.1:{db_port}/guitar_radar\n"
               "RADAR_USERNAME=owner\n"
               f"RADAR_PASSWORD={secrets.token_urlsafe(24)}\n"
               "RADAR_HOST=127.0.0.1\nRADAR_PORT=8000\n"
               "RADAR_ALLOWED_HOSTS=localhost,127.0.0.1,::1\n"
               "# 0 = manual; scheduled collection requires >=900 seconds.\n"
               "RADAR_INTERVAL_SECONDS=0\nRADAR_LOOKBACK_HOURS=72\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
