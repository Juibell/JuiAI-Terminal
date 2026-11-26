"""CPU-only semantic ranking for already authorized local candidates."""

import hashlib
import logging
import threading
from collections import OrderedDict
from functools import lru_cache

import numpy as np

from .config import ROOT, load_settings

DIRECTORY = ROOT / "02_Models/IBM-Granite-Embedding-311M-R2"
WEIGHTS = DIRECTORY / "onnx/model_quint8_avx2.onnx"
TOKENIZER = DIRECTORY / "tokenizer.json"
MEMORY_MIN_SIMILARITY = 0.84
LOGGER = logging.getLogger(__name__)
LOCK = threading.Lock()


class Granite:
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(TOKENIZER))
        self.tokenizer.enable_padding(pad_id=0, pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=384)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(WEIGHTS), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.inputs = {item.name for item in self.session.get_inputs()}
        self.vectors = OrderedDict()

    def encode(self, texts):
        batch = self.tokenizer.encode_batch(texts)
        inputs = {
            name: np.array([getattr(item, attr) for item in batch], dtype=np.int64)
            for name, attr in (
                ("input_ids", "ids"),
                ("attention_mask", "attention_mask"),
                ("token_type_ids", "type_ids"),
            )
            if name in self.inputs
        }
        hidden = self.session.run(None, inputs)[0]
        if hidden.ndim != 3 or hidden.shape[-1] != 768:
            raise ValueError("Unexpected embedding output")
        vectors = hidden[:, 0, :].copy()
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        return vectors

    def scores(self, query, documents):
        query_vector = self.encode([query[:4000]])[0]
        clipped = [text[:4000] for text in documents]
        keys = [hashlib.sha256(text.encode()).digest() for text in clipped]
        missing = list(
            {
                key: text
                for key, text in zip(keys, clipped, strict=True)
                if key not in self.vectors
            }.items()
        )
        for offset in range(0, len(missing), 16):
            group = missing[offset : offset + 16]
            for (key, _), vector in zip(
                group, self.encode([text for _, text in group]), strict=True
            ):
                self.vectors[key] = vector
        matrix = np.stack([self.vectors[key] for key in keys])
        for key in keys:
            self.vectors.move_to_end(key)
        while len(self.vectors) > 512:
            self.vectors.popitem(last=False)
        result = matrix @ query_vector
        if not np.isfinite(result).all():
            raise ValueError("Non-finite semantic scores")
        return result.tolist()


@lru_cache(maxsize=1)
def engine():
    if not WEIGHTS.is_file() or not TOKENIZER.is_file():
        raise FileNotFoundError("Granite model files missing")
    return Granite()


def scores(query, documents):
    if (
        not query.strip()
        or not documents
        or load_settings().get("retrieval", {}).get("model") != "granite-311m-r2"
    ):
        return None
    try:
        with LOCK:
            return engine().scores(query, documents)
    except Exception as error:  # noqa: BLE001 -- Never log private inference inputs.
        LOGGER.warning("Local Granite unavailable (%s)", type(error).__name__)
        return None
