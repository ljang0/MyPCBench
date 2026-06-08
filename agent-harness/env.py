"""MyPCBenchEnv — OSWorld-compatible desktop environment for MyPCBench.

Wraps a Docker container running the MyPCBench QEMU image (or boots the
qcow2 under qemu-system-x86_64 directly with --backend qemu) and exposes
the same interface as OSWorld's DesktopEnv: reset(), step(), _get_obs().
Agents built for OSWorld can plug in directly. Grading is not part of the
env — the runner writes a completion marker + rubric bundle and scoring is
a separate post-run step (agent-harness/judge_results.py).

Usage:
    env = MyPCBenchEnv(docker_image="ljang/mypcbench-qemu:latest")
    obs = env.reset(task_config=task)
    while not done:
        response, actions = agent.predict(instruction, obs)
        for action in actions:
            obs, reward, done, info = env.step(action)
"""

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from utils.persona_registry import resolve_persona_email

log = logging.getLogger("mypcbench.env")

CONTROL_API_PORT = 5000
VNC_PORT = 5901
DEFAULT_SCREEN_SIZE = (1280, 800)


# OVMF firmware discovery shared by env.py (single-VM) and
# run_parallel_tasks.py (parallel runs). Resolution order:
#   1. MYPCBENCH_OVMF_CODE [+ MYPCBENCH_OVMF_VARS] explicit override
#   2. MYPCBENCH_QEMU_EXTRACTED legacy extracted-RPM root
#   3. Glob auto-discovery in /usr/share/OVMF, /usr/share/edk2*/ovmf
#      (plain-UEFI variants preferred over .secboot / .ms / .snakeoil)
def _discover_ovmf() -> Tuple[Optional[str], Optional[str]]:
    import glob as _glob

    code_env = os.environ.get("MYPCBENCH_OVMF_CODE")
    if code_env:
        vars_env = os.environ.get("MYPCBENCH_OVMF_VARS") or code_env.replace("CODE", "VARS")
        if not os.path.exists(code_env):
            raise RuntimeError(
                f"MYPCBENCH_OVMF_CODE={code_env!r} does not exist. "
                f"Point it at a readable OVMF_CODE*.fd file."
            )
        if not os.path.exists(vars_env):
            raise RuntimeError(
                f"MYPCBENCH_OVMF_VARS={vars_env!r} does not exist. "
                f"Set MYPCBENCH_OVMF_VARS explicitly to the matching VARS file."
            )
        return code_env, vars_env

    extracted = os.environ.get("MYPCBENCH_QEMU_EXTRACTED")
    search_dirs = []
    if extracted:
        search_dirs.extend([
            f"{extracted}/usr/share/OVMF",
            f"{extracted}/usr/share/edk2/ovmf",
            f"{extracted}/usr/share/edk2-ovmf/x64",
        ])
    search_dirs.extend([
        "/usr/share/OVMF",
        "/usr/share/edk2/ovmf",
        "/usr/share/edk2-ovmf/x64",
        "/usr/share/qemu/ovmf",
    ])

    excluded = ("secboot", ".ms.", "snakeoil")  # variants needing signed kernels
    candidates = []
    for d in search_dirs:
        for code_path in sorted(_glob.glob(f"{d}/OVMF_CODE*.fd")):
            base = os.path.basename(code_path).lower()
            if any(tag in base for tag in excluded):
                continue
            vars_path = code_path.replace("CODE", "VARS")
            if os.path.exists(vars_path):
                candidates.append((code_path, vars_path))

    if not candidates:
        looked = ", ".join(search_dirs)
        raise RuntimeError(
            "OVMF UEFI firmware not found. The QEMU backend requires "
            "OVMF_CODE*.fd + OVMF_VARS*.fd. Install one of:\n"
            "  Debian/Ubuntu:  sudo apt install ovmf\n"
            "  Fedora/RHEL:    sudo dnf install edk2-ovmf\n"
            "  Arch:           sudo pacman -S edk2-ovmf\n"
            "Or set MYPCBENCH_OVMF_CODE=/path/to/OVMF_CODE.fd "
            "(and optionally MYPCBENCH_OVMF_VARS) to point at a "
            "non-standard install. Searched: " + looked
        )
    # Prefer the 4MB split (newer Debian/Ubuntu default) when present.
    candidates.sort(key=lambda p: (0 if "_4M" in p[0] else 1, p[0]))
    return candidates[0]




def _fix_pyautogui_less_than_bug(code: str) -> str:
    """Fix PyAutoGUI bug where '<' produces '>' in typewrite/press calls.

    Ported from OSWorld's desktop_env/desktop_env.py.
    Replaces '<' in typewrite() and press() calls with hotkey('shift', ',').
    """
    import re

    def _replace_less_than(match):
        prefix = match.group(1)  # pyautogui.typewrite( or pyautogui.press(
        content = match.group(2)
        suffix = match.group(3)
        if '<' not in content:
            return match.group(0)
        # Replace < with shift+comma
        parts = content.split('<')
        result_lines = []
        for i, part in enumerate(parts):
            if i > 0:
                result_lines.append("pyautogui.hotkey('shift', ',')")
            if part:
                result_lines.append(f"{prefix}{part}{suffix}")
        return '; '.join(result_lines)

    # Match typewrite('...') and press('...')
    pattern = r"(pyautogui\.(?:typewrite|press)\()(['\"].*?['\"])(\))"
    if '<' in code:
        code = re.sub(pattern, _replace_less_than, code)
    return code


