#!/usr/bin/env python3
"""Prepare this project's NapCat secrets/configuration on the Docker host.

Reads the existing NapCat WebUI configuration; writes only inside notify-stack.
Does not start/restart containers, change system files or install software.
"""

import argparse
import datetime
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def atomic_write(path: Path, text: str, mode=0o600):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--napcat-config", type=Path, default=Path(os.getenv("NAPCAT_CONFIG_PATH", "/root/services/AstrBot/napcat/config/webui.json")), help="Path to napcat webui.json")
    parser.add_argument("--network", default=os.getenv("NAPCAT_NETWORK", "astrbot_default"), help="Docker network name")
    parser.add_argument("--public-url", default=os.getenv("NAPCAT_PUBLIC_URL", "https://gotify.example.com"), help="Public HTTPS origin for QR login")
    parser.add_argument("--configure-tunnel", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run on the Docker host as root to prepare files readable by container UID 10001; no files changed")
    origin = urlsplit(args.public_url)
    if origin.scheme != "https" or not origin.hostname or origin.path not in {"", "/"} or origin.username or origin.password or origin.query or origin.fragment:
        parser.error("--public-url must be an HTTPS origin")
    p = args.project.resolve()
    env = p / ".env"
    if not env.exists():
        parser.error("Configure and deploy the notification gateway first (.env is missing)")
    data = json.loads(args.napcat_config.read_text(encoding="utf-8-sig"))
    token = data.get("token")
    if not isinstance(token, str) or not token:
        parser.error("NapCat WebUI token is missing")
    tunnel = p / "cloudflared/config.yml"
    tunnel_text = None
    if args.configure_tunnel:
        tunnel_text = tunnel.read_text()
        if "ingress:\n" not in tunnel_text or "hostname: " + origin.hostname not in tunnel_text:
            parser.error("Tunnel configuration does not contain the public hostname; no files changed")
    directory = p / ".secrets"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = directory / "backups" / ("napcat-config-" + stamp)
    backup.mkdir(parents=True)
    backup.chmod(0o700)
    atomic_write(backup / ".env", env.read_text())
    if tunnel_text is not None:
        atomic_write(backup / "cloudflared.yml", tunnel_text)
    target = directory / "napcat-watch"
    target.mkdir(exist_ok=True)
    target.chmod(0o700)
    atomic_write(target / "webui-token", token + "\n", 0o400)
    os.chown(target / "webui-token", 10001, 10001)
    lines = env.read_text().splitlines()
    old = dict(line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line)
    secret = old.get("NAPCAT_LINK_SECRET", "")
    if len(secret.strip("'\"")) < 32 or secret.strip("'\"").startswith("replace_"):
        secret = secrets.token_urlsafe(32)
    values = {
        "COMPOSE_FILE": "docker-compose.yml:docker-compose.napcat.yml",
        "NAPCAT_LINK_SECRET": secret,
        "NAPCAT_PUBLIC_URL": args.public_url.rstrip("/"),
        "NAPCAT_NETWORK": args.network,
        "NAPCAT_URL": "http://napcat:6099",
    }
    lines = [line for line in lines if line.split("=", 1)[0] not in values]
    lines.extend(key + "=" + value for key, value in values.items())
    atomic_write(env, "\n".join(lines) + "\n")
    if tunnel_text is not None and "path: ^/napcat/.*" not in tunnel_text:
        route = f"ingress:\n  - hostname: {origin.hostname}\n    path: ^/napcat/.*\n    service: http://napcat-watch:8091\n"
        atomic_write(tunnel, tunnel_text.replace("ingress:\n", route, 1), 0o644)
    print("NapCat watcher configuration prepared; existing containers were not restarted.")
    print("Protected configuration backup:", backup)
    print("Next: docker compose config --quiet && docker compose build gateway")
    print("Then: docker compose up -d --wait gotify gateway napcat-watch")
    if args.configure_tunnel:
        print("Reload tunnel routes: docker compose up -d --force-recreate --wait cloudflared")


if __name__ == "__main__":
    main()
