# SpeechCraft download and release process

## Current support status

| Platform | Package and installer | Validation | Distribution status |
| --- | --- | --- | --- |
| Linux x86-64 | Versioned application tarball; `install-linux.sh` calls the official per-user Python installer. Requires system Python 3.9+ and `curl`. | Installed-package smoke covers installation, repeat install, upgrade state preservation, local `/setup`, and operation without global Node/Bun. The setup page displays ECAPA publisher attribution, pinned revision, and CC BY 4.0 links. | Release-owner publication approval is still required for the scoped FFmpeg delivery question in `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`. |
| macOS Apple Silicon | ARM64 application archive and Python installer exist. | Archive cross-build and temporary-home registration were recorded; the Darwin Node binary and application were not run, and the package is unsigned and not notarized. | Development artifact; not production-ready. |
| Windows x64 | Native PowerShell entry point for a Windows wrapper that dispatches into an existing Ubuntu WSL2 installation. It does not install WSL. | Native Windows and WSL2 acceptance has not been completed. | Development artifact; not production-ready. |

Linux x86-64 is the only target of the candidate bootstrap. Model weights are not part of any package. Runtime and model preparation stays in the local `/setup` page. SpeechCraft binds its services to loopback.

## Public installation status

There is no public `v0.1.1` installer until the release-owner issue is closed with the `release-approved` label and a comment from the designated reviewer records `APPROVED_FOR_PUBLIC_TESTING_RELEASE v0.1.1 Linux x86-64 source <full-main-sha>`. The publishing workflow requires that reviewer’s GitHub username and checks the comment matches the exact source being released; the approver must differ from both the issue author and the workflow dispatcher. The immutable `v0.1.0` tag remains untouched. Existing internal drafts remain authenticated-only and must not be used as installer URLs.

For an authorized test download of the current draft's real GitHub assets, use `gh release download v0.1.0-internal.3 --repo speechcraftdev/speechcraft`. The uploaded bootstrap asset URL is `https://github.com/speechcraftdev/speechcraft/releases/download/untagged-23b5f0acd87ce30ada5d/install.sh`; draft access is authenticated. This URL is for internal testing only, not a public install entry point.

After the release-owner approval record exists and the protected publishing workflow completes, the versioned GitHub bootstrap URL is:

```text
https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/install.sh
```

This URL is not working until the public pre-release exists. The intended convenience command is `curl -fsSL https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/install.sh | sh`.

## Bootstrap behavior

For a published Linux release, the versioned bootstrap performs the following:

```sh
curl -fsSL https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/install.sh | sh
speechcraft
```

For the production flow, the temporary host contains only the generated release-specific `install.sh` and short instructions. The bootstrap downloads the exact tagged GitHub Release archive, adjacent `.sha256`, adjacent `.release.json`, and both official Linux installer files. It checks pinned SHA-256 and byte-size values for every file; verifies the checksum's content and the exact release version, platform, source commit, size, and digest; and only then invokes the per-user installer. The official installer checks the archive again and verifies its complete internal file inventory before activation.

The bootstrap fails before downloading if the OS is not Linux, the architecture is not x86-64, or system Python is older than 3.9. Production bootstraps use `curl` with HTTPS-only bounded redirects, retries, and timeouts; accepted redirect hosts are GitHub and GitHub's release asset hosts. A trapped temporary directory is removed on success or failure. No `sudo`, global Node/Bun install, repository clone, model download, or telemetry is involved.

### Private loopback staging for acceptance

When a GitHub draft is not anonymously downloadable, the official generator accepts `--asset-base-url http://127.0.0.1:<port>` or `http://localhost:<port>` for private acceptance staging. HTTP is allowed only for an explicit loopback host and port. The generated bootstrap pins that exact asset base URL, does not accept redirects away from it, and retains the SHA-256, size, release manifest and installer checks. Bind the staging server to loopback and use an authenticated SSH port forward when CozyTent needs to reach the laptop. Do not expose the staging server publicly.

```sh
node launcher/scripts/generate-bootstrap.mjs \
  --tag v0.1.0-internal.3 \
  --source-revision <exact-commit> \
  --asset-base-url http://127.0.0.1:28413 \
  --output <directory-containing-the-release-assets>
```

This validates the real curl bootstrap and package download through private staging. It is not a public installer URL or a production hosting configuration. Generated public bootstraps continue to use HTTPS GitHub release assets and the restricted GitHub redirect allowlist.