class MyPCBenchEnv:
    """OSWorld-compatible desktop environment backed by Docker."""

    def __init__(
        self,
        docker_image: str = "ljang/mypcbench-qemu:latest",
        container_name: str = "mypcbench-agent",
        persona: str = "michael_scott",
        world: str = "scranton-office",
        action_space: str = "pyautogui",
        screen_size: Tuple[int, int] = DEFAULT_SCREEN_SIZE,
        headless: bool = True,
        os_type: str = "Ubuntu",
        client_password: str = "password",
        backend: str = "qemu",
        qcow2_path: Optional[str] = None,
    ):
        self.docker_image = docker_image
        # Make the bare default unique per process so two concurrent
        # `run_mypcbench.py` invocations on one host can't collide on the
        # same /tmp/mypcbench-<name>-overlay.qcow2 / pidfile / ports and
        # cross-kill each other's QEMU. Explicit --container_name (e.g.
        # the parallel runner's per-VM names) is preserved verbatim.
        if container_name == "mypcbench-agent":
            container_name = f"mypcbench-agent-{os.getpid()}"
        self.container_name = container_name
        self.persona = persona
        # Canonical auto-login email (SANDBOX_LOGIN_EMAIL → persona JSON →
        # legacy fallback). Used for warmup cookies and DB assertions.
        self.persona_email = resolve_persona_email(persona)
        self.world = world
        self.action_space = action_space
        self.screen_width, self.screen_height = screen_size
        self.headless = headless
        self.os_type = os_type
        self.client_password = client_password

        # Backend: "docker" (default) or "qemu" (direct QEMU, no Docker)
        self.backend = backend
        # For the QEMU backend, pass a base qcow2 path or set MYPCBENCH_QCOW2.
        # The top-level runners refresh the managed ./mypcbench-vm cache from
        # latest before default QEMU boots.
        self.qcow2_path = qcow2_path or os.environ.get("MYPCBENCH_QCOW2")
        self._qemu_pid: Optional[int] = None
        self._overlay_path: Optional[str] = None
        self._vars_qcow2_path: Optional[str] = None

        self.vm_ip = os.environ.get("MYPCBENCH_VM_HOST", "localhost")
        self.api_port = CONTROL_API_PORT
        self._host_api_port: Optional[int] = None
        self._host_vnc_port: Optional[int] = None
        # Discovered host-side ports for the 17 Next.js apps + LibreOffice
        # service, keyed by their in-container port (3001..3018). Populated
        # by _start_container().
        self._host_app_ports: dict[int, int] = {}
        self._session = requests.Session()

        self.instruction: str = ""
        self.config: Dict = {}
        self.action_history: List = []
        self._step_no = 0
        self._traj_no = 0
        self.is_environment_used = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        port = self._host_api_port or self.api_port
        return f"http://{self.vm_ip}:{port}"

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    def _start_container(self):
        """Start a container, or attach to an existing one.

        Attach mode (set by the parallel runner via env vars, so one parent
        process can pre-boot many containers and then invoke run_mypcbench.py
        in a subprocess per replica without the subprocess destroying the
        container it's supposed to drive):
            MYPCBENCH_REUSE_CONTAINER=1          don't rm -f; just discover
            MYPCBENCH_HOST_API_PORT=<port>       skip port discovery
            MYPCBENCH_HOST_VNC_PORT=<port>       skip port discovery

        Default mode: hard-reset — `docker rm -f` + `docker run -d` with
        random host ports, matching OSWorld's DesktopEnv contract.
        """
        if self.backend == "qemu":
            return self._start_qemu()

        self._host_api_port = None
        self._host_vnc_port = None

        reuse = os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0") in ("1", "true", "yes")

        if reuse:
            # Attach to whatever is already running under this container name.
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
                capture_output=True, text=True,
            )
            if inspect.returncode != 0 or "true" not in inspect.stdout:
                raise RuntimeError(
                    f"MYPCBENCH_REUSE_CONTAINER=1 but container "
                    f"{self.container_name!r} is not running. "
                    f"Start it manually before invoking run_mypcbench.py."
                )
            # Prefer explicit env-var overrides (from the parallel runner);
            # otherwise discover from `docker port`.
            env_api = os.environ.get("MYPCBENCH_HOST_API_PORT")
            env_vnc = os.environ.get("MYPCBENCH_HOST_VNC_PORT")
            if env_api:
                self._host_api_port = int(env_api)
            if env_vnc:
                self._host_vnc_port = int(env_vnc)
            if not self._host_api_port or not self._host_vnc_port:
                port_result = subprocess.run(
                    ["docker", "port", self.container_name],
                    capture_output=True, text=True,
                )
                for line in port_result.stdout.strip().splitlines():
                    if f"{CONTROL_API_PORT}/tcp" in line and not self._host_api_port:
                        self._host_api_port = int(line.split(":")[-1])
                    elif f"{VNC_PORT}/tcp" in line and not self._host_vnc_port:
                        self._host_vnc_port = int(line.split(":")[-1])
            if not self._host_api_port:
                raise RuntimeError(
                    f"Failed to discover API port for container "
                    f"{self.container_name} (reuse mode). "
                    f"Set MYPCBENCH_HOST_API_PORT explicitly or expose 5000."
                )
            log.info(
                "Container %s reused (API: %s, VNC: %s)",
                self.container_name, self._host_api_port, self._host_vnc_port,
            )
            return

        # Default: hard-reset — rebuild the container.
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
        )

        # Expose the web app ports too — the agent reaches them
        # at localhost:<port> and they must be reachable from the host.
        # 3001–3017 are the 17 Next.js apps; 3018 is the LibreOffice
        # service. Using :0: lets Docker pick any free host port; we
        # rediscover them via `docker port` below.
        # The QEMU-in-Docker wrapper image needs `--privileged` and
        # `--device /dev/kvm` so the inner QEMU can use KVM; otherwise it
        # falls back to software emulation and Control API never comes
        # up within the 180 s _wait_for_ready budget, so always pass
        # --privileged + --device /dev/kvm.
        cmd = [
            "docker", "run", "-d",
            "--pull", "always",
            "--name", self.container_name,
            "--privileged",
            "--device", "/dev/kvm",
            "-e", f"PERSONA={self.persona}",
            "-e", f"VM_ID={self.persona}",
            "-e", f"WORLD={self.world}",
            "-p", f"127.0.0.1:0:{CONTROL_API_PORT}",
            "-p", f"127.0.0.1:0:{VNC_PORT}",
        ]
        for app_port in range(3001, 3019):
            cmd.extend(["-p", f"127.0.0.1:0:{app_port}"])
        cmd.append(self.docker_image)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start container: {result.stderr}")

        self._discover_ports()
        if not self._host_api_port:
            raise RuntimeError(
                f"Failed to discover API port for container {self.container_name}."
            )

        log.info(
            "Container %s started (API: %s, VNC: %s, %d app ports discovered)",
            self.container_name, self._host_api_port, self._host_vnc_port,
            len(self._host_app_ports),
        )

    def _discover_ports(self) -> None:
        """Populate _host_api_port / _host_vnc_port / _host_app_ports from
        `docker port <container>`.
        """
        if self.backend == "qemu":
            return self._discover_ports_qemu()

        self._host_app_ports = {}
        port_result = subprocess.run(
            ["docker", "port", self.container_name],
            capture_output=True, text=True,
        )
        for line in port_result.stdout.strip().splitlines():
            # Lines look like: "5000/tcp -> 127.0.0.1:49871"
            if "->" not in line:
                continue
            container_part, host_part = line.split("->", 1)
            container_port_str = container_part.strip().split("/")[0]
            host_port = host_part.strip().split(":")[-1]
            try:
                cp = int(container_port_str)
                hp = int(host_port)
            except ValueError:
                continue
            if cp == CONTROL_API_PORT and not self._host_api_port:
                self._host_api_port = hp
            elif cp == VNC_PORT and not self._host_vnc_port:
                self._host_vnc_port = hp
            elif 3001 <= cp <= 3018:
                # Prefer the first mapping we see per container port
                self._host_app_ports.setdefault(cp, hp)
        # Publish resolved host app ports via env vars so subprocess
        # agents can read the per-app forwarded ports.
        for cp, hp in self._host_app_ports.items():
            os.environ[f"MYPCBENCH_HOST_APP_PORT_{cp}"] = str(hp)

    # ------------------------------------------------------------------
    # QEMU-direct backend (additive — does not touch Docker code)
    # ------------------------------------------------------------------

    def _find_uefi_firmware(self) -> tuple[Optional[str], Optional[str]]:
        """Locate OVMF UEFI firmware (CODE + VARS).

        Resolution order:
          1. MYPCBENCH_OVMF_CODE (and optional MYPCBENCH_OVMF_VARS) —
             explicit user override; honored verbatim.
          2. MYPCBENCH_QEMU_EXTRACTED — extracted-RPM root for shared
             clusters (legacy escape hatch from the OSS README).
          3. Auto-discovery via glob across the standard distro locations
             (Ubuntu/Debian `OVMF_CODE_4M.fd`, Fedora/RHEL/Arch
             `OVMF_CODE.fd`, etc.). Plain-UEFI variants are preferred
             over secboot / .ms / .snakeoil siblings.
        """
        return _discover_ovmf()

    def _start_qemu(self):
        """Boot the QEMU VM directly (no Docker)."""
        reuse = os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0") in ("1", "true", "yes")

        # Read port overrides from env (set by parallel runner or user)
        env_api = os.environ.get("MYPCBENCH_HOST_API_PORT")
        env_vnc = os.environ.get("MYPCBENCH_HOST_VNC_PORT")
        if env_api:
            self._host_api_port = int(env_api)
        if env_vnc:
            self._host_vnc_port = int(env_vnc)

        if reuse:
            # Attach to an already-running QEMU process (local or remote).
            if not self._host_api_port:
                self._host_api_port = CONTROL_API_PORT
            if not self._host_vnc_port:
                self._host_vnc_port = VNC_PORT

            # For remote VMs, check connectivity via health endpoint
            # instead of local PID file.
            if self.vm_ip != "localhost" and self.vm_ip != "127.0.0.1":
                try:
                    r = self._session.get(f"{self.base_url}/health", timeout=5)
                    if not r.ok:
                        raise RuntimeError(f"Remote VM at {self.vm_ip} unhealthy")
                except Exception as e:
                    raise RuntimeError(
                        f"MYPCBENCH_REUSE_CONTAINER=1 but cannot reach QEMU VM "
                        f"at {self.base_url}/health: {e}"
                    )
            else:
                if not self._is_qemu_running():
                    raise RuntimeError(
                        f"MYPCBENCH_REUSE_CONTAINER=1 but no QEMU process found "
                        f"for {self.container_name!r}. Start the VM manually first."
                    )
            self._discover_ports_qemu()
            log.info(
                "QEMU %s reused (host=%s, PID=%s, API=:%s, VNC=:%s)",
                self.container_name, self.vm_ip, self._qemu_pid,
                self._host_api_port, self._host_vnc_port,
            )
            return

        # Default: fresh boot with new CoW overlay
        self._stop_qemu()  # clean up any stale process

        self._overlay_path = f"/tmp/mypcbench-{self.container_name}-overlay.qcow2"
        pidfile = f"/tmp/mypcbench-{self.container_name}.pid"

        # Fresh CoW overlay from base image
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2",
             "-b", self.qcow2_path, "-F", "qcow2",
             self._overlay_path],
            check=True, capture_output=True,
        )

        # Determine ports
        api_port = self._host_api_port or CONTROL_API_PORT
        vnc_port = self._host_vnc_port or VNC_PORT
        self._host_api_port = api_port
        self._host_vnc_port = vnc_port

        # Build hostfwd string — ports are deterministic
        vnc_display = vnc_port - 5900  # VNC display number
        hostfwd = f"hostfwd=tcp::{api_port}-:5000,hostfwd=tcp::{vnc_port}-:5901"
        for cp in range(3001, 3019):
            hp = int(os.environ.get(f"MYPCBENCH_HOST_APP_PORT_{cp}", cp))
            self._host_app_ports[cp] = hp
            hostfwd += f",hostfwd=tcp::{hp}-:{cp}"
        ssh_port = int(os.environ.get("MYPCBENCH_HOST_SSH_PORT", "2222"))
        hostfwd += f",hostfwd=tcp::{ssh_port}-:22"

        # Detect KVM
        kvm_args = []
        if os.path.exists("/dev/kvm"):
            kvm_args = ["-enable-kvm", "-cpu", "host"]
        else:
            kvm_args = ["-cpu", "qemu64"]
            log.warning("No /dev/kvm — VM will be very slow (TCG emulation)")

        # UEFI firmware (raises with actionable error if OVMF missing)
        code_path, vars_src = self._find_uefi_firmware()
        log.info("OVMF: code=%s vars=%s", code_path, vars_src)
        self._vars_qcow2_path = f"/tmp/mypcbench-{self.container_name}-vars.qcow2"
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-f", "raw",
             vars_src, self._vars_qcow2_path],
            check=True, capture_output=True,
        )
        uefi_args = [
            "-drive", f"if=pflash,format=raw,readonly=on,file={code_path}",
            "-drive", f"if=pflash,format=qcow2,file={self._vars_qcow2_path}",
        ]

        serial_log = f"/tmp/mypcbench-{self.container_name}-serial.log"
        cmd = [
            "qemu-system-x86_64",
            "-name", self.container_name,
            "-machine", "q35,accel=kvm:tcg",
            *kvm_args,
            "-m", "8G",
            "-smp", "4",
            *uefi_args,
            "-drive", f"file={self._overlay_path},format=qcow2,if=virtio,cache=unsafe,aio=threads",
            "-netdev", f"user,id=net0,{hostfwd}",
            "-device", "virtio-net-pci,netdev=net0",
            "-vga", "virtio",
            "-vnc", f":{vnc_display}",
            "-display", "none",
            "-serial", f"file:{serial_log}",
            "-monitor", "none",
        ]

        log.info("Starting QEMU: %s", " ".join(cmd))
        # Use Popen instead of -daemonize (which is incompatible with -display none)
        stderr_path = f"/tmp/mypcbench-{self.container_name}-stderr.log"
        stderr_f = open(stderr_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # The child has its own dup'd fd; close the parent's handle so we
        # don't leak one fd per QEMU boot / inter-task reset.
        stderr_f.close()
        self._qemu_pid = proc.pid

        # Write PID file manually
        with open(pidfile, "w") as f:
            f.write(str(self._qemu_pid))

        # Verify process started successfully (give it a moment)
        time.sleep(2)
        if proc.poll() is not None:
            stderr_msg = ""
            try:
                stderr_msg = open(stderr_path).read().strip()
            except Exception:
                pass
            raise RuntimeError(
                f"QEMU exited immediately with code {proc.returncode}: {stderr_msg}"
            )

        # Export app ports as env vars for in-VM helpers
        for cp, hp in self._host_app_ports.items():
            os.environ[f"MYPCBENCH_HOST_APP_PORT_{cp}"] = str(hp)

        log.info(
            "QEMU started: PID=%s, API=:%s, VNC=:%s, overlay=%s",
            self._qemu_pid, api_port, vnc_port, self._overlay_path,
        )

    def _stop_qemu(self):
        """Kill QEMU process and clean up overlay."""
        # When attached in reuse mode, leave the VM alone
        if os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0") in ("1", "true", "yes"):
            log.info("Skipping QEMU kill for reused VM %s", self.container_name)
            return

        pidfile = f"/tmp/mypcbench-{self.container_name}.pid"

        # Try to read PID from file if we don't have it
        if not self._qemu_pid and os.path.exists(pidfile):
            try:
                self._qemu_pid = int(open(pidfile).read().strip())
            except (ValueError, OSError):
                pass

        if self._qemu_pid:
            try:
                os.kill(self._qemu_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Wait for process to fully exit
            for _ in range(20):
                try:
                    os.kill(self._qemu_pid, 0)
                    try:
                        waited, _status = os.waitpid(self._qemu_pid, os.WNOHANG)
                        if waited:
                            break
                    except ChildProcessError:
                        pass
                    time.sleep(0.5)
                except ProcessLookupError:
                    break
            self._qemu_pid = None

        # Clean up files
        for f in [pidfile, self._overlay_path, self._vars_qcow2_path]:
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    def _reset_qemu(self):
        """Fresh reset: kill QEMU, recreate CoW overlay, reboot.

        Keeps MYPCBENCH_REUSE_CONTAINER=0 across BOTH the stop AND the
        start — otherwise _start_qemu's reuse branch runs, sees no
        pidfile (we just killed it), and raises 'no QEMU process found'.
        Silent no-op until task 2 crashes.  Restore the env var AFTER
        _start_qemu has fully launched the fresh VM.
        """
        # Save port settings before stop
        api_port = self._host_api_port
        vnc_port = self._host_vnc_port
        app_ports = dict(self._host_app_ports)

        # Force stop + start (bypass reuse branch throughout the reset)
        reuse_orig = os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0")
        os.environ["MYPCBENCH_REUSE_CONTAINER"] = "0"
        try:
            self._stop_qemu()

            # Wait for every forwarded port to be released by the kernel.
            # Checking only the Control API port can still leave an app port
            # in TIME_WAIT/listen cleanup, and QEMU then exits immediately
            # with "Could not set up host forwarding rule".
            ports_to_check = [
                api_port or CONTROL_API_PORT,
                vnc_port or VNC_PORT,
                *app_ports.values(),
                int(os.environ.get("MYPCBENCH_HOST_SSH_PORT", "2222")),
            ]
            for attempt in range(90):
                busy = []
                for port in ports_to_check:
                    try:
                        import socket
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.bind(("127.0.0.1", int(port)))
                        s.close()
                    except OSError:
                        busy.append(port)
                if not busy:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"Ports still in use after 90s: {busy}")

            # Restore port settings so _start_qemu uses the same ports
            self._host_api_port = api_port
            self._host_vnc_port = vnc_port
            self._host_app_ports = app_ports

            # Re-start with fresh overlay (still in reuse=0 mode)
            self._start_qemu()
        finally:
            os.environ["MYPCBENCH_REUSE_CONTAINER"] = reuse_orig

    def _discover_ports_qemu(self):
        """Ports are deterministic in QEMU mode — no discovery needed."""
        for cp in range(3001, 3019):
            hp = int(os.environ.get(f"MYPCBENCH_HOST_APP_PORT_{cp}", cp))
            self._host_app_ports[cp] = hp
            os.environ[f"MYPCBENCH_HOST_APP_PORT_{cp}"] = str(hp)

    def _is_qemu_running(self) -> bool:
        """Check if the QEMU process is alive."""
        if not self._qemu_pid:
            pidfile = f"/tmp/mypcbench-{self.container_name}.pid"
            if os.path.exists(pidfile):
                try:
                    self._qemu_pid = int(open(pidfile).read().strip())
                except (ValueError, OSError):
                    return False
            else:
                return False
        try:
            os.kill(self._qemu_pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _wait_for_ready(self, timeout: Optional[int] = None):
        """Wait for the container + every web app to become healthy.

        Default timeout pulled from MYPCBENCH_VM_READY_TIMEOUT (seconds).
        Falls back to 900s so it survives a fresh CoW overlay rebooting
        through firstboot (~10 min to /health on KVM); a shorter default
        can silently wedge the inter-task reset path. Override per-call by
        passing `timeout=N` explicitly.

        Two-phase readiness to keep the runner in sync with VM resets:

        Readiness = the Control API on port 5000 responds to /health
        (the VM's Flask-based OSWorld-compatible API is up; screenshots
        + execute + file I/O work, and the 17 seeded web apps are served
        behind it). Tasks never start against a half-initialized VM.
        """
        if timeout is None:
            timeout = int(os.environ.get("MYPCBENCH_VM_READY_TIMEOUT", "900"))
        start = time.time()
        nudged = False  # one-shot networking nudge after 60s of failures

        # Phase 1: Control API
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.base_url}/health", timeout=5)
                if r.ok:
                    log.info("Control API ready")
                    break
            except Exception:
                pass

            # Active intervention: if health checks have been failing for
            # over 60s the VM's network stack may be stalled. Nudge it
            # once by restarting NetworkManager / networking inside the
            # container (pattern borrowed from OSWorld's runner).
            elapsed = time.time() - start
            if elapsed > 60 and not nudged:
                log.info("Health check slow after 60s -- nudging VM networking")
                try:
                    self._execute_shell(
                        "sudo systemctl restart NetworkManager 2>/dev/null "
                        "|| sudo systemctl restart networking 2>/dev/null "
                        "|| true"
                    )
                except Exception:
                    pass
                nudged = True

            time.sleep(5)
        else:
            raise TimeoutError(f"Control API not ready after {timeout}s")

        # Phase 2: web-app readiness sweep
        self._wait_for_apps_ready(timeout=timeout - (time.time() - start))

        # Phase 3: Harden DNS + verify resolution (non-fatal)
        self._configure_dns()
        self._enforce_resolution()

        # Phase 4: Inject LLM auto-reply key for chat apps (non-fatal)
        self._inject_llm_key()

    # ── Post-boot hardening helpers ────────────────────────────────────

    def _configure_dns(self):
        """Pin DNS to reliable public resolvers. Prevents long-run DNS failures."""
        try:
            self._execute_shell(
                "echo 'nameserver 8.8.8.8\nnameserver 1.1.1.1' | sudo tee /etc/resolv.conf > /dev/null && "
                "sudo chattr +i /etc/resolv.conf 2>/dev/null || true"
            )
            log.debug("DNS hardened to 8.8.8.8 / 1.1.1.1")
        except Exception as e:
            log.debug("DNS hardening skipped: %s", e)

    def _enforce_resolution(self):
        """Verify VM resolution matches expected screen_size. Fix via xrandr if drifted."""
        try:
            result = self._execute_shell(
                "DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority xrandr 2>/dev/null | grep ' connected' | grep -oP '\\d+x\\d+'"
            )
            current = result.get("output", "").strip().split("\n")[0]
            expected = f"{self.screen_width}x{self.screen_height}"
            if current and current != expected:
                log.warning("Resolution drift: VM=%s, expected=%s — fixing via xrandr", current, expected)
                self._execute_shell(
                    f"DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority xrandr -s {expected} 2>/dev/null || true"
                )
            elif current:
                log.debug("Resolution OK: %s", current)
        except Exception as e:
            log.debug("Resolution check skipped: %s", e)

    def _inject_llm_key(self):
        """Inject OPENAI_API_KEY into chat app services for LLM auto-replies.

        Chat apps (buzzchat, workbuzz) use the shared @mypcbench/llm
        client to generate NPC replies via gpt-5.4-mini. The key is read
        from the process environment first, with project .env as a fallback,
        then injected into the running VM's systemd services.
        """
        api_key = (os.environ.get("OPENAI_API_KEY") or "").strip().strip('"')
        if not api_key:
            env_path = Path(__file__).resolve().parent.parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("OPENAI_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"')
                        break
        if not api_key:
            log.debug("No OPENAI_API_KEY in environment or .env — LLM auto-replies disabled")
            return
        try:
            import base64 as _b64
            script = (
                "#!/bin/bash\nset -e\n"
                f"KEY='{api_key}'\n"
                # Write to /etc/mypcbench-env so the in-VM grader
                # (llm_fuzzy_match) can source it at eval time.
                "grep -q OPENAI_API_KEY /etc/mypcbench-env 2>/dev/null && "
                '  sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$KEY|" /etc/mypcbench-env || '
                '  echo "OPENAI_API_KEY=$KEY" >> /etc/mypcbench-env\n'
                # Also inject into chat app systemd services for NPC replies.
                "for svc in mypcbench-buzzchat mypcbench-workbuzz; do\n"
                "  F=/etc/systemd/system/${svc}.service\n"
                '  [ -f "$F" ] || continue\n'
                '  sed -i "/OPENAI_API_KEY/d" "$F"\n'
                '  sed -i "/^ExecStart/i Environment=OPENAI_API_KEY=$KEY" "$F"\n'
                "done\n"
                "systemctl daemon-reload\n"
                "systemctl restart mypcbench-buzzchat mypcbench-workbuzz 2>/dev/null || true\n"
            )
            b64 = _b64.b64encode(script.encode()).decode()
            self._execute_shell(
                f"echo {b64} | base64 -d > /tmp/_llm_setup.sh && sudo bash /tmp/_llm_setup.sh"
            )
            log.info("LLM auto-reply key injected into chat apps")
        except Exception as e:
            log.warning("LLM key injection failed (non-fatal): %s", e)

    def _wait_for_apps_ready(self, timeout: float = 120.0):
        """No-op: web-app readiness is covered by the Control API health
        check in reset(); this stub keeps the call site stable."""
        return

    def _stop_container(self):
        if self.backend == "qemu":
            return self._stop_qemu()
        # When attached in reuse mode, leave the container alone — it was
        # created by the parent (parallel runner) and will be torn down there.
        if os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0") in ("1", "true", "yes"):
            log.info("Skipping docker rm for reused container %s", self.container_name)
            return
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
        )

    # ------------------------------------------------------------------
    # OSWorld-compatible API: reset / step / _get_obs / evaluate
    # ------------------------------------------------------------------

    # Every app in the VM, in canonical port order. Each entry is
    # (db_name, app, port, init_path). Hitting `init_path` with a signed
    # session cookie triggers the app's first-run migration + backfill
    # into the per-VM /data/<db>.sqlite — for apps that don't do lazy
    # migration it's still a cheap health-check that confirms the service
    # is up and responsive before the agent needs it.
    #
    # Must run BEFORE the agent starts so every app is consistent:
    # populated for apps that start empty, responsive for apps that don't.
    # Warming all 17 instead of just the known-lazy subset is safer — we
    # won't silently regress when a new app adds lazy init later.
    _LAZY_DB_WARMUPS = [
        ("vaultbank.sqlite",        "vaultbank",       3001, "/api/accounts"),
        ("batbucks.sqlite",         "batbucks",        3002, "/api/portfolio"),
        ("oddsmarket.sqlite",       "oddsmarket",      3003, "/api/markets"),
        ("buzzchat.sqlite",         "buzzchat",        3004, "/api/conversations"),
        ("workbuzz.sqlite",         "workbuzz",        3005, "/api/channels"),
        ("etaxi.sqlite",            "etaxi",           3006, "/api/rides"),
        ("hangrydash.sqlite",       "hangrydash",      3007, "/api/orders"),
        ("tablefind.sqlite",        "tablefind",       3008, "/api/reservations"),
        ("kwik-e-mart.sqlite",      "kwik-e-mart",     3009, "/api/orders"),
        ("hoolishop.sqlite",        "hoolishop",       3010, "/api/orders"),
        ("dinoco-airlines.sqlite",  "dinoco-airlines", 3011, "/api/flights"),
        ("cheskepdia.sqlite",       "cheskepdia",      3012, "/api/bookings"),
        ("sprintboard.sqlite",      "sprintboard",     3013, "/api/projects"),
        ("lockedin.sqlite",         "lockedin",        3014, "/api/posts"),
        ("speedtax.sqlite",         "speedtax",        3015, "/api/returns"),
        ("mail.sqlite",             "mail",            3016, "/api/emails"),
        ("hoolicalendar.sqlite",    "hoolicalendar",   3017, "/api/events?start=2026-01-01&end=2026-12-31"),
    ]

    # Per-app post-warmup assertion: SQL that must return ≥1 row before we
    # consider the warmup successful. Mirrors the canonical persona, so the
    # check fails loudly if a future image regression empties any of these.
    # Templates below are filled in with ``self.persona_email`` at runtime so
    # different personas work without editing this file.
    _LAZY_DB_ASSERTION_TEMPLATES = {
        "vaultbank.sqlite":       "SELECT 1 FROM accounts WHERE user_email='{email}' LIMIT 1",
        "batbucks.sqlite":        "SELECT 1 FROM holdings WHERE user_email='{email}' LIMIT 1",
        "oddsmarket.sqlite":      "SELECT 1 FROM positions WHERE user_email='{email}' LIMIT 1",
        "buzzchat.sqlite":        "SELECT 1 FROM contacts WHERE user_email='{email}' LIMIT 1",
        "workbuzz.sqlite":        "SELECT 1 FROM channels LIMIT 1",
        "etaxi.sqlite":           "SELECT 1 FROM rides WHERE user_email='{email}' LIMIT 1",
        "hangrydash.sqlite":      "SELECT 1 FROM orders WHERE user_email='{email}' AND placed_at IS NOT NULL LIMIT 1",
        "tablefind.sqlite":       "SELECT 1 FROM reservations WHERE user_email='{email}' LIMIT 1",
        "kwik-e-mart.sqlite":     "SELECT 1 FROM orders WHERE user_email='{email}' LIMIT 1",
        "hoolishop.sqlite":       "SELECT 1 FROM orders WHERE user_email='{email}' LIMIT 1",
        "dinoco-airlines.sqlite": "SELECT 1 FROM flights WHERE user_email='{email}' LIMIT 1",
        "cheskepdia.sqlite":      "SELECT 1 FROM bookings WHERE user_email='{email}' LIMIT 1",
        "sprintboard.sqlite":     "SELECT 1 FROM projects LIMIT 1",
        "lockedin.sqlite":        "SELECT 1 FROM posts LIMIT 1",
        "speedtax.sqlite":        "SELECT 1 FROM tax_returns WHERE user_email='{email}' LIMIT 1",
        "mail.sqlite":            "SELECT 1 FROM emails LIMIT 1",
        "hoolicalendar.sqlite":   "SELECT 1 FROM events WHERE user_email='{email}' LIMIT 1",
    }

    @property
    def _LAZY_DB_ASSERTIONS(self) -> Dict[str, str]:
        """Fill assertion templates with the resolved persona email."""
        email = self.persona_email
        return {db: tmpl.format(email=email) for db, tmpl in self._LAZY_DB_ASSERTION_TEMPLATES.items()}

    def _warmup_one_app(self, app: str, port: int, path: str, timeout: int = 60) -> bool:
        """Hit one app's bootstrap endpoint via signed session cookie.
        Returns True if the request returned a 2xx, False otherwise.
        """
        warmup_script = (
            f"python3 -c \"import hmac,hashlib,base64,json,urllib.request;"
            f"p=json.dumps({{'email':'{self.persona_email}','app':'{app}'}});"
            f"s=hmac.new(b'mypcbench-session-2026',p.encode(),hashlib.sha256).hexdigest();"
            f"tok=base64.b64encode((p+'.'+s).encode()).decode();"
            f"r=urllib.request.Request('http://localhost:{port}{path}',"
            f"headers={{'Cookie':'session_{app}='+tok}});"
            f"urllib.request.urlopen(r, timeout={timeout}).read();"
            f"print('OK')\" 2>&1"
        )
        try:
            result = self._execute_shell(warmup_script)
            output = (result or {}).get("output", "") if isinstance(result, dict) else str(result)
            return "OK" in output
        except Exception:
            return False

    def _assert_lazy_db_populated(self, db_name: str) -> bool:
        """Run the per-app assertion query. Returns True if it returned ≥1 row."""
        query = self._LAZY_DB_ASSERTIONS.get(db_name)
        if not query:
            return True  # no assertion defined → don't block
        try:
            result = self._execute_shell(
                f"sqlite3 /data/{db_name} \"{query};\" 2>&1"
            )
            output = (result or {}).get("output", "") if isinstance(result, dict) else str(result)
            return output.strip() == "1"
        except Exception:
            return False

    def _prewarm_lazy_dbs(self, subset: Optional[set] = None, max_retries: int = 3) -> None:
        """Hit each app's bootstrap endpoint AND verify the per-VM DB is
        populated afterward. Retries up to `max_retries` times per app
        before giving up. Logs (but doesn't raise) if an app's assertion
        fails after all retries — production callers can decide whether
        to fail-fast or proceed.

        If `subset` is given (set of db filenames like `{"hoolicalendar.sqlite"}`),
        only warm the apps whose DB is in that set. Otherwise warm every
        app in `_LAZY_DB_WARMUPS`.
        """
        failed_apps: list[str] = []
        for db_name, app, port, path in self._LAZY_DB_WARMUPS:
            if subset is not None and db_name not in subset:
                continue
            ok = False
            for attempt in range(max_retries):
                self._warmup_one_app(app, port, path)
                if self._assert_lazy_db_populated(db_name):
                    ok = True
                    break
                # Cold-start migrations can be slow; back off and retry.
                time.sleep(2 + attempt * 3)
            if not ok:
                failed_apps.append(app)
                log.warning(
                    "Lazy DB warmup failed after %d retries: app=%s db=%s",
                    max_retries, app, db_name,
                )
        if failed_apps:
            log.error(
                "Pre-warm finished with %d apps still empty: %s — "
                "the agent may see inconsistent state for these apps. "
                "Consider increasing the per-app retry timeout.",
                len(failed_apps), failed_apps,
            )

        # Backfill seed-data fields left at their default in the shipped image
        # but referenced by task grading. Only runs when dinoco-airlines is
        # in scope; the UPDATE is guarded by `fare_paid=0` so it's a no-op
        # for any row the agent may have mutated during the run.
        if subset is None or "dinoco-airlines.sqlite" in subset:
            try:
                self._execute_shell(
                    "sqlite3 /data/dinoco-airlines.sqlite "
                    "\"UPDATE flights SET fare_paid=512 WHERE flight_number='AA1482' AND fare_paid=0; "
                    "UPDATE flights SET fare_paid=387 WHERE flight_number='DL1358' AND fare_paid=0; "
                    "UPDATE flights SET fare_paid=289 WHERE flight_number='AS324' AND fare_paid=0;\" "
                    "2>/dev/null; true"
                )
            except Exception:
                pass

    def reset(self, task_config: Optional[Dict] = None, soft: bool = False) -> Dict[str, Any]:
        """Reset the environment for a new task.

        Default behavior (matches OSWorld): destroy and recreate the container
        for full isolation between tasks. This is the standard benchmark approach.

        Two modes:
          soft=False (default): Destroy and recreate container (~20s). Full isolation.
                                Same as OSWorld's Docker provider revert_to_snapshot.
          soft=True:            Keep container, clean up files and state (~2s).
                                Faster but less isolated. Use --soft-reset flag.

        Set self.soft_reset=True to use soft reset for all tasks.
        """
        self._traj_no += 1
        self._step_no = 0
        self.action_history.clear()

        use_soft = soft or getattr(self, 'soft_reset', False)
        reuse = os.environ.get("MYPCBENCH_REUSE_CONTAINER", "0") in ("1", "true", "yes")

        if not self._host_api_port:
            # First call: attach (reuse) or start a new container.
            self._start_container()
            self._wait_for_ready()
            # Warm all lazy DBs before the agent's first interaction so
            # the agent sees consistent state across apps it never touches.
            self._prewarm_lazy_dbs()
            self.is_environment_used = False
        elif self.is_environment_used and use_soft:
            # Soft reset: clean up files, kill stale processes, keep container
            self._soft_reset()
            # Soft reset keeps the container, so DBs stay warm from the
            # previous task — no re-warmup needed.
        elif self.is_environment_used:
            # Hard reset (OSWorld parity: full state revert).
            #
            # Reuse mode (parallel/multi-agent runners pre-boot the
            # container): `docker restart <name>`. For the QEMU wrapper,
            # the entrypoint rebuilds the CoW overlay from
            # /baseline/mypcbench.qcow2 on every start → this gives a
            # clean-state boot equivalent to OSWorld's Docker-provider
            # revert_to_snapshot. Ports and volumes are preserved.
            #
            # Standalone mode (env.py owns the container): full
            # `docker rm -f` + `docker run -d` matching OSWorld's
            # stop_emulator + start_emulator sequence.
            #
            # Retry loop (robustness fix): Docker restart or container
            # boot can transiently fail on loaded hosts. Retry up to 3
            # times with a short back-off before giving up.
            MAX_RESET_RETRIES = 3
            for reset_attempt in range(MAX_RESET_RETRIES):
                try:
                    if self.backend == "qemu":
                        # Hard reset is the default for every QEMU mode:
                        # CoW overlay rebuild gives a clean state revert,
                        # matching OSWorld's Docker-provider semantics.
                        # Works in both standalone and reuse (parallel)
                        # layouts — the subprocess sits on the
                        # same node as QEMU and owns the pidfile/overlay.
                        log.info("Hard reset via QEMU CoW overlay rebuild")
                        self._reset_qemu()
                        self._wait_for_ready()
                    elif reuse:
                        log.info("Hard reset via `docker restart %s` (reuse mode, QEMU CoW overlay rebuild)", self.container_name)
                        subprocess.run(
                            ["docker", "restart", self.container_name],
                            capture_output=True,
                            check=False,
                        )
                        # Preserve the port map — restart keeps it, so no
                        # rediscovery is needed.
                        self._wait_for_ready()
                    else:
                        self._start_container()
                        self._wait_for_ready()
                    break  # success
                except Exception as e:
                    log.warning("Reset attempt %d/%d failed: %s",
                                reset_attempt + 1, MAX_RESET_RETRIES, e)
                    if reset_attempt < MAX_RESET_RETRIES - 1:
                        time.sleep(5)
                    else:
                        raise  # final attempt failed
            # Fresh container → all lazy DBs start empty. Warm them now
            # so the agent can trust the app state from step 1.
            self._prewarm_lazy_dbs()
            self.is_environment_used = False

        if task_config:
            self.instruction = task_config.get("instruction", "")
            if not self.instruction:
                task_map = task_config.get("task", {})
                self.instruction = task_map.get("en", "") if isinstance(task_map, dict) else str(task_map)
            self.config = task_config

            # Run pre_command if present
            pre_cmd = task_config.get("pre_command", "")
            if pre_cmd:
                self._execute_shell(pre_cmd)

            self.is_environment_used = True

        obs = self._get_obs()
        return obs

    def _soft_reset(self):
        """Clean up container state without restarting it.

        Removes task-specific files, kills stale processes, and resets
        the desktop to a clean state. Much faster than container restart.
        """
        cleanup_commands = [
            # Remove deliverables from previous task
            "rm -rf /home/user/deliverables/*",
            # Remove staged reference files from previous task
            "rm -rf /home/user/Documents/reference_files/*",
            # Kill any stale user processes (Python scripts, LibreOffice instances)
            "pkill -u user -f 'python3 -c' 2>/dev/null || true",
            "pkill -u user -f 'soffice' 2>/dev/null || true",
            # Close any open GUI windows except the desktop essentials
            # GDM-autologin session lives on :0 with the Xauthority cookie
            # at /run/user/1000/gdm/Xauthority — same pattern as
            # _execute_pyautogui. Without these, wmctrl silently no-ops
            # (`|| true` swallowed the XauthError) and stale windows
            # leak across tasks in --soft-reset mode.
            "DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority wmctrl -c 'Mozilla Firefox' 2>/dev/null || true",
            "DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority wmctrl -c 'LibreOffice' 2>/dev/null || true",
            "DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority wmctrl -c 'Terminal' 2>/dev/null || true",
        ]
        for cmd in cleanup_commands:
            try:
                self._execute_command(cmd, shell=True)
            except Exception:
                pass
        log.info("Soft reset complete")

    def step(self, action, pause: float = 2.0) -> Tuple[Dict, float, bool, Dict]:
        """Execute an action and return (obs, reward, done, info)."""
        self._step_no += 1
        self.action_history.append(action)

        done = False
        info = {}
        reward = 0.0

        # Handle special actions
        if isinstance(action, str):
            action_upper = action.strip().upper()
            if action_upper == "WAIT":
                time.sleep(pause)
                return self._get_obs(), reward, False, info
            elif action_upper == "DONE":
                return self._get_obs(), reward, True, {"done": True}
            elif action_upper == "FAIL":
                return self._get_obs(), reward, True, {"fail": True}

        # Execute the action
        if self.action_space == "pyautogui":
            if isinstance(action, str):
                fixed = _fix_pyautogui_less_than_bug(action)
                self._execute_pyautogui(fixed)
            elif isinstance(action, dict):
                code = self._action_dict_to_pyautogui(action)
                if code:
                    self._execute_pyautogui(code)
        elif self.action_space == "computer_13":
            # OSWorld-API parity only. The shipped paper agents all emit
            # pyautogui actions, so the runner never selects computer_13
            # (the --action_space flag is a documented no-op); kept so the
            # env stays a drop-in OSWorld-compatible Env.
            if isinstance(action, dict):
                code = self._action_dict_to_pyautogui(action)
                if code:
                    self._execute_pyautogui(code)

        time.sleep(pause)
        obs = self._get_obs()
        return obs, reward, done, info

    def _get_obs(self) -> Dict[str, Any]:
        """Get current observation (screenshot + optional a11y tree)."""
        screenshot = self._get_screenshot()
        return {
            "screenshot": screenshot,
            "accessibility_tree": None,
            "terminal": None,
            "instruction": self.instruction,
        }


    def close(self):
        """Clean up the container."""
        try:
            self._stop_container()
        except Exception as e:
            log.warning("Error stopping container: %s", e)
        finally:
            self._session.close()

    # ------------------------------------------------------------------
    # Controller methods (OSWorld PythonController-compatible)
    # ------------------------------------------------------------------

    def _get_screenshot(self) -> Optional[bytes]:
        """GET /screenshot → raw PNG bytes."""
        for attempt in range(3):
            try:
                r = self._session.get(
                    f"{self.base_url}/screenshot", timeout=30
                )
                if r.ok and r.content[:4] == b"\x89PNG":
                    return r.content
            except Exception:
                pass
            time.sleep(3)
        return None

    def _execute_pyautogui(self, code: str) -> Optional[Dict]:
        """Execute pyautogui code on the VM via /execute.

        pyautogui resolves DISPLAY + XAUTHORITY at import time. The
        GDM-autologin session stores its cookie at
        /run/user/1000/gdm/Xauthority, not ~/.Xauthority, so we must
        export XAUTHORITY before `import pyautogui` or the Xlib client
        errors out with `[Errno 2] No such file or directory:
        '/home/user/.Xauthority'` and every click silently no-ops.
        """
        wrapped = (
            "import os; "
            "os.environ.setdefault('DISPLAY', ':0'); "
            "os.environ.setdefault('XAUTHORITY', '/run/user/1000/gdm/Xauthority'); "
            "import pyautogui; import time; "
            "pyautogui.FAILSAFE = False; pyautogui.PAUSE = 0; "
            + code
        )
        return self._execute_command(
            ["python3", "-c", wrapped], shell=False
        )

    def _execute_shell(self, command: str) -> Dict:
        """Execute a shell command on the VM."""
        return self._execute_command(command, shell=True)

    def _execute_command(self, command, shell=False, retries=3) -> Dict:
        """POST /execute with retry (matches OSWorld's PythonController)."""
        for attempt in range(retries):
            try:
                r = self._session.post(
                    f"{self.base_url}/execute",
                    json={"command": command, "shell": shell},
                    timeout=120,
                )
                r.raise_for_status()
                return r.json()
            except requests.exceptions.ReadTimeout:
                log.warning("Command timed out after 120s (attempt %d/%d)", attempt + 1, retries)
                return {"returncode": -1, "output": "", "error": "Command timed out after 120s"}
            except Exception as e:
                log.warning("Execute failed (attempt %d/%d): %s", attempt + 1, retries, e)
                if attempt < retries - 1:
                    time.sleep(5)
        return {"returncode": -1, "output": "", "error": "All retries failed"}

    def _action_dict_to_pyautogui(self, action: Dict) -> str:
        """Convert a structured action dict to pyautogui code (computer_13).

        OSWorld-API parity helper — only reached via the computer_13
        action space, which the shipped paper agents never select.
        """
        action_type = action.get("action_type", "")
        params = action.get("parameters", action)

        if action_type == "CLICK":
            x, y = params.get("x", 0), params.get("y", 0)
            button = params.get("button", "left")
            clicks = params.get("num_clicks", 1)
            return f"pyautogui.click(x={x}, y={y}, button='{button}', clicks={clicks})"
        elif action_type == "MOVE_TO":
            return f"pyautogui.moveTo({params['x']}, {params['y']})"
        elif action_type == "RIGHT_CLICK":
            return f"pyautogui.click(x={params['x']}, y={params['y']}, button='right')"
        elif action_type == "DOUBLE_CLICK":
            return f"pyautogui.doubleClick(x={params['x']}, y={params['y']})"
        elif action_type == "DRAG_TO":
            return f"pyautogui.dragTo({params['x']}, {params['y']}, duration=0.5)"
        elif action_type == "SCROLL":
            return f"pyautogui.scroll({params.get('dy', 0)}, x={params.get('x', 0)}, y={params.get('y', 0)})"
        elif action_type == "TYPING":
            return f"pyautogui.typewrite({repr(params['text'])}, interval=0.02)"
        elif action_type == "PRESS":
            return f"pyautogui.press({repr(params['key'])})"
        elif action_type == "KEY_DOWN":
            return f"pyautogui.keyDown({repr(params['key'])})"
        elif action_type == "KEY_UP":
            return f"pyautogui.keyUp({repr(params['key'])})"
        elif action_type == "HOTKEY":
            keys = params.get("keys", [])
            return f"pyautogui.hotkey({', '.join(repr(k) for k in keys)})"
        elif action_type in ("WAIT", "DONE", "FAIL"):
            return ""
        else:
            log.warning("Unknown action_type: %s", action_type)
            return ""

    # ------------------------------------------------------------------
    # Recording (stubs for OSWorld compatibility)
    # ------------------------------------------------------------------

    class controller:
        """Stub controller for OSWorld recording compatibility."""
        @staticmethod
        def start_recording():
            pass

        @staticmethod
        def end_recording(path: str = ""):
            pass
