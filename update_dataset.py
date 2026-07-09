import os
from huggingface_hub import HfApi, hf_hub_download

import scraper

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------

HF_TOKEN = os.environ["HF_TOKEN"]

REPO_ID = "mahad00/seed-price-dataset"

CSV_FILE = "seed_prices.csv"

api = HfApi(token=HF_TOKEN)


# ----------------------------------------------------
# Download previous CSV
# ----------------------------------------------------

try:
    downloaded = hf_hub_download(
        repo_id=REPO_ID,
        filename=CSV_FILE,
        repo_type="dataset",
        token=HF_TOKEN,
    )

    import shutil

    shutil.copy(downloaded, CSV_FILE)

    print("Downloaded previous CSV.")

except Exception:
    print("No existing CSV found. A new one will be created.")


# ----------------------------------------------------
# Run scraper
# ----------------------------------------------------

print("Running scraper...")

scraper.run_once(CSV_FILE)

print("Scraping completed.")


# ----------------------------------------------------
# Upload updated CSV
# ----------------------------------------------------

print("Uploading CSV...")

api.upload_file(
    path_or_fileobj=CSV_FILE,
    path_in_repo=CSV_FILE,
    repo_id=REPO_ID,
    repo_type="dataset",
)

print("Upload completed successfully.")