The public workflow uses GitHub Actions OIDC/Sigstore provenance to attest every release asset, then verifies each attestation against the expected repository workflow before publishing. HTTPS to the official GitHub repository authenticates the bootstrap transport; its embedded pins bind the archive, manifest, checksum, and official installer bytes. Users can independently verify provenance without making GitHub CLI a client installation dependency:

When a release is approved, the safe user flow is to download the *versioned* bootstrap as a file, verify it through the documented independent publisher mechanism, inspect the pinned release identity, then execute it:

```sh
curl -fSLo install.sh 'https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/install.sh'
gh attestation verify install.sh --repo speechcraftdev/speechcraft --signer-workflow speechcraftdev/speechcraft/.github/workflows/release-linux.yml
less install.sh
sh install.sh
```

GitHub CLI is optional for users who want local provenance verification; ordinary installation does not require it. Do not substitute a moving `latest` URL or treat an adjacent checksum by itself as publisher authentication.

## Create a draft release

The builder pins every package input to the exact 40-character source commit. It exports source with `git archive`, records that revision in package metadata, and the bootstrap generator rejects mismatched source revision, version, platform, archive size, or digest. It does not build from uncommitted or untracked application source. This makes the build inputs and recipe repeatable; generated frontend and wheel output are not claimed to be byte-for-byte identical across rebuilds. The release records hashes for the actual uploaded files. Do not reuse a tag or replace an existing asset.

From a clean checkout at the exact source commit, run a new semantic-version tag for a release candidate, or a numbered internal tag for an authenticated draft build:

```sh
launcher/scripts/publish-draft-release.sh v0.1.0-internal.1 <40-character-source-commit>
```

The command runs bootstrap and official-installer tests, builds the Linux package and release-specific `install.sh`, runs the installed-package smoke, writes a `VERIFICATION.md` report, creates the version tag on the exact source commit if it does not already exist, then calls `gh release create --draft`. It refuses an existing release or a tag that points elsewhere, and never force-moves a tag. A `vX.Y.Z-internal.N` tag retains application version `X.Y.Z` and is explicitly an authenticated, non-public candidate identity. The release includes the archive, checksum, release JSON manifest, `install.sh`, official `install-linux.py` and `install-linux.sh`, and the verification report. Its notes record the source commit, platform support status, and unresolved publication and trust gates. Inspect draft asset contents through authenticated `gh release download`; users cannot anonymously download draft assets.

The manual GitHub Actions workflow in `.github/workflows/release-draft.yml` runs the same command with least-privilege `contents: write`. It takes an exact commit and a never-before-used semantic version tag. It creates draft assets only. The checked-out commit must already be reachable from `main`.

The repository has an active `immutable version tags` ruleset for `refs/tags/v*`. It blocks updates and deletions with no bypass actors; the existing `main` review ruleset is unchanged. A candidate version tag is created once on its exact source commit before the draft release is created.

## Licensing gate and publication

The exact Linux archive and runtime dependency handoff is recorded in
[RELEASE-LICENSING-HANDOFF.md](RELEASE-LICENSING-HANDOFF.md).

The setup page visibly attributes ECAPA to WeSpeaker, links to the pinned model page and CC BY 4.0, and identifies the immutable revision. The Linux application archive contains neither model weights nor a prebuilt FFmpeg binary. Setup downloads ECAPA and the pinned BtbN LGPL FFmpeg/FFprobe build directly from upstream. The remaining release-owner decision is specific to the FFmpeg download path and is quoted with technical evidence in `setup-controller/resources/FFMPEG-LICENSE-NOTICE.md`. The public workflow refuses to run without a closed repository approval issue carrying the `release-approved` label. A GitHub prerelease is public once published and requires that approval record.

Keep the model manager's existing license and artifact-verification gates intact. The ECAPA notice has been added to the setup flow. Do not run the public workflow until the release owner records the exact FFmpeg decision required by the notice and handoff. The old TitaNet checkpoint/conversion blocker is superseded; its historical provenance record remains for audit history.

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

To uninstall while preserving user data, run `speechcraft uninstall` from any directory. It stops owned services through the authenticated lifecycle, then removes the verified per-user command and `~/.local/share/speechcraft/app`. It leaves `data`, `setup-controller`, and `models` in place. Add `--yes` when input is not a terminal. `--purge` is a separate confirmation and only deletes those three directories when each one is a real user-owned directory inside `~/.local/share/speechcraft`. The command is Linux-only; macOS and Windows uninstall are not implemented. A source checkout is not an installation and is not removed.
