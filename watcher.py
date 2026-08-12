import base64
import json
import os
import re
import time
import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from dotenv import load_dotenv
load_dotenv()  # Loads environment variables from .env file

# Configuration
WATCH_DIRECTORY = os.getenv("WATCH_DIRECTORY", "/home/ubuntu/Downloads/TikTok Live")
POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")
COLLECTION_NAME = os.getenv("POCKETBASE_COLLECTION_NAME", "auction_items")

# EvoLink AI Configuration
EVOLINK_API_TOKEN = os.getenv("EVOLINK_API_TOKEN")
MODEL_NAME = os.getenv("EVOLINK_MODEL_NAME", "gemini-3.5-flash-lite")
EVOLINK_URL = os.getenv(
    "EVOLINK_URL",
    f"https://direct.evolink.ai/v1beta/models/{MODEL_NAME}:generateContent",
)

if not EVOLINK_API_TOKEN:
    raise RuntimeError("EVOLINK_API_TOKEN is not set. Export it before running watcher.py.")


def normalize_item_number(raw_val: str) -> str:
    """
    Parses and standardizes item numbers into 'Part X Item #Y' format, 
    even if the AI returns them out of order (e.g., '#10 Part 1').
    """
    if not raw_val:
        return raw_val

    text = str(raw_val).strip()

    # Extract Part number and Item number using Regex
    part_match = re.search(r'Part\s*(\d+)', text, re.IGNORECASE)
    item_match = re.search(r'(?:Item\s*#?|#)\s*(\d+)', text, re.IGNORECASE)

    part_num = part_match.group(1) if part_match else None
    
    # Avoid capturing part_num twice if regex overlaps
    if item_match:
        matched_item = item_match.group(1)
        item_num = matched_item if matched_item != part_num else None
    else:
        item_num = None

    # If both Part and Item were extracted, return standardized syntax
    if part_num and item_num:
        return f"Part {part_num} Item #{item_num}"

    # Fallback to cleaned text if regex does not match both fields
    return text


class ScreenshotHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(('.png', '.jpg', '.jpeg')):
            return

        file_path = event.src_path
        print(f"New screenshot detected: {file_path}")

        time.sleep(1.0)  # Wait for file write completion

        try:
            extracted_data = self.process_image_with_gemini(file_path)
            print(f"Extracted Data: {extracted_data}")

            self.save_to_pocketbase(file_path, extracted_data)

        except Exception as e:
            print(f"Error processing {file_path}: {e}")

    def process_image_with_gemini(self, image_path):
        # Read and base64 encode the image
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

        prompt_text = (
            "Analyze this screenshot from a live shopping auction.\n"
            "Extract the following fields and return ONLY a valid JSON object with these exact keys:\n"
            "- 'item_number': The item text formatted strictly as 'Part <X> Item #<Y>'.\n"
            "  * Example 1: If screenshot shows '#10 Part 1', return 'Part 1 Item #10'.\n"
            "  * Example 2: If screenshot shows 'Part 2 Item 168', return 'Part 2 Item #168'.\n"
            "- 'name': The clear product name. Validate if the product is real and correct it if necessary.\n"
            "- 'retail_price': Search online or from screen to find out the numeric retail price in USD as a float or string (e.g., 29.99)."
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt_text},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": encoded_image
                            }
                        }
                    ]
                }
            ]
        }

        headers = {
            "Authorization": f"Bearer {EVOLINK_API_TOKEN}",
            "Content-Type": "application/json"
        }

        response = requests.post(EVOLINK_URL, json=payload, headers=headers)
        
        if response.status_code != 200:
            raise Exception(f"EvoLink API error: {response.text}")

        res_json = response.json()
        
        try:
            text_content = res_json['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError):
            raise Exception(f"Unexpected response structure from API: {res_json}")
        
        # Strip markdown code block formatting if returned by the model
        clean_json = text_content.replace('```json', '').replace('```', '').strip()
        data = json.loads(clean_json)

        # Apply deterministic normalization step
        if "item_number" in data:
            data["item_number"] = normalize_item_number(data["item_number"])

        return data

    def save_to_pocketbase(self, file_path, data):
        url = f"{POCKETBASE_URL}/api/collections/{COLLECTION_NAME}/records"
        
        payload = {
            "item_number": data.get("item_number"),
            "name": data.get("name"),
            "retail_price": data.get("retail_price")
        }

        with open(file_path, "rb") as f:
            files = {"screenshot": (os.path.basename(file_path), f)}
            response = requests.post(url, data=payload, files=files)

        if response.status_code in [200, 201]:
            print(f"Successfully saved item {data.get('item_number')} to PocketBase!")
        else:
            print(f"Failed to save to PocketBase: {response.text}")


if __name__ == "__main__":
    os.makedirs(WATCH_DIRECTORY, exist_ok=True)
    
    event_handler = ScreenshotHandler()
    observer = Observer()
    observer.schedule(event_handler, path=WATCH_DIRECTORY, recursive=False)

    print(f"Starting watcher on directory: {WATCH_DIRECTORY}...")
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()