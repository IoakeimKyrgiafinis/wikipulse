# Before running /ship-all

Install and start these on Windows once, so the automated run doesn't stop halfway:

1. **Docker Desktop**, running.
2. **Ollama**, running, with models pulled: `ollama pull qwen3:4b` and `ollama pull qwen3:8b`.
3. **Kubernetes tools** (needed from stage 8): `winget install Kubernetes.kind Kubernetes.kubectl Helm.Helm Hashicorp.Terraform`
4. **GitHub CLI** (optional, for pushing and watching CI): `winget install GitHub.cli`, then `gh auth login`.
5. Copy `.env.example` to `.env` after spec 00 creates it, and put your email in `USER_AGENT`. Wikimedia requires a contact address.
6. In Claude Code, keep auto mode on so routine commands don't wait for approval.

Then open this folder in the Claude desktop app's Code tab and run `/ship-all`.
