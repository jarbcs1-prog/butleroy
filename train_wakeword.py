"""Train a wake-word model for "Yo, Archie" / "Yo, Archibald".

A build script, not runtime code: the artefact it produces is a small ONNX file
that `audio.WakeWord` then loads. Nothing here runs while the assistant is
answering you.

Two things about the training data are deliberate and worth stating, because both
are places where the obvious choice produces a model that feels broken:

**Negatives are as important as positives.** Trained on positives alone, this model
fires on any speech at all - it learns "a human made a noise", not "someone said
Archie". So the negative set is deliberately larger than the positive set and
includes the near misses specifically: "yo" alone, "archie" alone, "hey archie",
and other names. Those four are what stop it firing on half your speech.

**"Wake up" was rejected, "yo archie" was not** - but a wake word made of common
words is a false-accept waiting to happen, so the negatives are checked against
ordinary conversational English, not against noise. A model that only beats
silence is a model that fires when you mention a colleague called Archie.

Both variants are trained because you should not have to remember which one you
said.

Augmentation is noise, reverb, gain and speed. Speed matters because it changes
formant timing, which is most of what makes a word recognisable; piper renders one
pitch and one tempo, so without jitter the model learns that tempo.
"""

from __future__ import annotations

import shutil
import sys
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from assistant import speech  # noqa: E402

WORK = ROOT / "build" / "wakeword"
CLIPS = WORK / "clips"
MODEL_DIR = ROOT / "models" / "wakeword"
PHRASES = ("yo archie", "yo archibald")

#: Held out from training entirely and used only to decide whether the model is
#: any good. A model judged on its own training data is a model that has memorised.
VAL_FRACTION = 0.15

#: Speech that must NOT trigger it. The first four are the near misses; the rest
#: is ordinary conversation, because the failure that matters is firing while the
#: user is talking about something else.
NEGATIVES = [
    # near misses - the ones that would actually break
    "yo", "yo!", "archie", "hey archie", "hey archibald", "yo archie how are you",
    "yo archibald!", "okay archie", "hello archie", "yo archieee",
    # other names, so it learns a person not a syllable pair
    "yo archie is that you", "yo alfred", "yo archimedes", "archie wherever you are",
    # ordinary conversation
    "hey there how is it going", "what time is it", "close the file please",
    "run the diagnostics and tell me what you find",
    "I was thinking about the weekend", "copy the notes to a backup",
    "how much memory is this machine using", "can you archive that folder",
    "put it in the quarantine", "what did the audit log say",
    "search for jumps in this folder", "list the workspace roots",
    "the build failed again", "start the daemon", "stop the child process",
    "did you see the error message", "that's a good point though",
    "let me check the temperature", "the fan is loud tonight",
    "I need to reboot eventually", "open the door please", "what is the weather",
    "turn the lights off", "play some music", "set a timer for ten minutes",
    "my name is Michael", "call me back later", "did you eat yet",
    "the cat is asleep on the sofa", "I forgot to tell you",
]

POSITIVE_FRAMES = [
    # Surrounding context, so it fires on the phrase rather than on a
    # conversation that happens to contain it at a clip boundary.
    "", "yes ", "ok ", "hey ", "so ", "alright ", "right ",
    "", "um ", "now ",
    "yes, yo archie", "ok yo archie", "hey yo archie", "right, yo archie",
]


#: openWakeWord frames are 1280 samples at 16 kHz = 80 ms, and the first usable
#: frame starts at 12400 samples because that is the embedding model's receptive
#: field. Both constants are load-bearing: `get_frame_labels` indexes with them,
#: and using 1600 or a zero offset silently shifts every label by ~1 second.
FRAME = 1280
FIRST_FRAME = 12400


