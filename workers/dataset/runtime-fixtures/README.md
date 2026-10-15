# SpeechCraft runtime probe audio

The probe fixtures are cropped/derived from **AcoustiAngel**, uploaded by AcoustiAngel on Freesound as sound 739037, “a clean, high-quality recording of a female voice talking for 2 minutes.” The sound page declares CC0 1.0 and lists the original as 48 kHz stereo. Source: <https://freesound.org/people/AcoustiAngel/sounds/739037/>. The page notes that the text was generated with GPT-4 assistance. No private SpeechCraft recording is included.

SpeechCraft used the publicly accessible MP3 preview (SHA-256 `4baed36074c705c654117f12e19179d72016fca2defc5ff14d360ed518465ed5`). The preview measures 127.727625 seconds, 24 kHz stereo. FFmpeg 8.1.3 produced the 18-second derivatives. `speech.wav` is 16 kHz mono PCM; `speech-silence.wav` adds one second of leading and trailing silence; `speech-compressed.mp3` is an MP3 encode used for FFmpeg decode coverage. The source is under CC0 1.0; these derivatives are also dedicated to the public domain under CC0.

| File | SHA-256 | Size | Duration | Rate | Channels | Use |
|---|---|---:|---:|---:|---:|---|
| `speech.wav` | `ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd` | 576078 | 18 s | 16000 Hz | 1 | Whisper, TitaNet and CUDA inference |
| `speech-silence.wav` | `65afa781f190dd7e6ee3b719e0b200b1224d057d4ae4c5077cd1b125685cda06` | 640078 | 20 s | 16000 Hz | 1 | Positive speech plus silence for Silero |
| `speech-compressed.mp3` | `e61a459dfc841b060aa2db5119ddd527346177ab965f290986a46c965302c33a` | 144908 | 18 s | 16000 Hz | 1 | Compressed audio probe for FFmpeg |

Probe outputs record only measurements and transcript character count, not fixture speech text.
