# Dev container

The repository includes a dev container for VS Code and GitHub Codespaces as an alternative to installing the build dependencies on your host.
It provides Ubuntu 24.04, the ARM GCC toolchain, GitHub CLI (`gh`), GitHub Copilot CLI, the dependencies needed by the bundled headless mGBA test runner, and a separate Python mGBA runtime for in-game QA.

## Local development

1. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), and the VS Code Dev Containers extension.
2. Clone this repository with Git, rather than downloading a ZIP, and open the repository folder in VS Code.
3. Run **Dev Containers: Reopen in Container** from the Command Palette.
4. Wait for setup to finish. The container rebuilds the repository's build and test tools, including any previously compiled for a different host.
   These binaries are stored in the shared checkout and replace any host-built copies. When switching between host and container builds, run `make -B tools check-tools` in the environment you switch to; reopening an existing container does not repeat this setup.

Inside the container, run:

```console
make -j$(nproc)
make debug -j$(nproc)
make check -j$(nproc)
```

These commands build `pokeemerald.gba`, build with debug symbols, and run the tests, respectively.
To apply changes to the container configuration, run **Dev Containers: Rebuild Container**.
GitHub Codespaces uses the same configuration without requiring a local Docker installation.

## In-game QA runtime

The image builds official mGBA Python bindings at commit
`fd9dc5c2cbf234365068855851107e762fc6211b`, with a small documented build-time
patch for callback initialization, matching cffi/native definitions, and private
library resolution. See [patch provenance](tools/qa/patches/README.md).
Normal FFmpeg support is enabled to avoid additional EReader header patches.
Qt, SDL, OpenGL, and scripting are disabled. The existing bundled native
`make check` runner is neither replaced nor pointed at this private library.

Build dependencies stay in a builder stage. The final image contains the
private `/opt/qa-venv`, `/opt/mgba`, FFmpeg/ffprobe, provenance/static-check logs,
MPL-2.0 notice and modified binding sources. It does not change global linker
configuration, install into system Python, or require Docker/Podman inside the
container. The build context allowlist excludes ROMs, saves, sessions, credentials,
and run artifacts.

After rebuilding, run `qa --help` from a checkout containing `tools/qa`.
See [the QA guide](tools/qa/README.md) for `qa build`, RAM-backed smoke flows,
stateless checkpoint stepping, recording, failure interpretation, and publication
boundaries. Run evidence identifies `image` versus `ad-hoc` runtime provenance;
an ad-hoc build does not prove the final image works.

FFmpeg and Python dependencies increase the runtime image footprint; CMake and
FFmpeg development headers are not copied from the builder. Measure the actual
rebuilt image size/build output on the Docker host rather than inferring it from
the development environment. A Codespaces prebuild can cache this work, but this
repository does not enable or prescribe a prebuild policy.

### Preserve sessions before rebuilding

A rebuild stops the current CLI and subagents. `/resume` needs retained session
data; it is not a backup service. Before rebuilding, verify a private durable
backup/persistence mechanism for the required Copilot session/history databases,
planning artifacts, and uncommitted work. Do not copy credentials into the
repository or Docker build context. Migrate existing data before mounting a
new volume over its old location, and verify ownership/readability afterward.

Also ensure the QA branch's files are present in the **host-visible checkout**
VS Code opens. A worktree created only in the container's writable overlay can
disappear and may not be visible to the host's image builder. A workspace path
alone does not establish persistence. Do not discard the old container until
the backup, source checkout, and resumption path have been checked.

The agent prepares the image changes and pauses for a host-side
**Dev Containers: Rebuild Container**. Do not add a nested engine, mount the host
Docker socket, or silently replace a missing image-validation environment.
Record the actual host image ID and command results when validating the rebuilt
image; a successful Dockerfile edit or source build alone is insufficient.

## GitHub CLI authentication

GitHub CLI (`gh`) is installed through the official `ghcr.io/devcontainers/features/github-cli:latest` Feature in `devcontainer.json` and is available to the `vscode` user.
For local VS Code devcontainers, configure GitHub CLI authentication once **on the host**:

```console
gh auth login --hostname github.com
gh auth setup-git --hostname github.com
```

If the host is already authenticated, skip `gh auth login`. Use host secure credential storage rather than accepting a plaintext fallback.
The second command makes the host's GitHub CLI available as a Git HTTPS credential helper; it is not run automatically by this repository.
Rebuild/reopen the devcontainer through VS Code after configuring the host helper.

Each new interactive Bash terminal retrieves the credential through VS Code's forwarded Git credential helper and exports `GH_TOKEN` for that terminal and its child processes.
Existing non-empty `GH_TOKEN` or `GITHUB_TOKEN` values are preserved, including authentication supplied by Codespaces.
Check authentication inside the container without displaying the token:

```console
gh auth status --hostname github.com
```

If forwarding fails, returns no credential, or takes longer than ten seconds, startup prints a warning and leaves the terminal usable without setting `GH_TOKEN`.
Check host authentication and helper configuration, reopen through VS Code, and start a new terminal to retry.
SSH-agent forwarding alone does not provide an API token.

This initialization applies only to interactive Bash terminals and commands launched from them, not independent VS Code tasks, services, or non-interactive `docker exec` commands.
The token is retrieved at runtime, not embedded in the image or saved to a credential file by this hook.
It remains in the terminal's environment and is accessible to its child processes and other sufficiently privileged processes, so only use credential forwarding with trusted containers.
After changing or refreshing host credentials, open a new terminal; existing terminals retain their previous token.

## GitHub Copilot CLI

The official `ghcr.io/devcontainers/features/copilot-cli:latest` Feature in `devcontainer.json` installs GitHub Copilot CLI, making `copilot` available to the `vscode` user without a separate Node.js installation.
With its default `latest` version setting, this Feature also checks for Copilot CLI updates when the container starts.
Run it from a container terminal:

```console
copilot
```

Copilot uses the same forwarded `GH_TOKEN`; a separate `/login` in the container is not needed.
Use a host GitHub CLI OAuth login, not a classic personal access token (`ghp_`), which Copilot CLI does not support.
The account must have Copilot access, and organization or enterprise policy must also allow Copilot CLI.
If Copilot still requests login, check the GitHub CLI authentication setup above rather than accepting plaintext token storage inside the container.

## Host architecture

The image is built and run as `linux/amd64` because the bundled Linux test runner is x86-64.
It runs natively on x86-64 Docker hosts and through emulation on ARM64 hosts, including Apple Silicon; no host-detection script is needed.
Docker Desktop includes emulation support. Standalone Docker Engine on ARM64 Linux requires QEMU/binfmt support configured on the Docker host.
Compilation and testing under emulation can be significantly slower.

The container runs Linux, not macOS. The [native macOS instructions](docs/install/mac/MAC_OS.md) are a separate host-based setup and are not needed inside the container.
For local development, desktop tools such as mGBA and Porymap can remain on the host and open files from the shared checkout.
