# Security policy

Do not put API keys, Codex authentication files, private source, or sensitive
design-system data in an issue or research result. Report vulnerabilities
through the repository's private security-advisory feature.

The enforcement CLI does not use the network. The optional GitHub research
workflow passes `OPENAI_API_KEY` only to the official Codex Action in a
read-only job. Research output is untrusted until reviewed.
