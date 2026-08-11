# TikTok PocketBase Watcher

Automates the flow from screenshot to structured record:

1. Watches a folder for new image files.
2. Sends each image to EvoLink's Gemini endpoint for extraction.
3. Saves the extracted data and screenshot to PocketBase.
4. Moves processed images into an `archive/` folder.

## Requirements

- Python 3.10 or newer
- A running PocketBase instance
- An EvoLink API token

## Setup

```bash
cd /Users/nikhilkhadgi/Projects/tiktok-pocketbase
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a local environment file from the example:

```bash
cp .env.example .env
```

Then export the variables in your shell before starting the watcher:

```bash
export EVOLINK_API_TOKEN="your-token-here"
export WATCH_DIRECTORY="/home/ubuntu/Downloads/TikTok Live"
export POCKETBASE_URL="http://127.0.0.1:8090"
```

You can optionally override `EVOLINK_MODEL_NAME`, `EVOLINK_URL`, and `POCKETBASE_COLLECTION_NAME` the same way.

## PocketBase Collection

Create a collection named `auction_items` with these fields:

- `item_number` - Text
- `name` - Text
- `retail_price` - Number
- `screenshot` - File, single upload

If you want the script to create records without authentication, keep the create rule empty.

## Run

```bash
source venv/bin/activate
python watcher.py
```

The watcher will create the watch directory if needed and process new `.png`, `.jpg`, and `.jpeg` files as they arrive.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EVOLINK_API_TOKEN` | none | Required EvoLink bearer token |
| `EVOLINK_MODEL_NAME` | `gemini-3.5-flash-lite` | Gemini model name |
| `EVOLINK_URL` | EvoLink Gemini endpoint | Optional full override for the API URL |
| `WATCH_DIRECTORY` | `/home/ubuntu/Downloads/TikTok Live` | Folder that the watcher monitors |
| `POCKETBASE_URL` | `http://127.0.0.1:8090` | PocketBase base URL |
| `POCKETBASE_COLLECTION_NAME` | `auction_items` | PocketBase collection name |

## Project Files

- `watcher.py` - main file watcher and ingestion script
- `requirements.txt` - Python dependencies
- `.env.example` - sample environment configuration
- `.gitignore` - ignores local secrets, virtual environments, and generated files

## Notes

- The API token is no longer hardcoded in the source.
- Processed images are moved into an `archive/` directory next to the source file.
- If you want `.env` loading inside Python, add that explicitly before relying on the file at runtime.