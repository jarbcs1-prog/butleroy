import sys
sys.path.insert(0, r'src')
from assistant.modelconfig import load, available_models
cfg = load()
print('Tiers loaded:')
for n, t in cfg.tiers.items():
    print(f'  {n}: {t.model}')
print('Provenance:', cfg.provenance)
print()
print('Available on disk:')
for m in available_models():
    print(f'  {m["name"]}: {m["est_vram_bytes"]:,} est VRAM')