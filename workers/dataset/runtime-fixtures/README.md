# SpeechCraft runtime probe audio

The probe fixtures are cropped/derived from **AcoustiAngel**, uploaded by AcoustiAngel on Freesound as sound 739037, “a clean, high-quality recording of a female voice talking for 2 minutes.” The sound page declares CC0 1.0 and lists the original as 48 kHz stereo. Source: <https://freesound.org/people/AcoustiAngel/sounds/739037/>. The page notes that the text was generated with GPT-4 assistance. No private SpeechCraft recording is included.

SpeechCraft used the publicly accessible MP3 preview (SHA-256 `4baed36074c705c654117f12e19179d72016fca2defc5ff14d360ed518465ed5`). The preview measures 127.727625 seconds, 24 kHz stereo. FFmpeg 8.1.3 produced the 18-second derivatives. `speech.wav` is 16 kHz mono PCM; `speech-silence.wav` adds one second of leading and trailing silence; `speech-compressed.mp3` is an MP3 encode used for FFmpeg decode coverage. The source is under CC0 1.0; these derivatives are also dedicated to the public domain under CC0.

| File | SHA-256 | Size | Duration | Rate | Channels | Use |
|---|---|---:|---:|---:|---:|---|
| `speech.wav` | `ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd` | 576078 | 18 s | 16000 Hz | 1 | Whisper, WeSpeaker ECAPA and CUDA inference |
| `speech-silence.wav` | `65afa781f190dd7e6ee3b719e0b200b1224d057d4ae4c5077cd1b125685cda06` | 640078 | 20 s | 16000 Hz | 1 | Positive speech plus silence for Silero |
| `speech-compressed.mp3` | `e61a459dfc841b060aa2db5119ddd527346177ab965f290986a46c965302c33a` | 144908 | 18 s | 16000 Hz | 1 | Compressed audio probe for FFmpeg |

Probe outputs record only measurements and transcript character count, not fixture speech text.

## Community-1 diarization fixture

`community1-two-voice.wav` is a 24-second, 16 kHz mono PCM crop of the first
24 seconds of the Freesound item [“Podcast AI Slop” by qubodup](https://freesound.org/people/qubodup/sounds/842832/).
The uploader marks the item CC0 1.0 and documents that it is a fictional
two-voice dialogue generated with Kokoro TTS voices “us Heart” and “us Liam.”
SpeechCraft converted the public low-quality MP3 preview to mono 16 kHz PCM with
FFmpeg 9.0.1; SHA-256 is
`981721582d5a7524cd8e825d8331d433b9e06a346de76f99855b026ca4216e7d`, size
768,078 bytes, duration 24 seconds. It is synthetic dialogue, not a recording
of two human speakers. The Community-1 probe requires valid diarization turns
for at least two distinct labels, but this fixture cannot establish quality on
real human conversation. A licensed human multi-speaker fixture remains a
validation limitation and should be supplied before release qualification.
