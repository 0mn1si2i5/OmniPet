# Security Policy

## Supported Versions

The current alpha `0.1.0a1` is the only supported version. Alpha support means reports are assessed on a best-effort basis; compatibility fixes may require a new prerelease.

## Reporting A Vulnerability

Use the repository's GitHub **Private vulnerability reporting** feature. Do not open a public issue or discussion. If that feature is unavailable, contact the project maintainers privately through GitHub repository moderation rather than disclosing details publicly. No public email address is designated.

Include the affected version, platform, reproduction steps, impact, and a minimal sanitized example. Remove API keys, personal data, proprietary prompts, reference images, absolute user paths, and raw provider responses.

## Security Scope

High-priority concerns include API key exposure; path traversal, symlink, archive, or unsafe file-promotion behavior; provider request leakage or unexpected network access; unbounded image or manifest processing; package and checkpoint integrity; and bypasses of approval or QA gates.

OmniPet accepts `OPENAI_API_KEY` only through the process environment and supports only its built-in provider path. Project validation is bounded but is not a general secret scanner. Rotate a key immediately through the provider if exposure is suspected.
