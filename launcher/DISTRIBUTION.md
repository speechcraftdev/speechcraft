# SpeechCraft download and release process

## Current support status

| Platform | Package and installer | Validation | Distribution status |
| --- | --- | --- | --- |
| Linux x86-64 | Versioned application tarball; `install-linux.sh` calls the official per-user Python installer. Requires system Python 3.9+ and `curl`. | Existing installed-package smoke covers installation, repeat install, upgrade state preservation, local `/setup`, and operation without global Node/Bun. | Candidate only. Public release is blocked pending FFmpeg redistribution review and TitaNet provenance approval. |
| macOS Apple Silicon | ARM64 application archive and Python installer exist. | Archive cross-build and temporary-home registration were recorded; the Darwin Node binary and application were not run, and the package is unsigned and not notarized. | Development artifact; not production-ready. |
| Windows x64 | Native PowerShell entry point for a Windows wrapper that dispatches into an existing Ubuntu WSL2 installation. It does not install WSL. | Native Windows and WSL2 acceptance has not been completed. | Development artifact; not production-ready. |

The Linux bootstrap is the only production-targeted flow in this implementation. Model weights are not part of any package. Runtime and model preparation stays in the local `/setup` page. SpeechCraft binds its services to loopback.

## Public installation status

There is no public installer URL yet. The only existing GitHub release is the draft candidate `v0.1.0`, whose assets require authenticated GitHub access. Its release page is [available for authenticated inspection](https://github.com/speechcraftdev/speechcraft/releases/tag/untagged-200784c21a5744c7d660); its assets are not anonymously downloadable. Do not use `curl | sh` from that draft.

After a production release has passed the compliance and publisher-authentication gates, its versioned GitHub bootstrap URL will be:

```text
https://github.com/speechcraftdev/speechcraft/releases/download/<approved-tag>/install.sh
```

`<approved-tag>` is a placeholder, not a currently working URL. No temporary or custom install domain is configured. The intended convenience command after approval is `curl -fsSL <approved-versioned-bootstrap-url> | sh`; until then, do not advertise that command as available.

## Bootstrap behavior

For a published Linux release, the versioned bootstrap performs the following:

```sh
curl -fsSLo install.sh https://<temporary-host>/install.sh
sh install.sh
speechcraft
```

The temporary host contains only the generated release-specific `install.sh` and short instructions. The bootstrap downloads the exact tagged GitHub Release archive, adjacent `.sha256`, adjacent `.release.json`, and both official Linux installer files. It checks pinned SHA-256 and byte-size values for every file; verifies the checksum's content and the exact release version, platform, source commit, size, and digest; and only then invokes the per-user installer. The official installer checks the archive again and verifies its complete internal file inventory before activation.

The bootstrap fails before downloading if the OS is not Linux, the architecture is not x86-64, or system Python is older than 3.9. It uses `curl` with HTTPS-only bounded redirects, retries, and timeouts; accepted redirect hosts are GitHub and GitHub's release asset hosts. A trapped temporary directory is removed on success or failure. No `sudo`, global Node/Bun install, repository clone, model download, or telemetry is involved.

The checksum delivered beside an archive is only a consistency check. The generated script pins the archive, metadata, checksum, and official installer bytes, so changes to any downloaded file are detected. However, if the bootstrap itself is fetched from the same release, those pins do not authenticate the publisher independently. The current workflow therefore creates internal draft candidates only and is not production-secure. Before public installation, establish an independent publisher trust anchor and have the bootstrap verify it (for example, a signed build attestation rooted in the reviewed GitHub Actions identity). The release report states this limitation explicitly.

When a release is approved, the safe user flow is to download the *versioned* bootstrap as a file, verify it through the documented independent publisher mechanism, inspect the pinned release identity, then execute it:

```sh
curl -fSLo install.sh 'https://github.com/speechcraftdev/speechcraft/releases/download/<approved-tag>/install.sh'
# Verify install.sh with the independently published signature/attestation instructions.
less install.sh
sh install.sh
```

Do not substitute a moving `latest` URL or treat a checksum beside the archive as publisher authentication.

## Create a draft release

The builder pins every package input to the exact 40-character source commit. It exports source with `git archive`, records that revision in package metadata, and the bootstrap generator rejects mismatched source revision, version, platform, archive size, or digest. It does not build from uncommitted or untracked application source. This makes the build inputs and recipe repeatable; generated frontend and wheel output are not claimed to be byte-for-byte identical across rebuilds. The release records hashes for the actual uploaded files. Do not reuse a tag or replace an existing asset.

From a clean checkout at the exact source commit, run:

```sh
launcher/scripts/publish-draft-release.sh v0.1.0 <40-character-source-commit>
```

The command runs bootstrap and official-installer tests, builds the Linux package and release-specific `install.sh`, runs the installed-package smoke, writes a `VERIFICATION.md` report, creates the version tag on the exact source commit if it does not already exist, then calls `gh release create --draft`. It refuses an existing release or a tag that points elsewhere, and never force-moves a tag. The release includes the archive, checksum, release JSON manifest, `install.sh`, official `install-linux.py` and `install-linux.sh`, and the verification report. Its notes record the source commit, platform support status, and unresolved publication and trust gates. Inspect draft asset contents through authenticated `gh release download`; users cannot anonymously download draft assets.

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

## First run, upgrades, and data

The Linux command installs per-user under `~/.local/share/speechcraft/app` and registers `~/.local/bin/speechcraft`. The official installer uses a versioned releases directory and atomically switches the `current` pointer after verification. Reinstalling the same version is safe; installing a lower version is rejected. Upgrades preserve the separate user data tree, including models, projects, recordings, exported datasets, credentials, and controller state.

After installation, run `speechcraft`, open the local `/setup` page, and follow its runtime preparation flow. Model downloads and gated Hugging Face authorization happen there; the bootstrap never accepts model terms or downloads models. Use `speechcraft status` and `speechcraft stop` to inspect and stop the local services.

To uninstall while preserving all user data, stop SpeechCraft, remove only the versioned application and command registration, and keep `~/.local/share/speechcraft` data intact. The supported manual sequence is:

```sh
speechcraft stop
rm -rf -- "$HOME/.local/share/speechcraft/app"
rm -f -- "$HOME/.local/bin/speechcraft"
```

This uninstall command assumes the command path is the installer-managed SpeechCraft wrapper; inspect it first if it was replaced or customized. To remove user data as well, do that separately and only after making any backups you need.
