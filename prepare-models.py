"""Download official embedding/reranker weights before offline Docker startup."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--cache-dir', required=True)
args = parser.parse_args()
cache = Path(args.cache_dir).resolve()
cache.mkdir(parents=True, exist_ok=True)
os.environ['MODEL_CACHE'] = str(cache)
os.environ['MODEL_OFFLINE'] = 'false'
from incident_simulator.retrieval import Retriever
retriever = Retriever()
retriever.load_models()
print(f'Models ready: {cache}')
