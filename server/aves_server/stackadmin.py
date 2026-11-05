"""Adding and removing dumpsite shares from Settings: the app updates its own Portainer stack.

A share only becomes visible to the containers when the stack mounts it, so adding one means setting a free
DUMPSITE_<slot> variable on the stack and redeploying it, which restarts the app (about a minute). The list of
shares comes from the TrueNAS API, and only those shares can be mounted: never an arbitrary path from the browser.

Configured with (web service only): AVES_PORTAINER_URL, AVES_PORTAINER_TOKEN, AVES_PORTAINER_STACK_ID,
AVES_PORTAINER_ENDPOINT_ID, AVES_TRUENAS_URL, AVES_TRUENAS_API_KEY; AVES_PHOTOS_HOST_PATH and
AVES_APPDATA_HOST_PATH tell it which folders must never become a dumpsite.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import PurePosixPath

from .dumpsites import SLOTS

logger = logging.getLogger(__name__)


class StackAdminError(RuntimeError):
    pass


@dataclass
class StackAdmin:
    portainer_url: str
    portainer_token: str
    stack_id: int
    endpoint_id: int
    truenas_url: str
    truenas_key: str
    protected: dict[str, str]  # host path -> why it can't be a dumpsite

    @classmethod
    def from_env(cls) -> StackAdmin | None:
        env = os.environ
        needed = ("AVES_PORTAINER_URL", "AVES_PORTAINER_TOKEN", "AVES_PORTAINER_STACK_ID", "AVES_TRUENAS_URL", "AVES_TRUENAS_API_KEY")
        if not all(env.get(name, "").strip() for name in needed):
            return None
        truenas = env["AVES_TRUENAS_URL"].strip().rstrip("/")
        protected = {}
        if env.get("AVES_PHOTOS_HOST_PATH"):
            protected[env["AVES_PHOTOS_HOST_PATH"].rstrip("/")] = "holds the main dumpsite and the published library"
        if env.get("AVES_APPDATA_HOST_PATH"):
            protected[env["AVES_APPDATA_HOST_PATH"].rstrip("/")] = "holds the app's own data"
        return cls(env["AVES_PORTAINER_URL"].strip().rstrip("/"), env["AVES_PORTAINER_TOKEN"].strip(), int(env["AVES_PORTAINER_STACK_ID"]),
                   int(env.get("AVES_PORTAINER_ENDPOINT_ID") or 3), truenas if "://" in truenas else f"https://{truenas}",
                   env["AVES_TRUENAS_API_KEY"].strip(), protected)

    # ------------------------------------------------------------ HTTP

    @staticmethod
    def _request(url: str, headers: dict, method: str = "GET", body=None, timeout: float = 20):
        # Both run on the NAS itself with self-signed certificates.
        context = ssl._create_unverified_context()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, method=method, data=data, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            raise StackAdminError(f"{url.split('/api/')[0]} answered {exc.code}: {detail}") from exc
        except OSError as exc:
            raise StackAdminError(f"Could not reach {url.split('/api/')[0]}: {exc}") from exc

    def _portainer(self, method: str, path: str, body=None, timeout: float = 20):
        return self._request(self.portainer_url + path, {"X-API-Key": self.portainer_token}, method, body, timeout)

    # ------------------------------------------------------------ shares and slots

    def shares(self) -> list[dict]:
        rows = self._request(self.truenas_url + "/api/v2.0/sharing/smb", {"Authorization": f"Bearer {self.truenas_key}"})
        return sorted(({"name": row["name"], "path": row["path"].rstrip("/"), "enabled": bool(row.get("enabled", True))}
                       for row in rows if str(row.get("path", "")).startswith("/mnt/")), key=lambda s: s["name"].casefold())

    def stack_env(self) -> list[dict]:
        return self._portainer("GET", f"/api/stacks/{self.stack_id}")["Env"] or []

    @staticmethod
    def mounted_slots(env: list[dict]) -> dict[int, str]:
        result = {}
        for item in env:
            name, value = item["name"], (item.get("value") or "").strip()
            if name.startswith("DUMPSITE_") and name[9:].isdigit() and value:
                result[int(name[9:])] = value.rstrip("/")
        return result

    def blocked_reason(self, path: str, dumpsite_paths: dict[str, str]) -> str | None:
        """Why a share can't be added; None when it can. Overlapping folders would be scanned twice or publish into themselves."""
        def overlaps(a: str, b: str) -> bool:
            pa, pb = PurePosixPath(a), PurePosixPath(b)
            return pa == pb or pa in pb.parents or pb in pa.parents

        for protected, why in self.protected.items():
            if overlaps(path, protected):
                return f"Overlaps {protected}, which {why}"
        for other, name in dumpsite_paths.items():
            if path == other:
                return f"Already the dumpsite {name}"
            if overlaps(path, other):
                return f"Overlaps the dumpsite {name} ({other})"
        return None

    # ------------------------------------------------------------ redeploy

    def _redeploy(self, change: dict[str, str | None]) -> None:
        stack = self._portainer("GET", f"/api/stacks/{self.stack_id}")
        env = [item for item in stack["Env"] or [] if item["name"] not in change]
        env += [{"name": name, "value": value} for name, value in change.items() if value is not None]
        body = {"Env": env, "Prune": False, "RepositoryReferenceName": stack["GitConfig"]["ReferenceName"],
                "RepositoryAuthentication": False, "PullImage": False}
        self._portainer("PUT", f"/api/stacks/{self.stack_id}/git/redeploy?endpointId={self.endpoint_id}", body, timeout=1800)


class Redeployer:
    """Runs one stack redeploy at a time in the background: the request that asks for it can't wait, as the
    redeploy restarts this very process. Portainer carries on with the deploy if the caller goes away."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running: str | None = None
        self.error: str | None = None

    def start(self, admin: StackAdmin, change: dict[str, str | None], what: str) -> None:
        with self._lock:
            if self.running:
                raise StackAdminError(f"Still busy: {self.running}")
            self.running, self.error = what, None
        threading.Thread(target=self._run, args=(admin, change, what), daemon=True).start()

    def _run(self, admin: StackAdmin, change: dict[str, str | None], what: str) -> None:
        try:
            admin._redeploy(change)
        except Exception as exc:  # the process survives a failed deploy, so the error can be shown
            logger.exception("Stack redeploy failed (%s)", what)
            self.error = f"{what} failed: {exc}"
        finally:
            self.running = None


redeployer = Redeployer()


def free_slot(env_slots: dict[int, str], used: set[int]) -> int | None:
    return next((slot for slot in SLOTS if slot not in env_slots and slot not in used), None)
