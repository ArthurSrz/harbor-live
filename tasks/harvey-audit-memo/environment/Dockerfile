# Base: https://github.com/harveyai/harvey-labs/blob/main/sandbox/Dockerfile
# Local copy of harveyai/corporate-governance-draft-audit-findings-memorandum, with Claude Code baked in
# so harbor skips its per-trial download (downloads.claude.ai drops or rate-limits the 292 MB binary).

# The binary comes from npm's per-platform package; the lab image has no tar, so unpack it here.
# ponytail: arm64 only (Apple Silicon); on x86 use claude-code-linux-x64.
FROM busybox:1.36 AS claude
ADD https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-2.1.308.tgz /cc.tgz
RUN tar xzf /cc.tgz -C /

FROM ghcr.io/harveyai/lab-sandbox@sha256:cf4dac013b4cbae0fe417074a0180109fe1a3cccb6ec8b149909e3f8b87b4cb3

COPY --from=claude /package/claude /root/.local/bin/claude

COPY documents/ /workspace/documents/
