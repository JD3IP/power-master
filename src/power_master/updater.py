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
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    import docker as docker_sdk
except ImportError:
    docker_sdk = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# GHCR image coordinates
GHCR_IMAGE = "ghcr.io/jd3ip/power-master"
GHCR_MANIFEST_URL = f"https://ghcr.io/v2/jd3ip/power-master/tags/list"
GHCR_TOKEN_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository:jd3ip/power-master:pull"
GITHUB_RELEASES_API = "https://api.github.com/repos/JD3IP/power-master/releases"

VERSION_FILE = Path("/opt/power-master/version.json")
UPDATE_STATUS_FILE = Path("/data/.update_status.json")

CHECK_INTERVAL_SECONDS = 3600  # 1 hour
CONTAINER_NAME = "power-master"


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

    # Changelog from GitHub Release notes
    changelog: str = ""

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
                self._state.last_check_error = "Could not fetch version info from GHCR"
                self._state.last_check_at = datetime.now(timezone.utc).isoformat()
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
                self._state.changelog = await self._fetch_changelog(remote_version)
                logger.info(
                    "Update available: %s → %s",
                    self._state.current.version,
                    remote_version,
                )
            else:
                self._state.update_available = False
                self._state.changelog = ""

            self._state.state = "idle"
            return self._state.update_available

        except Exception as e:
            self._state.last_check_error = str(e)
            self._state.state = "idle"
            logger.warning("Update check failed: %s", e)
            return False

    def _check_docker_available(self) -> str | None:
        """Check if Docker SDK and socket are available.

        Returns None if OK, or an error message string.
        """
        if docker_sdk is None:
            return "Docker SDK not installed — self-update unavailable"

        socket_path = Path("/var/run/docker.sock")
        if not socket_path.exists():
            return (
                "Docker socket not found at /var/run/docker.sock. "
                "Mount it in docker-compose.yml: "
                "/var/run/docker.sock:/var/run/docker.sock"
            )

        try:
            client = docker_sdk.from_env()
            client.ping()
            return None
        except Exception as e:
            return f"Cannot connect to Docker daemon: {e}"

    @property
    def docker_available(self) -> bool:
        """Whether the Docker socket is reachable for self-updates."""
        return self._check_docker_available() is None

    async def execute_update(self) -> dict:
        """Pull the latest image and restart the container.

        Returns a status dict. The actual restart happens asynchronously —
        this method returns before the container is recreated.
        """
        if not self._state.update_available:
            return {"status": "error", "message": "No update available"}

        if self._state.state in ("downloading", "restarting"):
            return {"status": "error", "message": "Update already in progress"}

        # Pre-flight: check Docker access
        docker_err = self._check_docker_available()
        if docker_err:
            logger.error("Update blocked: %s", docker_err)
            self._state.state = "failed"
            self._state.error = docker_err
            return {"status": "error", "message": docker_err}

        self._state.state = "downloading"
        self._state.progress_message = "Pulling new image..."

        try:
            # 1. Pull the new image via Docker SDK (through mounted socket)
            logger.info("Pulling %s:latest ...", GHCR_IMAGE)
            client = docker_sdk.from_env()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: client.images.pull(GHCR_IMAGE, tag="latest")
            )
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
            asyncio.ensure_future(self._delayed_restart())

            return {"status": "ok", "message": "Update downloaded, restarting..."}

        except Exception as e:
            self._state.state = "failed"
            self._state.error = str(e)
            self._state.progress_message = ""
            logger.error("Update failed: %s", e)
            return {"status": "error", "message": str(e)}

    async def _delayed_restart(self) -> None:
        """Wait for API response to be sent, then trigger restart."""
        await asyncio.sleep(2.0)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._restart_container)

    def _restart_container(self) -> None:
        """Launch a helper container that stops this one and starts a new version.

        A container cannot restart itself directly — ``docker stop`` kills all
        processes inside it.  Instead we spin up a short-lived helper from the
        NEW image.  The helper: stops the old container → renames it → creates
        a new container with the same configuration → starts it.  If creation
        fails the helper restores the old container automatically.
        """
        try:
            client = docker_sdk.from_env()
            current = client.containers.get(CONTAINER_NAME)
            attrs = current.attrs

            restart_config = json.dumps({
                "name": current.name,
                "image": f"{GHCR_IMAGE}:latest",
                "binds": attrs["HostConfig"].get("Binds") or [],
                "network_mode": attrs["HostConfig"].get("NetworkMode", "bridge"),
                "restart_policy": attrs["HostConfig"].get("RestartPolicy") or {},
                "environment": attrs["Config"].get("Env") or [],
                "labels": attrs["Config"].get("Labels") or {},
            })

            # Python script executed inside the helper container.  It reads the
            # full container config from the RESTART_CONFIG env-var so there are
            # no shell-escaping issues.
            helper_script = '''
import docker, json, os, sys, time
time.sleep(3)
config = json.loads(os.environ["RESTART_CONFIG"])
client = docker.from_env()
name = config["name"]

def parse_binds(binds):
    vols = {}
    for b in binds:
        parts = b.split(":")
        if len(parts) == 2:
            vols[parts[0]] = {"bind": parts[1], "mode": "rw"}
        elif len(parts) >= 3:
            vols[parts[0]] = {"bind": parts[1], "mode": parts[2]}
    return vols

try:
    old = client.containers.get(name)
    old.stop(timeout=30)
    old.rename(name + "-old")
except Exception as e:
    print(f"Warning stopping old container: {e}")

try:
    client.containers.run(
        config["image"], name=name, detach=True,
        environment=config["environment"],
        volumes=parse_binds(config["binds"]),
        network_mode=config["network_mode"],
        restart_policy=config["restart_policy"],
        labels=config["labels"],
    )
    print(f"Container {name} recreated successfully")
    try:
        client.containers.get(name + "-old").remove(force=True)
    except Exception:
        pass
except Exception as e:
    print(f"ERROR recreating container: {e}", file=sys.stderr)
    try:
        old = client.containers.get(name + "-old")
        old.rename(name)
        old.start()
        print(f"Restored old container {name}")
    except Exception as e2:
        print(f"Failed to restore old container: {e2}", file=sys.stderr)
    sys.exit(1)
'''

            # Remove leftover helper from a previous attempt
            try:
                client.containers.get("power-master-updater").remove(force=True)
            except docker_sdk.errors.NotFound:
                pass

            client.containers.run(
                f"{GHCR_IMAGE}:latest",
                entrypoint="python",
                command=["-c", helper_script],
                name="power-master-updater",
                detach=True,
                auto_remove=True,
                environment={"RESTART_CONFIG": restart_config},
                volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock"}},
            )

            logger.info("Restart helper container launched")

        except Exception:
            logger.exception("Failed to trigger container restart")
            self._write_status({
                "state": "failed",
                "error": "Container restart failed",
            })

    async def _fetch_changelog(self, target_version: str) -> str:
        """Fetch release notes from GitHub Releases API.

        Tries the exact version tag first, then falls back to the latest release.
        Returns the release body (markdown) or empty string on failure.
        """
        headers = {"Accept": "application/vnd.github+json"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Try exact version tag
                resp = await client.get(
                    f"{GITHUB_RELEASES_API}/tags/v{target_version}",
                    headers=headers,
                )
                if resp.status_code == 404:
                    # Fall back to latest release
                    resp = await client.get(
                        f"{GITHUB_RELEASES_API}/latest",
                        headers=headers,
                    )
                if resp.status_code != 200:
                    logger.debug("GitHub release fetch failed: %d", resp.status_code)
                    return ""
                return resp.json().get("body", "") or ""
        except Exception as e:
            logger.debug("Failed to fetch changelog: %s", e)
            return ""

    async def _fetch_remote_labels(self) -> dict | None:
        """Fetch OCI labels from the latest GHCR image manifest.

        Uses the GHCR v2 API with anonymous token for public repos.
        GHCR redirects blob fetches to Azure storage, so follow_redirects
        is required (httpx strips Authorization on cross-origin redirects).
        """
        # Detect local architecture to pick the right platform from manifest
        arch_map = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}
        local_arch = arch_map.get(platform.machine(), "amd64")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
                    "application/vnd.oci.image.manifest.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            }

            # 2. Get the manifest for :latest
            manifest_url = "https://ghcr.io/v2/jd3ip/power-master/manifests/latest"
            resp = await client.get(manifest_url, headers=headers)
            if resp.status_code != 200:
                logger.warning("GHCR manifest fetch failed: %d", resp.status_code)
                return None

            manifest = resp.json()

            # 3. If it's a manifest list (multi-arch), pick the platform manifest
            if manifest.get("mediaType") in (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
            ):
                for m in manifest.get("manifests", []):
                    plat = m.get("platform", {})
                    if plat.get("architecture") == local_arch:
                        digest = m["digest"]
                        resp = await client.get(
                            f"https://ghcr.io/v2/jd3ip/power-master/manifests/{digest}",
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": (
                                    "application/vnd.oci.image.manifest.v1+json, "
                                    "application/vnd.docker.distribution.manifest.v2+json"
                                ),
                            },
                        )
                        if resp.status_code != 200:
                            logger.warning(
                                "GHCR platform manifest fetch failed: %d (arch=%s)",
                                resp.status_code, local_arch,
                            )
                            return None
                        manifest = resp.json()
                        break
                else:
                    logger.warning("No manifest found for arch=%s", local_arch)
                    return None

            # 4. Get the config blob which contains labels
            config_digest = manifest.get("config", {}).get("digest")
            if not config_digest:
                logger.warning("No config digest in manifest")
                return None

            resp = await client.get(
                f"https://ghcr.io/v2/jd3ip/power-master/blobs/{config_digest}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.oci.image.config.v1+json",
                },
            )
            if resp.status_code != 200:
                logger.warning("GHCR config blob fetch failed: %d", resp.status_code)
                return None

            try:
                config = resp.json()
            except Exception:
                logger.warning("Config blob not valid JSON (len=%d)", len(resp.content))
                return None

            labels = config.get("config", {}).get("Labels", {})
            if not labels:
                logger.info("No OCI labels found in config blob")
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
            "docker_available": self.docker_available,
            "changelog": self._state.changelog,
            "last_check": self._state.last_check_at,
            "last_check_error": self._state.last_check_error,
            "state": self._state.state,
            "error": self._state.error,
            "progress": self._state.progress_message,
        }
