"""AppleContainerEnvironment with host directory bind-mount support.

harbor 0.2.0's AppleContainerEnvironment.start() hardcodes exactly three
bind mounts (agent/verifier/artifacts log dirs) and has no CLI-level
equivalent of Docker's --mounts-json for apple-container. Apple's own
`container` CLI supports `-v host:target` mounts fine (confirmed via
`container run --help`); harbor's wrapper just never passes one through.

This subclass adds a `mount` constructor kwarg (wired up via harbor's
`--environment-import-path` + `--ek mount=...` extension points, see
harbor.environments.factory.create_environment_from_config) so a real host
directory can be bind-mounted into an otherwise-unmodified apple-container
trial. Format: comma-separated `host_path:container_path` pairs, e.g.
`--ek mount=/Users/me/some-repo:/workspace`.

start() is a full copy of the parent implementation (see
harbor/environments/apple_container.py::AppleContainerEnvironment.start)
with one addition -- extra -v flags before the image name -- because the
parent builds and runs the container in a single method with no override
seam. Keep this in sync if harbor's pin ever moves off 0.2.0.
"""

from harbor.environments.apple_container import AppleContainerEnvironment
from harbor.models.trial.paths import EnvironmentPaths


class MountedAppleContainerEnvironment(AppleContainerEnvironment):
    def __init__(self, *args, mount: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_mounts: list[tuple[str, str]] = []
        if mount:
            for pair in mount.split(","):
                host_path, _, container_path = pair.partition(":")
                if not host_path or not container_path:
                    raise ValueError(
                        f"Invalid mount spec {pair!r}; expected host_path:container_path"
                    )
                self._extra_mounts.append((host_path, container_path))

    async def start(self, force_build: bool):
        self._use_prebuilt = not force_build and bool(self.task_env_config.docker_image)

        if not self._use_prebuilt:
            lock = self._image_build_locks.setdefault(
                self.environment_name, __import__("asyncio").Lock()
            )
            async with lock:
                await self._run_container_command(
                    [
                        "build",
                        "-t",
                        self._image_name,
                        "-f",
                        str((self.environment_dir / "Dockerfile").resolve().absolute()),
                        str(self.environment_dir.resolve().absolute()),
                    ],
                    timeout_sec=int(self.task_env_config.build_timeout_sec),
                )

        image: str = (
            self.task_env_config.docker_image
            if self._use_prebuilt and self.task_env_config.docker_image
            else self._image_name
        )

        try:
            await self._run_container_command(
                ["stop", self._container_name], check=True
            )
        except RuntimeError:
            pass
        try:
            await self._run_container_command(["rm", self._container_name], check=True)
        except RuntimeError:
            pass

        run_cmd: list[str] = ["run", "-d", "--name", self._container_name]

        run_cmd.extend(["-c", str(self.task_env_config.cpus)])
        run_cmd.extend(["-m", f"{self.task_env_config.memory_mb}M"])

        mounts = {
            str(self.trial_paths.verifier_dir.resolve().absolute()): str(
                EnvironmentPaths.verifier_dir
            ),
            str(self.trial_paths.agent_dir.resolve().absolute()): str(
                EnvironmentPaths.agent_dir
            ),
            str(self.trial_paths.artifacts_dir.resolve().absolute()): str(
                EnvironmentPaths.artifacts_dir
            ),
        }
        for host_path, container_path in mounts.items():
            run_cmd.extend(["-v", f"{host_path}:{container_path}"])

        for host_path, container_path in self._extra_mounts:
            run_cmd.extend(["-v", f"{host_path}:{container_path}"])

        run_cmd.append(image)
        run_cmd.extend(["sh", "-c", "sleep infinity"])

        await self._run_container_command(run_cmd)

        await self.exec(
            f"chmod 777 {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}"
        )
