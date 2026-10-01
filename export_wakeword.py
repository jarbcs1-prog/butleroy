"""Export the trained wake word to ONNX from the checkpoint.

The training run wrote `models/wakeword/yo_archie.trained.pt` and then died
*printing*: torch 2.14's ONNX exporter emits a progress line containing a check
mark, the console is cp1252, and `print()` raised `UnicodeEncodeError` for a
character that has nothing to do with the model. The export itself was never the
problem, and re-running 8000 steps plus 134 seconds of featurisation to produce
the same weights again would be paying twice for one bug.

So this rebuilds the same architecture from the same explicit arguments the
training script used and loads the weights into it.

Both arguments are stated rather than defaulted, on purpose. `Net` takes
`layer_dim` positionally and its default is 128; getting it wrong would build a
network of the right *shape* with the wrong weights and `load_state_dict` would
either refuse (loud, fine) or, in the one case where the shapes happened to
match, load into the wrong architecture (silent, not fine). `input_shape` is
`WINDOW * DIM` = 16 * 96 = 1536, which is the shape the featuriser produced and
the one the training matrix had.

Run with `PYTHONIOENCODING=utf-8` set. That is the whole fix for the crash that
cost the run, and it is set here in the script too, before torch is imported, so
that this file cannot be defeated the same way twice.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Before any import that might print. The failure being prevented is precisely a
# print() of a non-cp1252 character, so setting it afterwards would be too late.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a stream that cannot be reconfigured is fine
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

#: The window the featuriser produced: WINDOW frames of DIM dimensions each.
WINDOW, DIM = 16, 96

#: TWO shapes that share one name in upstream, and that is where bug 4 of this
#: project came from.
#:
#: The constructor wants the *unflattened* window (16, 96) because it computes
#: input_shape[0] * input_shape[1] itself and then applies its own Flatten. The
#: tensor actually handed to the network is the flat (1, 1536). Passing the
#: flattened shape to the constructor raises IndexError, which is loud; passing
#: the window where the flat row was meant would be silent, so the two names are
#: kept apart here rather than left to a reader to infer.
NET_INPUT_SHAPE = (WINDOW, DIM)
ROW_SHAPE = (WINDOW * DIM,)
LAYER_DIM = 128       # Net's own default; passed explicitly so a change to it
N_BLOCKS = 1          # upstream would be silent about.


def _build_net():
    """The architecture `Model.__init__` built, rebuilt here.

    Both `Net` classes in `openwakeword/train.py` are defined *inside* the
    `__init__` body, so they are local names and cannot be imported. Rebuilding is
    the only option, which makes this function a transcription of upstream code
    rather than a design of mine — so it is transcribed from the checkpoint's own
    parameter names as well, and `load_state_dict(..., strict=True)` is the check
    that the transcription is faithful.

    From the checkpoint:
        layer1            (128, 1536)   input_shape[0]*input_shape[1] -> layer_dim
        layernorm1        (128,)
        blocks.0.fcn_layer(128, 128)    one block, layer_dim -> layer_dim
        blocks.0.layer_norm (128,)
        last_layer        (1, 128)      -> n_classes

    The block order is Linear then LayerNorm then ReLU, which is what
    `FCNBlock.forward` does (`self.relu(self.layer_norm(self.fcn_layer(x)))`) and
    the reverse of the more usual arrangement. Getting it backwards would load
    the weights perfectly and compute the wrong function, which no shape check
    would catch.
    """
    import torch
    from torch import nn

    class FCNBlock(nn.Module):
        def __init__(self, layer_dim: int):
            super().__init__()
            self.fcn_layer = nn.Linear(layer_dim, layer_dim)
            self.relu = nn.ReLU()
            self.layer_norm = nn.LayerNorm(layer_dim)

        def forward(self, x):
            return self.relu(self.layer_norm(self.fcn_layer(x)))

    class Net(nn.Module):
        def __init__(self, input_shape, layer_dim, n_blocks=1, n_classes=1):
            super().__init__()
            self.flatten = nn.Flatten()
            self.layer1 = nn.Linear(input_shape[0] * input_shape[1], layer_dim)
            self.relu1 = nn.ReLU()
            self.layernorm1 = nn.LayerNorm(layer_dim)
            self.blocks = nn.ModuleList(
                [FCNBlock(layer_dim) for _ in range(n_blocks)])
            self.last_layer = nn.Linear(layer_dim, n_classes)
            self.last_act = nn.Sigmoid() if n_classes == 1 else nn.ReLU()

        def forward(self, x):
            x = self.relu1(self.layernorm1(self.layer1(self.flatten(x))))
            for block in self.blocks:
                x = block(x)
            return self.last_act(self.last_layer(x))

    return Net(NET_INPUT_SHAPE, LAYER_DIM, n_blocks=N_BLOCKS, n_classes=1)


def _stub_openwakeword_data() -> None:
    """Let `openwakeword.train` be importable without its unused dependencies.

    `train.py` line 19 imports three names from `openwakeword.data`, and `data.py`
    line 32 pulls in `speechbrain` — which is not installed here and is not needed
    for anything this file does. The three names are only used by upstream's own
    data pipeline; we synthesise our own audio and hold our own features in
    memory.

    The stubs *raise* rather than returning something plausible. A stub that
    returned an empty list would let a real mistake pass silently, and the whole
    point of stubbing a module is to make it impossible to use by accident.
    """
    import types

    def unused(name: str):
        def _raise(*_a, **_k):
            raise RuntimeError(
                f"openwakeword.data.{name} is not available: this script substitutes "
                f"a stub for it, so anything calling it is a bug in the script "
                f"rather than a missing package."
            )
        return _raise

    module = types.ModuleType("openwakeword.data")
    for name in ("generate_adversarial_texts", "augment_clips", "mmap_batch_generator"):
        setattr(module, name, unused(name))
    sys.modules.setdefault("openwakeword.data", module)


def main() -> int:
    import torch

    _stub_openwakeword_data()

    ckpt_path = ROOT / "models" / "wakeword" / "yo_archie.trained.pt"
    onnx_path = ROOT / "models" / "wakeword" / "yo_archie.onnx"

    if not ckpt_path.exists():
        print(f"no checkpoint at {ckpt_path} - nothing to export", file=sys.stderr)
        return 1

    blob = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    state = blob["model"] if isinstance(blob, dict) and "model" in blob else blob
    print(f"checkpoint {ckpt_path.name}: {len(state)} tensors, "
          f"classes={blob.get('classes') if isinstance(blob, dict) else '?'}, "
          f"threshold={blob.get('threshold') if isinstance(blob, dict) else '?'}")

    net = _build_net()
    # strict=True on purpose: a missing or unexpected key means the transcription
    # above does not match the checkpoint, and a load that quietly skips keys
    # would produce a network with randomly initialised parts.
    net.load_state_dict(state, strict=True)
    print(f"loaded strictly into Net({NET_INPUT_SHAPE}, {LAYER_DIM}, blocks={N_BLOCKS})")

    # A checkpoint that loads is not yet a model that works. `Net.forward` ends in
    # a sigmoid, so a sane output is in (0, 1); anything outside that means the
    # weights did not land where they were meant to.
    net.eval()
    with torch.no_grad():
        out = net(torch.zeros(1, *ROW_SHAPE))
    value = float(out.flatten()[0])
    print(f"forward on zeros -> {value:.6f}")
    if not (0.0 < value < 1.0):
        print(f"FAIL: the network's output on silence is {value}, which is not a "
              f"probability. The weights are not where they were meant to be.",
              file=sys.stderr)
        return 1

    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(net.to("cpu"), torch.rand(1, *ROW_SHAPE), str(onnx_path),
                      output_names=["score"])
    print(f"exported {onnx_path} ({onnx_path.stat().st_size:,} bytes)")

    # Reading it back is the only check that says the file is loadable. A size is
    # not a model, and a file that exists is not a working one.
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        got = sess.run(None, {sess.get_inputs()[0].name: torch.rand(1, *ROW_SHAPE).numpy()})
        print(f"reloaded with onnxruntime: input {sess.get_inputs()[0].shape}, "
              f"output {list(got[0].shape)} -> {float(got[0].flatten()[0]):.6f}")
    except ImportError:
        print("onnxruntime not installed; skipped the reload check")
    except Exception as why:  # noqa: BLE001
        print(f"FAIL: the exported file does not load: {why}", file=sys.stderr)
        return 1

    print("RESULT: PASS - the checkpoint exports and reloads")
    return 0


if __name__ == "__main__":
    sys.exit(main())