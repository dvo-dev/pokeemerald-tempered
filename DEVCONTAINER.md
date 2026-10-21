# Dev container

The repository includes a dev container for VS Code and GitHub Codespaces as an alternative to installing the build dependencies on your host.
It provides Ubuntu 24.04, the ARM GCC toolchain, GitHub CLI (`gh`), GitHub Copilot CLI, and the dependencies needed by the bundled headless mGBA test runner.

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

Copilot CLI's `/new worktree` and `/worktree` create Git worktrees in a folder next to the checkout, `/workspaces/<repository>.worktrees`.
`/workspaces` itself is owned by `root`, so container setup creates that folder and gives it to the `vscode` user.
The folder lives inside the container, so worktrees in it are not kept when the container is rebuilt; push branches you want to keep, and run `git worktree prune` inside the container to clear stale entries afterwards.

## Host architecture

The image is built and run as `linux/amd64` because the bundled Linux test runner is x86-64.
It runs natively on x86-64 Docker hosts and through emulation on ARM64 hosts, including Apple Silicon; no host-detection script is needed.
Docker Desktop includes emulation support. Standalone Docker Engine on ARM64 Linux requires QEMU/binfmt support configured on the Docker host.
Compilation and testing under emulation can be significantly slower.

The container runs Linux, not macOS. The [native macOS instructions](docs/install/mac/MAC_OS.md) are a separate host-based setup and are not needed inside the container.
For local development, desktop tools such as mGBA and Porymap can remain on the host and open files from the shared checkout.
