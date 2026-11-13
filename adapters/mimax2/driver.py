from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


class MiMax2FlashDriver:
    """Linux sysfs flash driver with a fixed, non-configurable device path."""

    FLASH_PATH = Path("/sys/class/leds/white:flash/brightness")
    MAX_PATH = Path("/sys/class/leds/white:flash/max_brightness")

    def __init__(self, robot_id: str, *, flash_path: Path | None = None):
        self.robot_id = robot_id
        self.flash_path = flash_path or self.FLASH_PATH
        if self.flash_path != self.FLASH_PATH:
            raise ValueError("MiMax2 flash path is fixed")
        self.connected = False
        self.max_brightness = 4

    def connect(self) -> None:
        if not self.flash_path.is_file():
            raise RuntimeError(f"MiMax2 flash device is unavailable: {self.flash_path}")
        try:
            maximum = int(self.MAX_PATH.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError("MiMax2 flash brightness range is unavailable") from exc
        if maximum < 1 or maximum > 255:
            raise RuntimeError(f"Unexpected MiMax2 flash brightness range: {maximum}")
        self.max_brightness = maximum
        self.connected = True

    def health(self) -> bool:
        return self.connected and self.flash_path.is_file()

    def identity(self) -> tuple[str, str]:
        return "Xiaomi", "MiMax2"

    def read_brightness(self) -> int | None:
        """Read the current LED brightness, or None when the node is unreadable."""
        try:
            return int(self.flash_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def set_flash(self, enabled: bool) -> int | None:
        """Write the LED and return the observed brightness (None if unreadable).

        The read-back is the completion evidence the runtime requires; a
        readable node that does not change is a hard failure.
        """
        if not self.health():
            raise RuntimeError("MiMax2 flash driver is disconnected")
        value = self.max_brightness if enabled else 0
        payload = f"{value}\n".encode("ascii")
        try:
            self.flash_path.write_bytes(payload)
        except PermissionError:
            self._write_via_sudo(enabled, payload)
        return self._verify_brightness(value)

    def _verify_brightness(self, expected: int) -> int | None:
        observed = self.read_brightness()
        if observed is not None and observed != expected:
            raise RuntimeError(
                f"MiMax2 flash state did not change: requested {expected}, read back {observed}"
            )
        return observed

    def _write_via_sudo(self, enabled: bool, payload: bytes) -> None:
        sudo = shutil.which("sudo")
        tee = shutil.which("tee")
        helper = Path("/usr/local/sbin/voice-assistant-flashlight")
        if sudo and helper.is_file():
            action = "on" if enabled else "off"
            result = subprocess.run(
                [sudo, "-n", str(helper), action],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=1.0,
            )
            if result.returncode == 0:
                return
        if not sudo or not tee:
            raise PermissionError(f"Cannot write MiMax2 flash device: {self.flash_path}")
        result = subprocess.run(
            [sudo, "-n", tee, str(self.flash_path)],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1.0,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise PermissionError(detail or "sudo could not write MiMax2 flash device")

    def close(self) -> None:
        if self.connected:
            try:
                self.set_flash(False)
            except Exception:
                # Shutdown must stay best-effort: a missing or unreadable LED
                # node must not turn service teardown into a crash.
                pass
            finally:
                self.connected = False

    def discover_capabilities(self) -> dict[str, Any]:
        return {}

    def drain_observations(self) -> tuple[Any, ...]:
        return ()
