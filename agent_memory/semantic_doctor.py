"""The weekly doctor's embedding step (spec 論文借鏡改善 B1), run as a child
process under the interpreter that has the semantic extra (numpy,
onnxruntime, tokenizers); the doctor itself never imports them.

Reads {"root", "texts": {id: [section, ...]}, "floor"} as JSON on stdin and
prints {"pairs": [[id, id, cosine], ...], "count", "seconds", "peak_rss_kb"}:
every pair whose cosine reaches the floor. A record's vector is the mean of
its sections' vectors, each embedded on its own: the model was trained on
short passages, and one long concatenation would let the first section
decide. Only the local model is used; nothing is downloaded, and the
semantic activation gate for hooks does not apply, since a person reviews
the output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import sys
import time

BATCH = 4
# The model was trained on passages of 128 tokens; a section is cut there,
# which also bounds the attention memory of each batch.
MAX_TOKENS = 128


def main() -> int:
    started = time.monotonic()
    payload = json.load(sys.stdin)
    root = Path(str(payload["root"])).expanduser()
    texts = {str(key): [str(s) for s in value if str(s).strip()] for key, value in dict(payload["texts"]).items()}
    floor = float(payload.get("floor", 0.5))
    max_tokens = int(payload.get("max_tokens", MAX_TOKENS))
    from .semantic import _model_root, _profile, verify_model_artifacts

    problem = verify_model_artifacts(root)
    if problem:
        print(json.dumps({"skipped": f"模型檢查沒過（{problem}）"}, ensure_ascii=False))
        return 0
    import numpy as np

    from .embedding import LocalOnnxEncoder

    encoder = LocalOnnxEncoder(_model_root(root), _profile(), threads=2, max_length=max_tokens)
    ids = [record_id for record_id, sections in texts.items() if sections]
    flat = [section for record_id in ids for section in texts[record_id]]
    vectors = list(encoder.embed(flat, role="passage", batch_size=BATCH))
    rows = []
    at = 0
    for record_id in ids:
        count = len(texts[record_id])
        mean = np.mean(np.stack(vectors[at:at + count]), axis=0)
        at += count
        rows.append(mean / max(float(np.linalg.norm(mean)), 1e-12))
    pairs = []
    if rows:
        matrix = np.stack(rows)
        scores = matrix @ matrix.T
        for i, j in zip(*np.nonzero(np.triu(scores >= floor, k=1))):
            pairs.append([ids[int(i)], ids[int(j)], round(float(scores[i, j]), 3)])
    print(json.dumps({
        "pairs": pairs, "count": len(ids), "sections": len(flat),
        "seconds": round(time.monotonic() - started, 1),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    raise SystemExit(main())
