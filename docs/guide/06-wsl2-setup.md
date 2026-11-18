# Setting up on Windows with WSL2 (Ubuntu)

ToolForge is developed and run inside WSL2 so that paths, line endings, shell scripts, Docker and the `Makefile` all behave like
Linux. Everything below happens in the Ubuntu terminal unless it says PowerShell. Nothing here is specific to this project except
the last two sections; the rest is the standard setup, kept in one place so you do it once.

## 1. Check WSL2 and Ubuntu (PowerShell)

```
wsl --status
wsl -l -v
```

`VERSION` for your Ubuntu distribution must be `2`. If not: `wsl --set-version <DistroName> 2`. Ubuntu 24.04 is the easiest
choice because it ships Python 3.12.

## 2. Keep the repository on the Linux side

Create it under your Linux home, for example `~/projects/toolforge`, **not** under `/mnt/c/...`. Files on the Windows drive are slow
for git and tests, lose Unix permissions (the signing keys of S01 need mode 0600) and invite line-ending trouble. To open the folder
in Windows Explorer use `\\wsl.localhost\Ubuntu\home\<you>\projects`; to open it in VS Code run `code .` from the Ubuntu terminal
(VS Code's WSL extension does the rest).

## 3. System packages and Python 3.12

```
sudo apt update && sudo apt install -y git build-essential python3-venv python3-pip dos2unix
python3 --version
```

If it prints 3.12 or newer you are done. On Ubuntu 22.04 it prints 3.10; install 3.12 with the deadsnakes PPA or with `uv`
(`uv python install 3.12`), then use that interpreter below. Python 3.12 is required.

Ubuntu 24.04 refuses `pip install` outside a virtual environment ("externally-managed-environment"). That is expected. Always work
inside one:

```
cd ~/projects/toolforge
python3 -m venv .venv
. .venv/bin/activate
```

`.venv/` is git-ignored. Activate it in every new terminal before running `pip`, `pytest` or `toolforge`.

## 4. Docker

Real sandboxes need a Docker daemon your user can reach. Unit tests do not (they use `FakeRunner`), so you can start building
before this works. Pick one:

* **Docker Desktop** with WSL integration: in Docker Desktop, Settings, Resources, WSL integration, switch on your Ubuntu
  distribution. Docker Desktop must be running whenever you use ToolForge.
* **Docker Engine inside Ubuntu**: follow the official instructions for Ubuntu at `docs.docker.com/engine/install/ubuntu/`, then
  `sudo usermod -aG docker $USER` and open a new terminal.

Verify (all three must work without `sudo`):

```
docker version
docker run --rm hello-world
groups | tr ' ' '\n' | grep -x docker
```

Starting with P04, `toolforge doctor` checks the same things and tells you what is missing. The first sandbox run pulls
`python:3.12-slim`, so it needs internet once.

## 5. Git settings (once)

```
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main
git config --global core.autocrlf false
```

`core.autocrlf false` matters: CRLF line endings break `git am`, the deterministic package hashes and shell scripts. The repository's
`.gitattributes` and `.editorconfig` already ask for LF.

To push to GitHub from WSL, either run `gh auth login` (GitHub CLI) or add an SSH key to your account. Do not paste tokens into
files in the repository.

## 6. Applying a patch you received

The coding agent gives you a `.patch` file. Keep patch files **outside** the repository (`*.patch` is git-ignored anyway):

```
mkdir -p ~/patches
cp /mnt/c/Users/<WindowsUser>/Downloads/0001-p01-skeleton.patch ~/patches/
cd ~/projects/toolforge
git status                                # must be clean, on main
git am --check ~/patches/0001-p01-skeleton.patch
git am ~/patches/0001-p01-skeleton.patch
```

If `git am --check` complains, work down this list:

| Symptom | Cause and fix |
| --- | --- |
| "patch does not apply" on a clean tree | You skipped a patch, or applied one twice. Compare `git log --oneline` with `PROGRESS.md`. |
| Whitespace or "corrupt patch" errors | The download got Windows line endings. Run `dos2unix ~/patches/<file>.patch` and retry. |
| A previous `git am` is still in progress | `git am --abort`, then retry. |
| Dirty working tree | Commit or `git stash` your changes first, or the patch will not apply. |

Then run the patch's acceptance commands from its patch file, and only then tick the box in `PROGRESS.md` (the patch itself already
ticks it; if the commands fail, tell the agent and ask for a `fix-pxx-...` patch).

## 7. Running ToolForge from WSL2

* Servers started in WSL2 (`toolforge serve --transport http`, `toolforge dashboard`, `python3 -m http.server -d site/dist 8000`)
  are normally reachable from Windows browsers at `http://localhost:<port>`. Keep them bound to `127.0.0.1` (the default); the guide
  requires a token before anything else.
* An MCP client running on Windows (for example a desktop assistant) cannot start a stdio server inside WSL2 by itself. Either run
  the client inside WSL2, or launch the server through `wsl.exe -d Ubuntu -- <full path to toolforge> serve --transport stdio`, or use
  the HTTP transport with the token. `docs/clients.md` (P07) has the exact snippets once the server exists.
* If Docker feels slow or runs out of memory, give WSL2 more room with a `.wslconfig` file in your Windows user folder (the `memory`
  and `processors` keys under `[wsl2]`), then `wsl --shutdown` from PowerShell. Check Microsoft's WSL documentation for the current
  options.
* Leftover containers after a crash: `toolforge doctor --clean` (P04), or `docker ps -a` to look.

## 8. Quick self-check before P01

```
git --version && python3 --version && docker version --format '{{.Server.Version}}'
python3 scripts/check_guide.py
```

The last line should end with "guide is consistent". If Docker is not ready yet, everything except the Docker-marked tests still
works.
