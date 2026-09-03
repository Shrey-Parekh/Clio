# Training a wake word model

Wake phrases live in `wake_word.phrases` in `.env`/`config/default.toml`. Each one needs
a trained `.onnx` file at `models/wake_words/<slug>.onnx`, where `<slug>` is the phrase
lowercased with spaces replaced by underscores (`WakeWordConfig.slug()`). Config validation
fails clearly at startup if a configured phrase has no matching model file.

## Why this isn't done on Windows

openWakeWord's training pipeline depends on Piper TTS for synthetic speech generation,
which is documented by the maintainers as Linux-only (untested on Windows). Training runs
in WSL2 (Ubuntu 24.04); the runtime model (`.onnx`, via `onnxruntime`) works fine on Windows -
only the training step needs Linux.

## One-time environment setup (WSL2 / Ubuntu 24.04)

```bash
wsl --install -d Ubuntu-24.04
```

Inside WSL:

```bash
mkdir -p ~/oww_training && cd ~/oww_training
python3.11 -m venv venv   # must be 3.11, not the system 3.12 - see gotchas below
git clone https://github.com/dscripka/openwakeword
git clone --branch v2.0.0 https://github.com/rhasspy/piper-sample-generator  # pin v2.0.0, see gotchas
wget -O piper-sample-generator/models/en_US-libritts_r-medium.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt

venv/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
venv/bin/pip install numpy scipy 'scipy<1.15' piper-phonemize webrtcvad mutagen==1.47.0 \
  torchinfo==1.8.0 torchmetrics==1.2.0 audiomentations==0.33.0 torch-audiomentations==0.11.0 \
  pronouncing==0.2.0 datasets pyyaml tqdm onnx onnxruntime-gpu speechbrain acoustics \
  scikit-learn requests
venv/bin/pip install -e ./openwakeword

mkdir -p openwakeword/openwakeword/resources/models
cd openwakeword/openwakeword/resources/models
for f in embedding_model.onnx embedding_model.tflite melspectrogram.onnx melspectrogram.tflite; do
  wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f
done
```

### Gotchas hit building this (all fixed above, kept here so they don't get rediscovered)

- **Python must be 3.11.** `piper-phonemize` has no Linux wheel for 3.12 (only macOS). Ubuntu
  24.04 ships 3.12 by default - install 3.11 via `deadsnakes` PPA.
- **`piper-sample-generator` must be pinned to `v2.0.0`.** The `main` branch was restructured
  into a package (v3.x) that openWakeWord's `train.py` doesn't know about - it still does
  `from generate_samples import generate_samples`, which only exists at the repo root in the
  old (pre-v3) layout.
- **`torch.load` needs `weights_only=False`** for the v2.0.0 checkpoint - PyTorch 2.6 changed
  the default and the old checkpoint format isn't in the safe-globals allowlist. Patch
  `generate_samples.py` line ~74. Only do this because the checkpoint is from the official
  GitHub release - never blanket-disable weights_only for an untrusted file.
- **`scipy<1.15`** - the `acoustics` package (unmaintained since ~2020) uses
  `scipy.special.sph_harm`, removed in newer scipy.
- **Install `torch` and `torchaudio` together, not separately** - installing them in two pip
  calls can resolve mismatched CUDA builds (torch cu124 + torchaudio cu13), which breaks with
  a `libcudart.so.13` error.
- **`onnxruntime-gpu`'s CUDA provider may fail to load** (wants a different CUDA runtime than
  torch does) - it falls back to CPU automatically, which is fine; feature extraction is cheap
  enough that this doesn't matter in practice.
- **Skip `tensorflow`/`onnx_tf`.** The notebook this pipeline is based on installs them for an
  optional `.tflite` export step. Not needed - Clio's runtime uses `.onnx` directly via
  `onnxruntime`, same as Kokoro and the VAD model. `train.py` throws a `ModuleNotFoundError`
  at the very end of a successful run because of this - harmless, the `.onnx` is already saved
  by that point.
- **The AudioSet dataset the old notebook references (`bal_train09.tar`) no longer exists** -
  it moved from `.tar` archives to `.parquet` shards. Also, the `datasets` library's `Audio`
  feature now requires `torchcodec` (a GPU video-decoding library) even with `decode=False` -
  more trouble than it's worth for plain WAV/FLAC. Download parquet files directly and decode
  with `soundfile` instead of going through `datasets`' audio pipeline.

## Shared data (one-time, ~18GB, reused across every phrase)

```bash
# RIRs (~270 files) + AudioSet background noise - see download_shared_data2.py pattern:
# direct file/parquet download, not datasets.load_dataset()'s Audio() casting.

# Negative features + validation set (the actual expensive download):
wget https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy
wget https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy
```

## Per-phrase training

```bash
venv/bin/python make_config.py "hey clio" 2000 500 10000   # phrase, n_samples, n_samples_val, steps
venv/bin/python openwakeword/openwakeword/train.py --training_config config_hey_clio.yaml --generate_clips
venv/bin/python openwakeword/openwakeword/train.py --training_config config_hey_clio.yaml --augment_clips
venv/bin/python openwakeword/openwakeword/train.py --training_config config_hey_clio.yaml --train_model
```

~7-8 minutes per phrase at `n_samples=2000, steps=10000` on an RTX 4060 Ti. Doubling steps to
20000 barely moves accuracy - the useful lever is `n_samples`, not step count, past a point.

**Don't trust the training-reported accuracy/recall numbers on their own.** They're measured
at openWakeWord's internal evaluation threshold, which doesn't necessarily match the deployed
`wake_word.threshold`. A model reporting recall 0.45 scored 0.94 on its own phrase and ~0.001
on unrelated speech when tested for real - a ~1000x separation, comfortably usable. Verify with
real inference (`openwakeword.model.Model(wakeword_models=[...])`, feed real audio, check
scores against the actual configured threshold), not the training log.

**Training has real variance.** Two phrases out of the first six failed on the first attempt
(near-zero own-phrase score) with otherwise identical settings. One (`hello clio`) fixed itself
on a straight retry. One (`daddy is home clio`, a 4-word sentence-length phrase) failed
identically twice - a repeat rather than noise, suggesting the phrase itself doesn't fit the
pipeline well (likely too long for the embedding window, which appears tuned for short 1-3 word
wake phrases). If a retry doesn't fix it, don't chase it - shorten the phrase instead.

## After training

Copy the `.onnx` (not `.tflite` - Clio doesn't use it) to Windows:

```bash
cp models/hey_clio/hey_clio.onnx /mnt/c/Users/<you>/Documents/Clio/models/wake_words/hey_clio.onnx
```

`WakeWordDetector` in `clio/speech/wake_word.py` loads every phrase in `wake_word.phrases`
from `wake_word.models_dir` (default `models/wake_words/`) automatically - no code changes
needed for a new phrase, just train it and drop the `.onnx` in place with the right slug name.
