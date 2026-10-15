# Managed FFmpeg and FFprobe notice

SpeechCraft's Linux x86-64 runtime recipe downloads the paired FFmpeg and FFprobe executables from the immutable BtbN release asset recorded in `runtime-manifest.json`:

- BtbN release: `autobuild-2026-10-14-13-03` (`63ac3265b0789ea16c7b1f8351cd2ad195a28722`)
- Asset: `ffmpeg-n8.1.3-18-g46d899d7a0-linux64-lgpl-8.1.tar.xz`
- SHA-256: `602ca2985139dbab87e0d7c70f48afc69548060463dcfab44c0e5f50ebe39df8`
- FFmpeg source identity: `46d899d7a02bc63237472cc4897258d5ddf274ac`
- Executables: FFmpeg `99ca80f681d424d09af9557d3e9cba0280c15e63b058743cc819ba87d3f577ee`; FFprobe `20d63f0983ecc952f488fa7ca1e6b20b9ad3e2f655cefd322f5d9cd04b9dbacc`
- Reported license: LGPL-3.0-or-later (`--enable-version3`). Inspected configuration does not contain `--enable-gpl` or `--enable-nonfree`; `libx264` and `libx265` are disabled.

The controller verifies the archive and each executable before publication. It installs only the paired executable files and the archive's license text in the versioned SpeechCraft tools directory. It does not bundle system libraries or invoke an install script. Both executables report the same exact version identity and were exercised against the checked-in compressed speech fixture.

The Linux x86-64 executables are dynamically linked to the normal system C runtime (`libc`, `libm`, `libdl`, `librt`, `libpthread`, `libmvec`, `libgcc_s`, plus the ELF loader); `readelf` showed no required `GLIBC_*` symbol newer than 2.28. They were run on the current Linux host. SpeechCraft therefore declares glibc 2.28 as the minimum and does not ship those system libraries.

This identifies the selected artifact and its reported build flags; it is not a legal opinion or a conclusion that SpeechCraft's release obligations are satisfied. FFmpeg's legal guidance says optional GPL components change the applicable license and describes source/configuration obligations for binary distribution. Before a public SpeechCraft release, legal/release owners must confirm the applicable obligations for this third-party build, retain and provide the exact corresponding source and build configuration as required, include license/attribution notices, and review codec patent considerations. The artifact remains directly downloaded from the named publisher rather than redistributed in the SpeechCraft application archive. Keep `redistribution_status` in the manifest as `source_identity_and_notice_documented; product legal review remains required before application release` until that review is complete.

References:

- [FFmpeg legal considerations](https://www.ffmpeg.org/legal.html)
- [BtbN FFmpeg-Builds pinned source revision](https://github.com/BtbN/FFmpeg-Builds/tree/63ac3265b0789ea16c7b1f8351cd2ad195a28722)
- [FFmpeg source revision](https://github.com/FFmpeg/FFmpeg/tree/46d899d7a02bc63237472cc4897258d5ddf274ac)
- [Pinned BtbN release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-14-13-03)
