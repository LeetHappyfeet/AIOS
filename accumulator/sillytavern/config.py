from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
REJECTED_DIR = BASE_DIR / "rejected"
STATE_FILE = BASE_DIR / ".ingest_state.json"

INPUT_DIR.mkdir(parents=True, exist_ok=True)
REJECTED_DIR.mkdir(parents=True, exist_ok=True)

ACCUMULATOR_ID = "sillytavern_jsonl_v1"
SOURCE_KIND = "sillytavern_chat_log"
