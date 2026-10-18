# SpeechCraft download and release process

## Current support status

| Platform | Package and installer | Validation | Distribution status |
| --- | --- | --- | --- |
| Linux x86-64 | Versioned application tarball; `install-linux.sh` calls the official per-user Python installer. Requires system Python 3.9+ and `curl`. | Existing installed-package smoke covers installation, repeat install, upgrade state preservation, local `/setup`, and operation without global Node/Bun. | Candidate only. Public release is blocked pending FFmpeg redistribution review and TitaNet provenance approval. |
| macOS Apple Silicon | ARM64 application archive and Python installer exist. | Archive cross-build and temporary-home registration were recorded; the Darwin Node binary and application were not run, and the package is unsigned and not notarized. | Development artifact; not production-ready. |
| Windows x64 | Native PowerShell entry point for a Windows wrapper that dispatches into an existing Ubuntu WSL2 installation. It does not install WSL. | Native Windows and WSL2 acceptance has not been completed. | Development artifact; not production-ready. |

The Linux bootstrap is the only production-targeted flow in this implementation. Model weights are not part of any package. Runtime and model preparation stays in the local `/setup` page. SpeechCraft binds its services to loopback.

## User installation flow

For a published Linux release, the intended command is:

```sh
curl -fsSLo install.sh https://<temporary-host>/install.sh
sh install.sh
speechcraft
```

The temporary host contains only the generated release-specific `install.sh` and short instructions. The bootstrap downloads the exact tagged GitHub Release archive, adjacent `.sha256`, adjacent `.release.json`, and both official Linux installer files. It checks pinned SHA-256 and byte-size values for every file; verifies the checksum's content and the exact release version, platform, source commit, size, and digest; and only then invokes the per-user installer. The official installer checks the archive again and verifies its complete internal file inventory before activation.

The bootstrap fails before downloading if the OS is not Linux, the architecture is not x86-64, or system Python is older than 3.9. It uses `curl` with HTTPS-only bounded redirects, retries, and timeouts; accepted redirect hosts are GitHub and GitHub's release asset hosts. A trapped temporary directory is removed on success or failure. No `sudo`, global Node/Bun install, repository clone, model download, or telemetry is involved.

The checksum delivered beside an archive is only a consistency check. Trust comes from reviewing the release-specific bootstrap's pins over HTTPS and the immutable source commit/tag in the official repository. The bootstrap pins the archive, metadata, checksum file, and official installers individually, so a modified adjacent checksum or installer is rejected. The current repository does not publish a signed release attestation; GitHub-hosted signed/provenance metadata can be added later without weakening the pinned hash checks.

## Create a draft release

The builder pins every package input to the exact 40-character source commit. It exports source with `git archive`, records that revision in package metadata, and the bootstrap generator rejects mismatched source revision, version, platform, archive size, or digest. It does not build from uncommitted or untracked application source. This makes the build inputs and recipe repeatable; generated frontend and wheel output are not claimed to be byte-for-byte identical across rebuilds. The release records hashes for the actual uploaded files. Do not reuse a tag or replace an existing asset.

From a clean checkout at the exact source commit, run:

```sh
launcher/scripts/publish-draft-release.sh v0.1.0 <40-character-source-commit>
```

The command builds the Linux package and release-specific `install.sh`, creates the version tag on the exact source commit if it does not already exist, then calls `gh release create --draft`. It refuses an existing release or a tag that points elsewhere, and never force-moves a tag. The release includes the archive, checksum, release JSON manifest, `install.sh`, `install-linux.py`, and `install-linux.sh`. Its notes record the source commit, platform support status, and the unresolved publication gate. Inspect draft asset contents through authenticated `gh release download`; users cannot anonymously download draft assets.

The manual GitHub Actions workflow in `.github/workflows/release-draft.yml` runs the same command with least-privilege `contents: write`. It takes an exact commit and a never-before-used semantic version tag. It creates draft assets only. The checked-out commit must already be reachable from `main`.

The repository has an active `immutable version tags` ruleset for `refs/tags/v*`. It blocks updates and deletions with no bypass actors; the existing `main` review ruleset is unchanged. A candidate version tag is created once on its exact source commit before the draft release is created.

## Licensing gate and publication

Creating release infrastructure and uploading a draft are separate from publicly publishing an application. The source documentation marks FFmpeg redistribution and experimental TitaNet provenance as unresolved. The Linux application archive does not ship model weights or a prebuilt FFmpeg binary, but its runtime setup can acquire FFmpeg and contains the experimental TitaNet integration and provenance record. Those points remain blockers for publicly advertising or publishing SpeechCraft until the project documents clearance. A GitHub prerelease is public once published and does not satisfy this gate.

Do not publish the draft until the project has documented applicable FFmpeg redistribution terms and resolved the TitaNet checkpoint/conversion rights. Keep the model manager's existing licensing and verification gates intact.

## Temporary host and later domain move

Cloudflare Pages credentials are not available in the current environment, so no `pages.dev` site is configured. GitHub Pages is the fallback host, but it should be deployed only when a release is approved for public download. The Pages output should contain just the release-specific `install.sh` and a small page with the installation command and platform support link.

When the project chooses a permanent domain, configure that domain as a custom domain for the static Pages site and update DNS/TLS there. The bootstrap's GitHub Release URL and pins remain unchanged. Keep the old Pages address available as a redirect for a transition period; do not replace or move existing release tags/assets. The site must never proxy or host the Next.js application.

## Platform instructions

- [macOS status and manual archive installer](MACOS.md)
- [Windows PowerShell and WSL2 installer](WINDOWS.md)
- [Linux package and application behavior](README.md)