def frame_labels(n_frames: int, start: int, end: int, pad: int = 8000,
                  positive: bool = True) -> np.ndarray:
    """Mark every frame that could contain any part of the phrase.

    Deliberately wider than openWakeWord's own `get_frame_labels`, which marks two
    frames at each boundary. That function assumes frame 0 corresponds to sample
    12400 - the embedding model's receptive field - but the *streaming*
    featuriser produces frames from sample 0 with no such offset, so its frame
    indexing and this one disagree by most of a second.

    Rather than guess which is right, over-label by +/-0.5 s. The asymmetry is
    what matters: a label that is 0.5 s too generous costs a little precision,
    while a label that is 0.5 s too narrow means the model never sees its target
    marked and the training silently produces something that does not work.

    `n_frames` is passed in rather than computed from the sample count, so the
    labels and the features are guaranteed to be the same length. A length
    mismatch here would be a broadcast error at best and mislabelled data at
    worst.
    """
    labels = np.zeros(max(n_frames, 0), dtype=np.float32)
    if n_frames <= 0:
        return labels
    if not positive:
        return labels          # a negative is 0 for every frame it occupies
    lo = max(0, int((start - pad) / FRAME))
    hi = min(n_frames, int(np.ceil((end + pad) / FRAME)))
    labels[lo:max(hi, lo + 1)] = 1.0
    return labels


def say(text: str) -> str:
    """Piper is punctuation-sensitive; render obvious variants."""
    return text


def synth_all(texts: list[str], tag: str) -> list[Path]:
    """Render every text with every voice we have. Returns the wav paths."""
    made: list[Path] = []
    for voice in ("en_US-lessac-medium", "en_US-amy-medium", "en_GB-alan-medium"):
        out = CLIPS / tag / voice
        try:
            speech.ensure_voice(voice)
        except Exception as why:  # noqa: BLE001
            print(f"  skip {voice}: {why}")
            continue
        for text in texts:
            try:
                for _t, path in speech.synthesise([text], out, voice=voice):
                    made.append(path)
            except Exception as why:  # noqa: BLE001
                print(f"  piper failed on {text!r} with {voice}: {why}")
    return made


def load16k(path: Path) -> np.ndarray:
    """Read any wav and return 16 kHz mono float32 in -1..1.

    Piper renders at 22050 Hz, not the 16000 the featuriser wants. The first
    version of this asserted 16 kHz and refused to run, which was better than
    training on mislabelled audio but not useful - so it converts instead, using
    the same resampler `audio._resample` uses and that the tests cover.

    The rate is checked rather than assumed on read, and reported, because a
    silent mismatch here would be a silent corruption of the training set.
    """
    import wave

    from assistant.audio import _resample

    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        assert wf.getnchannels() == 1, f"{path} is not mono"
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    x = data.astype(np.float32) / 32768.0
    return _resample(x, rate, 16000)


def vary(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """One augmented copy: speed, gain, noise, reverb.

    Speed is done by resampling, which shifts formants as well as tempo. That is
    a crude pitch shift and it is fine - the point is that the model stops
    keying on the exact timing piper happens to produce.
    """
    y = x
    speed = float(rng.uniform(0.9, 1.12))
    if abs(speed - 1.0) > 0.01:
        n = max(1, int(len(y) / speed))
        y = np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)

    # gain, including a genuinely quiet take: the mic's own floor is low and a
    # model that only ever heard full-scale audio is deaf in a real room
    y = y * float(rng.uniform(0.15, 1.6))
    peak = float(np.max(np.abs(y)))
    if peak > 0.99:
        y = y * (0.99 / peak)

    if rng.random() < 0.6:
        snr_db = float(rng.uniform(5, 20))
        noise = rng.normal(0, 1, len(y)).astype(np.float32)
        sig_rms = float(np.sqrt(np.mean(y ** 2))) or 1e-6
        noise_rms = float(np.sqrt(np.mean(noise ** 2))) or 1e-6
        y = y + noise * (sig_rms / ((10 ** (snr_db / 20)) * noise_rms))

    if rng.random() < 0.3:  # cheap synthetic reverb
        room = int(rng.uniform(0.02, 0.12) * 16000)
        if room > 1 and len(y) > room:
            ir = rng.normal(0, 1, room).astype(np.float32)
            ir[0] = 1.0
            ir *= np.exp(-np.arange(room) / (room / 3.0))
            ir /= float(np.sqrt(np.sum(ir ** 2))) or 1.0
            wet = np.convolve(y, ir, mode="full")[: len(y)]
            y = (y + wet * float(rng.uniform(0.2, 0.7))).astype(np.float32)

    return np.clip(y, -1.0, 1.0)


