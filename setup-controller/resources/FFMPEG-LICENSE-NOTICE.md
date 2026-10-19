# FFmpeg and FFprobe runtime notice and release review

SpeechCraft invokes FFmpeg and FFprobe as local command-line programs. It does
not link the SpeechCraft application to FFmpeg libraries. The Linux setup
controller downloads the binaries directly from the pinned BtbN GitHub Release
URL; the SpeechCraft application archive does not contain or mirror the
FFmpeg/FFprobe binaries. The controller verifies the archive and executable
hashes, extracts only the two executables and the upstream LGPL text, then runs
the audio decode probe. The user copy is stored under SpeechCraft's per-user
data directory.

## Exact Linux x86-64 artifact

- Publisher release: [BtbN Auto-Build 2026-10-14 13:03](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-14-13-03)
- Publisher release commit: `63ac3265b0789ea16c7b1f8351cd2ad195a28722`
- Asset: `ffmpeg-n8.1.3-18-g46d899d7a0-linux64-lgpl-8.1.tar.xz`
- Asset SHA-256: `602ca2985139dbab87e0d7c70f48afc69548060463dcfab44c0e5f50ebe39df8`
- FFmpeg source revision: `46d899d7a02bc63237472cc4897258d5ddf274ac`
- FFmpeg executable SHA-256: `99ca80f681d424d09af9557d3e9cba0280c15e63b058743cc819ba87d3f577ee`
- FFprobe executable SHA-256: `20d63f0983ecc952f488fa7ca1e6b20b9ad3e2f655cefd322f5d9cd04b9dbacc`
- Publisher build variant: `linux64-lgpl 8.1`; BtbN's release identifies it as LGPL and its README defines the `lgpl` variant as excluding GPL-only dependencies, including libx264 and libx265.
- Pinned recipe files: [`variants/linux64-lgpl.sh`](https://github.com/BtbN/FFmpeg-Builds/blob/63ac3265b0789ea16c7b1f8351cd2ad195a28722/variants/linux64-lgpl.sh), [`variants/defaults-lgpl.sh`](https://github.com/BtbN/FFmpeg-Builds/blob/63ac3265b0789ea16c7b1f8351cd2ad195a28722/variants/defaults-lgpl.sh), [`addins/8.1.sh`](https://github.com/BtbN/FFmpeg-Builds/blob/63ac3265b0789ea16c7b1f8351cd2ad195a28722/addins/8.1.sh), and the pinned [`build.sh`](https://github.com/BtbN/FFmpeg-Builds/blob/63ac3265b0789ea16c7b1f8351cd2ad195a28722/build.sh).
- The recipe selects FFmpeg's `release/8.1` branch and passes `--enable-version3 --disable-debug`; BtbN's build command also passes `--pkg-config-flags=--static`. The LGPL variant adds no `--enable-gpl` or `--enable-nonfree` flag. The built executable reports LGPL-3.0-or-later and the release archive includes FFmpeg's `COPYING.LGPLv3`, which SpeechCraft stores as `COPYING.LGPLv3.txt` beside the installed executables.
- `readelf` inspection found dynamic dependencies on the host's normal C runtime and no required `GLIBC_*` symbol newer than 2.28. SpeechCraft does not bundle those host libraries. The package's exact binary identities and probe evidence are recorded in `runtime-manifest.json`.

The SHA-256 above agrees with both the repository's runtime manifest and the
asset digest published by the pinned BtbN Release API. The BtbN build recipe and
source revision are public and immutable. The application archive omits all
FFmpeg binaries. The setup controller downloads the archive straight from
BtbN's release asset URL, validates the trusted digest already in the
application manifest, and keeps BtbN's LGPL text with the installed tools.

## Notice basis and release-owner decision

FFmpeg describes its project license as LGPL 2.1-or-later unless GPL-only
components are enabled. Its published checklist is expressly framed for
applications linking against FFmpeg libraries and includes additional source,
configuration, notice, and patent guidance. SpeechCraft launches the pinned
FFmpeg CLI as a subprocess; it does not link against FFmpeg libraries. The
checklist is evidence for review, not a finding that every item applies to this
separate-process, direct-upstream-download design. FFmpeg also says it cannot
give a definitive patent-clearance answer.

Before the release owner clears publication, the designated compliance
reviewer must answer this specific question:

> Does SpeechCraft's setup-controlled, direct HTTPS download from BtbN to the
> user's own per-user data directory make SpeechCraft a distributor/conveyor
> of this FFmpeg binary for the v0.1.1 product release? If yes, are BtbN's
> published exact source revision, pinned build recipe, upstream LGPL text,
> source links, and our notice sufficient for this static-dependency CLI build,
> or must SpeechCraft host a corresponding-source bundle/build configuration
> or take other steps before release?

The reviewer should also state whether codec patent review is required for the
project's stated testing-release regions/use. No approval is inferred here.
Until that decision is recorded, `redistribution_status` in
`runtime-manifest.json` must remain
`source_identity_and_notice_documented; product legal review remains required before application release`.

## References

- [FFmpeg license and legal considerations](https://ffmpeg.org/legal.html)
- [BtbN pinned release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-14-13-03)
- [BtbN pinned source and build recipe](https://github.com/BtbN/FFmpeg-Builds/tree/63ac3265b0789ea16c7b1f8351cd2ad195a28722)
- [FFmpeg source revision](https://github.com/FFmpeg/FFmpeg/tree/46d899d7a02bc63237472cc4897258d5ddf274ac)
- [GNU LGPL 3.0](https://www.gnu.org/licenses/lgpl-3.0.html)
