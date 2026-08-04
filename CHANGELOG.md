# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with PEP 440 versions for Python distributions.

## [Unreleased]

### Changed

- Froze OmniPet Engine feature development and made OmniPet-Skill the recommended path for creating new pets, while retaining the published `0.1.0a1` workflows for compatibility.

## [0.1.0a1] - 2026-07-22

### Added

- Public alpha package with a guided CLI for project initialization, generation, staged QA and approval, packaging, and checkpoints.
- Built-in OpenAI image generation and deterministic hatch tooling.
- Offline test suite, cross-platform CI, trusted PyPI publishing, and release artifacts.

### Security

- Environment-only API key handling, bounded project schemas, path containment, symlink rejection, and atomic artifact promotion.
