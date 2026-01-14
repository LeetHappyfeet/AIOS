# accumulator/web/writer.py

import json
from datetime import datetime
from pathlib import Path


class JSONLWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def write(self, record: dict):
        date = datetime.utcnow().strftime("%Y-%m-%d")
        path = self.output_dir / f"{date}.jsonl"

        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
