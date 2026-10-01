"""Does the exported wake word actually work, on audio it has never seen?

The trainer's step 7 scored `pos_wavs[:30]` - thirty of the eighty-four positives
that were in the training set. That is a third instance of the same defect D55
describes, and the one that would have mattered most: the number printed would
have been a recollection dressed as a measurement.

So this builds its own positives. They are synthesised here, from sentences and
prefixes that appear nowhere in `train_wakeword.py`, which makes them held-out by
construction rather than by an index range I might have got wrong.

Three numbers, each with its denominator, because a rate without "out of how
many" is a number rather than a measurement:

  * recall        - does it fire when the phrase is said
  * false fires   - does it stay quiet on ordinary speech (the hard direction;
                    silence is easy, conversation is not)
  * the scores themselves, so a near-miss is visible before it becomes a failure

The onnx file is loaded through onnxruntime rather than read from the torch
model, because the onnx file is what `voice.watch` will load. Verifying the
checkpoint and shipping the export are two different claims.

`PYTHONIOENCODING` is set before torch is imported, for the reason D56 records:
torch 2.14's ONNX exporter prints a check mark and a cp1252 console raises on it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT))
from train_wakeword import (  # noqa: E402
    WINDOW, lead_in_silence, load16k, synth_all, vary,
)

SR = 16000


def featurise_stream(featurizer, x) -> "np.ndarray":
    """One stream in, (n, 1536) prediction rows out.

    Deliberately the same shape as the trainer's `frame_features`, minus the
    labelling: `AudioFeatures` is a streaming front end with a hard ten-second
    history (`raw_data_buffer` is a `deque(maxlen=sr*10)`), so the stream is fed
    in one-second blocks and the rows are counted off `feature_buffer` rather than
    derived from samples consumed. Those are different quantities, and reading the
    second as the first is what once turned 153 real features into a claimed
    12,600.

    A prediction is sixteen 80 ms frames flattened, because a wake word cannot be
    identified from 80 ms of audio. `sliding_window_view(raw, WINDOW, axis=0)`
    returns feature-major, so the transpose is not optional - without it the
    96 dimensions of all sixteen frames interleave and the columns end up in an
    order the network has no name for.
    """
    import numpy as np

    featurizer.feature_buffer_max_len = 4_000_000
    before = featurizer.feature_buffer.shape[0]
    block = 16000
    for start in range(0, len(x), block):
        featurizer(x[start:start + block].astype(np.float32))
    raw = featurizer.feature_buffer[before:]
    if raw.shape[0] <= WINDOW:
        return np.zeros((0, WINDOW * raw.shape[1] if raw.ndim > 1 else WINDOW * 96),
                        dtype=np.float32)
    return np.lib.stride_tricks.sliding_window_view(
        raw, WINDOW, axis=0
    ).transpose(0, 2, 1).reshape(
        len(raw) - WINDOW + 1, WINDOW * raw.shape[1]
    ).astype(np.float32)
#: Where openWakeWord puts a hit. Taken from the trained model rather than
#: assumed: `threshold` was recorded in the checkpoint.
THRESHOLD = 0.5

#: Held-out phrases. "yo archie" / "yo archibald" with prefixes that do NOT appear
#: in the trainer's list, so nothing here was seen even once.
FRESH_POSITIVE_TEXTS = [
    f"{p}yo archie"
    for p in ("", "right ", "hello there ", "listen ", "okay ", "er ", "so ")
] + [
    f"{p}yo archibald"
    for p in ("", "well ", "hang on ", "um ", "now ")
]

#: Held-out negatives, weighted toward the awkward cases: the name on its own, the
#: greeting without it, and names that merely start the same way.
FRESH_NEGATIVE_TEXTS = [
    "archie", "archibald", "yo", "hey", "okay", "so", "now",
    "alright then", "come here", "sit down", "what time is it",
    "archie told me", "yo mama", "my name is archie",
    "alphabet soup", "archimedes principle", "alfred hitchcock",
    "the archibald was long", "yo yoyo", "hey arthur",
    "good morning everyone", "can you hear me now",
    "i think it is going to rain", "let me check the file",
    "the system is using four gigabytes", "open the door",
    "turn the lights off please", "where did you put it",
    "that is not what i asked for", "start the download",
    "how much memory is left", "copy notes to a backup",
    "read the quarterly report", "list the files in here",
    "what is the weather like", "stop the timer now",
    "call me back in ten minutes", "the build failed again",
    "i would like some water", "never mind forget it",
]

VOICES = ["en_US-lessac-medium", "en_US-amy-medium", "en_GB-alan-medium"]


def main() -> int:
    import numpy as np
    import onnxruntime

    from openwakeword.utils import AudioFeatures

    model_dir = (Path(sys.modules["openwakeword"].__file__).parent
                 / "resources" / "models")
    featurizer = AudioFeatures(
        melspec_model_path=str(model_dir / "melspectrogram.onnx"),
        embedding_model_path=str(model_dir / "embedding_model.onnx"),
        sr=16000, ncpu=4, inference_framework="onnx", device="cpu",
    )

    onnx_path = ROOT / "models" / "wakeword" / "yo_archie.onnx"
    if not onnx_path.exists():
        print(f"no model at {onnx_path}", file=sys.stderr)
        return 1
    sess = onnxruntime.InferenceSession(str(onnx_path),
                                        providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    print(f"model {onnx_path.name} ({onnx_path.stat().st_size:,} bytes), "
          f"threshold {THRESHOLD}")

    rng = np.random.default_rng(20261001)

    def score(x: np.ndarray) -> float:
        """Highest score over the stream, featurised the way training did it."""
        feats = featurise_stream(featurizer, x.astype(np.float32))
        if len(feats) == 0:
            return 0.0
        # One row at a time: the exported graph declares a batch of 1, and
        # `sess.run` would rather refuse an n-row batch than quietly reshape it.
        scores = np.concatenate([
            sess.run(None, {iname: feats[i:i + 1].astype(np.float32)})[0]
            for i in range(len(feats))
        ])
        return float(np.max(scores))

    def rendered(texts: list[str], tag: str) -> list[np.ndarray]:
        wavs = synth_all(texts, tag)
        out = []
        for w in wavs:
            body = vary(load16k(w), rng)
            out.append(np.concatenate([lead_in_silence(rng), body]))
        return out

    print(f"\n1. synthesising {len(FRESH_POSITIVE_TEXTS)} held-out positives...")
    positives = rendered(FRESH_POSITIVE_TEXTS, "verify_pos")
    print(f"2. synthesising {len(FRESH_NEGATIVE_TEXTS)} held-out negatives...")
    negatives = rendered(FRESH_NEGATIVE_TEXTS, "verify_neg")

    print("\n3. scoring")
    pos_scores = [score(p) for p in positives]
    neg_scores = [score(n) for n in negatives]

    hits = sum(1 for s in pos_scores if s > THRESHOLD)
    fires = sum(1 for s in neg_scores if s > THRESHOLD)

    print(f"   positives fired {hits}/{len(pos_scores)} "
          f"({100 * hits / max(1, len(pos_scores)):.0f}%)")
    print(f"   negatives fired {fires}/{len(neg_scores)} "
          f"({100 * fires / max(1, len(neg_scores)):.0f}%)")
    print(f"   positive scores: min {min(pos_scores):.3f} "
          f"median {float(np.median(pos_scores)):.3f} max {max(pos_scores):.3f}")
    print(f"   negative scores: max {max(neg_scores):.3f} "
          f"median {float(np.median(neg_scores)):.3f}")

    worst = sorted(zip(neg_scores, range(len(neg_scores))),
                   reverse=True)[:5]
    print("   highest-scoring negatives:")
    for s, i in worst:
        print(f"     {s:.3f}  {FRESH_NEGATIVE_TEXTS[i]!r}")

    print()
    if hits / max(1, len(pos_scores)) < 0.7:
        print("RESULT: FAIL - it misses the phrase too often to be usable.")
        return 1
    if fires / max(1, len(neg_scores)) > 0.15:
        print("RESULT: FAIL - it fires on ordinary speech too often to be usable.")
        print("        More negative data, or a longer and rarer phrase.")
        return 1
    print("RESULT: PASS on synthesised held-out audio.")
    print("        This is still NOT the last word: piper voices are not a mouth,")
    print("        and a room is not a quiet studio. probe_wakeword_real.py measures")
    print("        that, and it needs the user to speak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())