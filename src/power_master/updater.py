"""Self-update manager — checks GHCR for new versions and orchestrates updates.

Flow:
  1. Periodic check (hourly): compare local version.json against GHCR latest manifest
  2. User triggers update via Settings UI → POST /api/system/update
  3. Pull new image, tag old as rollback, recreate container
  4. New container runs startup health check → marks success or failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# GHCR image coordinates
GHCR_IMAGE = "ghcr.io/jd3ip/power-master"
GHCR_MANIFEST_URL = f"https://ghcr.io/v2/jd3ip/power-master/tags/list"
GHCR_TOKEN_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository:jd3ip/power-master:pull"

VERSION_FILE = Path("/opt/power-master/version.json")
UPDATE_STATUS_FILE = Path("/data/.update_status.json")

CHECK_INTERVAL_SECONDS = 3600  # 1 hour


@dataclass
class VersionInfo:
    """Version information for current or remote build."""

    version: str = "dev"
    sha: str = "unknown"
    built_at: str = ""


@dataclass
class UpdateState:
    """Current state of the update system."""

    # Version info
    current: VersionInfo = field(default_factory=VersionInfo)
    latest: VersionInfo | None = None
    update_available: bool = False

    # Check state
    last_check_at: str = ""
    last_check_error: str = ""

    # Update progress
    state: str = "idle"  # idle, checking, downloading, restarting, success, failed
    error: str = ""
    progress_message: str = ""


class UpdateManager:
    """Manages version checking and self-updates via GHCR + Docker."""

    def __init__(self) -> None:
        self._state = UpdateState()
        self._stop_event = asyncio.Event()
        self._load_current_version()
        self._check_post_update_status()

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def update_available(self) -> bool:
        return self._state.update_available

    @property
    def latest_version(self) -> str | None:
        return self._state.latest.version if self._state.latest else None

    def _load_current_version(self) -> None:
        """Read the baked-in version.json from the Docker image."""
        try:
            if VERSION_FILE.exists():
                data = json.loads(VERSION_FILE.read_text())
                self._state.current = VersionInfo(
                    version=data.get("version", "dev"),
                    sha=data.get("sha", "unknown"),
                    built_at=data.get("built_at", ""),
                )
                logger.info(
                    "Current version: %s (sha: %s)",
                    self._state.current.version,
                    self._state.current.sha[:8],
                )
            else:
                logger.info("No version.json found — running dev/local build")
        except Exception:
            logger.warning("Failed to read version.json", exc_info=True)

    def _check_post_update_status(self) -> None:
        """On startup, check if we just completed an update."""
        try:
            if UPDATE_STATUS_FILE.exists():
                data = json.loads(UPDATE_STATUS_FILE.read_text())
                if data.get("state") == "updating":
                    # We just restarted after an update — mark success
                    logger.info(
                        "Post-update startup: updated from %s to %s",
                        data.get("from", "?"),
                        data.get("to", "?"),
                    )
                    self._state.state = "success"
                    self._state.progress_message = (
                        f"Updated from {data.get('from', '?')} to {data.get('to', '?')}"
                    )
                    self._write_status({"state": "success", **data})
                elif data.get("state") == "failed":
                    self._state.state = "failed"
                    self._state.error = data.get("error", "Unknown error")
        except Exception:
            logger.warning("Failed to read update status", exc_info=True)

    async def run(self) -> None:
        """Run the periodic version check loop."""
        logger.info("Update manager starting (check interval: %ds)", CHECK_INTERVAL_SECONDS)

        # Initial check after a short delay (let the app fully start first)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            return  # stopped
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            await self.check_for_update()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Signal the check loop to stop."""
        self._stop_event.set()

    async def check_for_update(self) -> bool:
        """Check GHCR for a newer image. Returns True if update available."""
        self._state.state = "checking"
        try:
            remote_labels = await self._fetch_remote_labels()
            if remote_labels is None:
                self._state.state = "idle"
                return False

            remote_version = remote_labels.get(
                "org.opencontainers.image.version", ""
            )
            remote_sha = remote_labels.get(
                "org.opencontainers.image.revision", ""
            )

            self._state.latest = VersionInfo(
                version=remote_version or "unknown",
                sha=remote_sha or "unknown",
            )
            self._state.last_check_at = datetime.now(timezone.utc).isoformat()
            self._state.last_check_error = ""

            # Compare: update available if SHA differs and remote isn't empty
            if remote_sha and remote_sha != self._state.current.sha:
                self._state.update_available = True
                logger.info(
                    "Update available: %s → %s",
                    self._state.current.version,
                    remote_version,
                )
            else:
                self._state.update_available = False

            self._state.state = "idle"
            return self._state.update_available

        except Exception as e:
            self._state.last_check_error = str(e)
            self._state.state = "idle"
            logger.warning("Update check failed: %s", e)
            return False

    async def execute_update(self) -> dict:
        """Pull the latest image and restart the container.

        Returns a status dict. The actual restart happens asynchronously —
        this method returns before the container is recreated.
        """
        if not self._state.update_available:
            return {"status": "error", "message": "No update available"}

        if self._state.state in ("downloading", "restarting"):
            return {"status": "error", "message": "Update already in progress"}

        self._state.state = "downloading"
        self._state.progress_message = "Pulling new image..."

        try:
            # 1. Pull the new image
            logger.info("Pulling %s:latest ...", GHCR_IMAGE)
            result = subprocess.run(
                ["docker", "pull", f"{GHCR_IMAGE}:latest"],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"docker pull failed: {result.stderr.strip()}")

            logger.info("Image pulled successfully")

            # 2. Write update status so the new container knows it just updated
            self._write_status({
                "state": "updating",
                "from": self._state.current.version,
                "to": self._state.latest.version if self._state.latest else "unknown",
                "started_at": datetime.now(timezone.utc).isoformat(),
            })

            # 3. Schedule restart (gives time for the API response to be sent)
            self._state.state = "restarting"
            self._state.progress_message = "Restarting container..."
            asyncio.get_event_loop().call_later(2.0, self._restart_container)

            return {"status": "ok", "message": "Update downloaded, restarting..."}

        except Exception as e:
            self._state.state = "failed"
            self._state.error = str(e)
            self._state.progress_message = ""
            logger.error("Update failed: %s", e)
            return {"status": "error", "message": str(e)}

    def _restart_container(self) -> None:
        """Recreate the container using docker compose (runs in background)."""
        try:
            # Find the compose file — check common locations
            compose_file = self._find_compose_file()
            if compose_file:
                cmd = ["docker", "compose", "-f", compose_file, "up", "-d"]
            else:
                cmd = ["docker", "compose", "up", "-d"]

            logger.info("Restarting via: %s", " ".join(cmd))
            # Fire and forget — the daemon will stop this container and start a new one
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.exception("Failed to trigger container restart")
            self._write_status({
                "state": "failed",
                "error": "Container restart failed",
            })

    def _find_compose_file(self) -> str | None:
        """Locate the docker-compose file."""
        candidates = [
            "/data/docker-compose.yml",
            "/data/docker-compose.pi.yml",
            # Compose file might be bind-mounted
            os.environ.get("COMPOSE_FILE", ""),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    async def _fetch_remote_labels(self) -> dict | None:
        """Fetch OCI labels from the latest GHCR image manifest.

        Uses the GHCR v2 API with anonymous token for public repos.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Get anonymous bearer token
            resp = await client.get(GHCR_TOKEN_URL)
            if resp.status_code != 200:
                logger.warning("GHCR token request failed: %d", resp.status_code)
                return None
            token = resp.json().get("token")
            if not token:
                return None

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            }

            # 2. Get the manifest for :latest
            manifest_url = f"https://ghcr.io/v2/jd3ip/power-master/manifests/latest"
            resp = await client.get(manifest_url, headers=headers)
            if resp.status_code != 200:
                logger.warning("GHCR manifest fetch failed: %d", resp.status_code)
                return None

            manifest = resp.json()

            # 3. If it's a manifest list (multi-arch), pick the amd64 manifest
            if manifest.get("mediaType") in (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
            ):
                for m in manifest.get("manifests", []):
                    platform = m.get("platform", {})
                    if platform.get("architecture") == "amd64":
                        digest = m["digest"]
                        resp = await client.get(
                            f"https://ghcr.io/v2/jd3ip/power-master/manifests/{digest}",
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.docker.distribution.manifest.v2+json",
                            },
                        )
                        if resp.status_code != 200:
                            return None
                        manifest = resp.json()
                        break
                else:
                    return None

            # 4. Get the config blob which contains labels
            config_digest = manifest.get("config", {}).get("digest")
            if not config_digest:
                return None

            resp = await client.get(
                f"https://ghcr.io/v2/jd3ip/power-master/blobs/{config_digest}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return None

            config = resp.json()
            labels = config.get("config", {}).get("Labels", {})
            return labels

    def _write_status(self, data: dict) -> None:
        """Write update status to the persistent data volume."""
        try:
            UPDATE_STATUS_FILE.write_text(json.dumps(data))
        except Exception:
            logger.warning("Failed to write update status", exc_info=True)

    def to_dict(self) -> dict:
        """Serialize state for API/SSE consumption."""
        return {
            "current": {
                "version": self._state.current.version,
                "sha": self._state.current.sha,
                "built_at": self._state.current.built_at,
            },
            "latest": {
                "version": self._state.latest.version,
                "sha": self._state.latest.sha,
            } if self._state.latest else None,
            "update_available": self._state.update_available,
            "last_check": self._state.last_check_at,
            "last_check_error": self._state.last_check_error,
            "state": self._state.state,
            "error": self._state.error,
            "progress": self._state.progress_message,
        }