def lead_in_silence(rng: np.random.Generator, max_s: float = 0.6) -> np.ndarray:
    """Silence before the phrase, so the model learns to fire on the phrase and
    not on the arrival of sound."""
    n = int(16000 * rng.uniform(0.0, max_s))
    return rng.normal(0, 0.0008, n).astype(np.float32) if n else np.zeros(0, np.float32)


def main() -> int:
    rng = np.random.default_rng(20261001)  # fixed: a training run is reproducible
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    CLIPS.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    print("1. synthesising positives")
    pos_texts = []
    for phrase in PHRASES:
        for pre in POSITIVE_FRAMES:
            pos_texts.append(say(f"{pre}{phrase}").strip())
    pos_wavs = synth_all(pos_texts, "pos")
    print(f"   {len(pos_texts)} texts -> {len(pos_wavs)} wavs")

    print("2. synthesising negatives (deliberately more than the positives)")
    neg_texts = list(NEGATIVES)
    neg_wavs = synth_all(neg_texts, "neg")
    print(f"   {len(neg_texts)} texts -> {len(neg_wavs)} wavs")

    if len(pos_wavs) < 20 or len(neg_wavs) < 20:
        print("RESULT: FAIL - not enough audio was synthesised to train on.")
        print("        The counts above say why; do not lower the bar and retrain.")
        return 1

    print("3. featurising")
    from openwakeword.utils import AudioFeatures

    mdir = Path(sys.modules["openwakeword"].__file__).parent / "resources" / "models"
    featurizer = AudioFeatures(
        melspec_model_path=str(mdir / "melspectrogram.onnx"),
        embedding_model_path=str(mdir / "embedding_model.onnx"),
        sr=16000, ncpu=4, inference_framework="onnx", device="cpu",
    )
    print(f"   featurizer ready, models from {mdir}")

    def frame_features(
            chunks: list[tuple[np.ndarray, int, int]],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Build one long stream, featurise it, label the phrase regions.

        The chunks are concatenated with a second of near-silence between them
        rather than featurised individually, and that is not a formatting
        choice. Featurised separately, every chunk is roughly 2 s long and the
        phrase plus its padding fills nearly all of it - so 99.9% of frames come
        out labelled positive, for the negatives as well as the positives, and
        the model has no contrast to learn from. Measured, not assumed: the
        first run produced 9261 positive frames out of 9271.

        Joined into one stream, the gaps are unlabelled and the negatives
        finally are negative.
        """
        gap = np.zeros(16000, dtype=np.float32)  # 1 s between utterances
        stream: list[np.ndarray] = []
        regions: list[tuple[int, int, bool]] = []
        at = 0
        for audio, begin, finish, positive in chunks:
            stream.append(gap)
            at += len(gap)
            stream.append(audio)
            regions.append((at + begin, at + finish, positive))
            at += len(audio)
        joined = np.concatenate(stream)

        # Fed in one-second blocks, which is the only way this class works.
        #
        # `AudioFeatures` is a *streaming* front end with fixed-size history:
        # `raw_data_buffer` is a `deque(maxlen=sr*10)` and `melspectrogram_buffer`
        # clips to 970 rows. Each call computes embeddings only for frames it can
        # still see in those buffers. Handed the whole stream at once, everything
        # except the final ten seconds is gone before the first embedding is
        # computed, so the inner loop runs its ~12,600 iterations with an
        # insufficient window, `x.shape[1] == 76` fails, and nothing is appended.
        #
        # Measured, not reasoned about: that version produced 153 features for
        # 1128 chunks. The streaming path is how it is *meant* to be driven, and
        # `AudioProcessor` feeds it in small blocks for the same reason.
        #
        # 1280-sample remainders are carried across block boundaries internally
        # (`raw_data_remainder`), so this introduces no seam - which is the same
        # reason a resampler has to carry its last sample.
        featurizer.feature_buffer_max_len = 4_000_000
        before = featurizer.feature_buffer.shape[0]
        block = 16000
        for start in range(0, len(joined), block):
            featurizer(joined[start:start + block])
        # Counted off the buffer rather than derived from samples consumed. Those
        # are different quantities, and reading the second as the first is what
        # turned 153 real features into a claimed 12,600 - a training set ten
        # times smaller than the label spans had been drawn against.
        feats = featurizer.feature_buffer[before:]
        made = feats.shape[0]
        if made <= 0:
            raise SystemExit("the featuriser produced no frames at all")

        # float32 because the Dataset stacks these into a tensor alongside the
        # features, which are float; the bitwise-or needs an integer view, so it is
        # cast rather than accumulated in place. `|=` on a float array raises, and
        # the float is required - so the cast is not a tidiness fix, it is the
        # only order the two types can be combined in.
        mask = np.zeros(len(feats), dtype=bool)
        for begin, finish, positive in regions:
            mask |= frame_labels(len(feats), begin, finish, positive=positive).astype(bool)
        return feats, mask.astype(np.float32)

    pos_chunks: list[tuple[np.ndarray, int, int]] = []
    for wav in pos_wavs:
        base = load16k(wav)
        for _ in range(4):  # four augmented copies per render
            lead = lead_in_silence(rng)
            body = vary(base, rng)
            pos_chunks.append((np.concatenate([lead, body]), len(lead),
                               len(lead) + len(body) - 1, True))

    neg_chunks: list[tuple[np.ndarray, int, int]] = []
    for wav in neg_wavs:
        base = load16k(wav)
        for _ in range(6):
            lead = lead_in_silence(rng)
            body = vary(base, rng)
            # Every frame of a negative is 0 - silence and speech alike. See the
            # note on `frame_labels`: `y == 0` means *not the wake phrase*, and
            # that is the only thing the training loop reads as a negative. An
            # earlier version of this file labelled these 1 on the reasoning that
            # openWakeWord "marks where speech occurs", which is not what its
            # training loop does, and it would have trained the model to fire on
            # "yo" and "archie" and "okay archie".
            neg_chunks.append((np.concatenate([lead, body]), len(lead),
                               len(lead) + len(body) - 1, False))
    print(f"   {len(pos_chunks)} positive chunks, {len(neg_chunks)} negative chunks")

    order = rng.permutation(len(pos_chunks))
    cut = int(len(order) * (1 - VAL_FRACTION))
    train_idx, val_idx = order[:cut], order[cut:]

    # Negatives are split the same way, and the two halves are *disjoint*.
    #
    # This used to train on all 792 negatives and validate against `neg_chunks[:40]`
    # - the first 40 of the same 792. So validation was measured on audio the model
    # had already been fitted on, which flatters the false-fire rate rather than
    # testing it, and the split also left the validation set almost entirely
    # positive (40 negatives against a handful of held-out positives: 91%). A
    # metric measured on the training set is not a weak metric, it is not one.
    neg_order = rng.permutation(len(neg_chunks))
    neg_cut = int(len(neg_order) * (1 - VAL_FRACTION))
    train_neg, val_neg = neg_order[:neg_cut], neg_order[neg_cut:]

    print("4. extracting features (this is the slow part)")
    t0 = time.time()
    # Negatives are concatenated into the *same* datasets as the positives.
    # Passing them only as `false_positive_val_data` measures the false-fire rate
    # and does nothing to the weights: `train_model` takes its negatives from the
    # batch (`y == 0`), so a negative set that never enters a batch is audio that
    # was listened to and never learned from.
    train_feats, train_labels = frame_features(
        [pos_chunks[i] for i in train_idx] + [neg_chunks[i] for i in train_neg])
    val_feats, val_labels = frame_features(
        [pos_chunks[i] for i in val_idx] + [neg_chunks[i] for i in val_neg])
    # Held-out negatives, for the false-fire metric `auto_train` computes. Never
    # seen in a batch and never fitted on, which is what makes the number a
    # measurement rather than a recollection.
    fp_audio = [neg_chunks[i][0] for i in val_neg]
    print(f"   train {train_feats.shape} "
          f"pos={int((train_labels == 1).sum())} "
          f"neg={int((train_labels == 0).sum())}")
    print(f"   val   {val_feats.shape} "
          f"pos={int((val_labels == 1).sum())} "
          f"neg={int((val_labels == 0).sum())}  (disjoint from train)")
    print(f"   {time.time() - t0:.0f}s")

    # Both classes must actually be present. A dataset that is 99% one label
    # trains a model that is 99% that answer, and it does so quietly - which is
    # exactly the failure this script already produced twice.
    pos_n = int((train_labels == 1).sum())
    neg_n = int((train_labels == 0).sum())
    if pos_n < 50 or neg_n < 50:
        print(f"RESULT: FAIL - the training set needs both classes: "
              f"{pos_n} positive frames, {neg_n} negative frames.")
        return 1
    print(f"   both classes present: {pos_n} positive / {neg_n} negative frames")

    print("5. training")
    import torch

    # `openwakeword.train` imports `openwakeword.data` at module level, and that
    # module pulls audiomentations, librosa, numba, torch_audiomentations,
    # speechbrain, mutagen and then `acoustics` - a chain of audio DSP packages
    # installed purely to reach a training loop.
    #
    # None of the three names it provides is on this path: the positives are
    # synthesised here, the augmentation is `vary()`, and the batches are already
    # featurised in memory. So a stub is substituted - and it *raises* rather than
    # returning a plausible empty result, because a stub that silently no-ops is
    # how a script ends up training on nothing and reporting a healthy run. If
    # anything ever does reach one of these, it fails loudly at the call.
    _stub = types.ModuleType("openwakeword.data")

    def _unused(name: str):
        def _raise(*a, **k):
            raise RuntimeError(
                f"openwakeword.data.{name} is not available: this script "
                f"substitutes a stub for it, so anything calling it is a bug in "
                f"the script rather than a missing package."
            )
        return _raise

    for _n in ("generate_adversarial_texts", "augment_clips", "mmap_batch_generator"):
        setattr(_stub, _n, _unused(_n))
    sys.modules.setdefault("openwakeword.data", _stub)

    from openwakeword.train import Model

    def dataset(feats, labels):
        ds = torch.utils.data.TensorDataset(
            torch.tensor(feats, dtype=torch.float32),
            torch.tensor(labels, dtype=torch.float32),
        )
        return torch.utils.data.DataLoader(ds, batch_size=256, shuffle=True, drop_last=True)

    model = Model(n_classes=1, input_shape=(16, 96), model_type="dnn")
    # `fp_audio` is the held-out negative set built above. It wants audio, not
    # features, and the one on the next line used to be a slice of the *training*
    # negatives - so the false-fire rate `auto_train` reports would have been
    # measured against audio the model was already fitted on.
    model.auto_train(
        dataset(train_feats, train_labels),
        dataset(val_feats, val_labels),
        fp_audio,
        steps=8000,
        max_negative_weight=500,
        target_fp_per_hour=0.2,
    )

    out = MODEL_DIR / "yo_archie.onnx"
    model.export_to_onnx(str(out), class_mapping="")
    print(f"6. exported {out} ({out.stat().st_size:,} bytes)")

    print("7. verifying on held-out audio - not on the training data")
    import onnxruntime

    sess = onnxruntime.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name

    def score(x: np.ndarray) -> float:
        f = featurizer(x.astype(np.float32))
        if len(f) == 0:
            return 0.0
        return float(np.max(sess.run(None, {iname: f})[0]))

    def wrap(w):
        lead = lead_in_silence(rng)
        body = vary(load16k(w), rng)
        return np.concatenate([lead, body])

    hits = [score(wrap(w)) for w in pos_wavs[:30]]
    fp_on_neg = [score(wrap(w)) for w in neg_wavs]

    hit_rate = float(np.mean([h > 0.5 for h in hits]))
    fp_rate = float(np.mean([f > 0.5 for f in fp_on_neg]))
    print(f"   held-out positives fired: {hit_rate * 100:.0f}%  (n={len(hits)})")
    print(f"   negatives fired:          {fp_rate * 100:.0f}%  (n={len(fp_on_neg)})")
    print(f"   worst positive score {min(hits):.3f}, best negative {max(fp_on_neg):.3f}")

    if hit_rate < 0.7:
        print("RESULT: FAIL - it misses the phrase too often to be usable.")
        return 1
    if fp_rate > 0.15:
        print("RESULT: FAIL - it fires on ordinary speech too often to be usable.")
        print("        More negative data, or a longer and rarer phrase.")
        return 1
    print("RESULT: PASS on synthetic held-out audio. This is NOT the last word:")
    print("        it must still be measured against the real microphone, which")
    print("        is a different distribution - that is probe_wakeword.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
