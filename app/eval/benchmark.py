"""HuggingFace invoice-extraction-benchmark loader (§9) — LOADER CODE ONLY.

The benchmark (181 docs + answer keys) is NOT downloaded by this scaffold
and must never be downloaded implicitly. `load_benchmark()` raises a clear
error until the dataset is present locally; call it with download=True
explicitly to fetch it (requires the `datasets` package).
"""

from __future__ import annotations

import os

HF_DATASET = "invoice-extraction-benchmark"  # placeholder id — verify on HF Hub
LOCAL_CACHE_DIR = os.environ.get("ELRIC_BENCHMARK_DIR", "./data/benchmark")


class BenchmarkNotDownloaded(RuntimeError):
    pass


def load_benchmark(download: bool = False) -> dict:
    """Load the HF invoice-extraction benchmark.

    download=False (default): returns only the descriptor; raises
    BenchmarkNotDownloaded if the local cache is absent.
    download=True: fetches via `datasets.load_dataset` into LOCAL_CACHE_DIR.
    """
    if download:
        from datasets import load_dataset  # lazy import; optional dep

        ds = load_dataset(HF_DATASET, cache_dir=LOCAL_CACHE_DIR)
        return {"dataset": HF_DATASET, "splits": {k: len(v) for k, v in ds.items()}}
    if not os.path.isdir(LOCAL_CACHE_DIR):
        raise BenchmarkNotDownloaded(
            f"benchmark not downloaded — set ELRIC_BENCHMARK_DIR or call "
            f"load_benchmark(download=True) to fetch {HF_DATASET} from the HF Hub"
        )
    return {"dataset": HF_DATASET, "cache_dir": LOCAL_CACHE_DIR, "status": "cached"}
