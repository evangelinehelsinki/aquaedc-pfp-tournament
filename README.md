# aquaedc-pfp-tournament

A system for collecting images from your bookmarks/favorites across Twitter, Reddit, and DeviantArt, then running a tournament bracket to let an AI instance pick its own profile picture.

## How it works

1. **Scrape** images from your saved/bookmarked content across platforms
2. **Preprocess** images: detect caption-style layouts and crop them, filter NSFW content
3. **Tournament** bracket where Claude compares pairs of images and picks favorites
4. **Winner** emerges after log₂(n) rounds of single-elimination
5. **Credit info** is saved so you can properly attribute the artist

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/evangelinehelsinki/aquaedc-pfp-tournament.git
cd aquaedc-pfp-tournament

python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

pip install -r requirements.txt

# For browser automation (Twitter and DeviantArt scrapers)
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with:
- **Reddit**: Create an app at https://www.reddit.com/prefs/apps (script type)
- **Anthropic**: Your API key from https://console.anthropic.com

### 3. Get your Twitter archive (for Twitter scraping)

1. Go to https://twitter.com/settings/download_your_data
2. Request your archive
3. Wait for email, download, and extract the zip
4. Note the path to the extracted folder

## Usage

### Scraping images

```bash
# Reddit saved posts
python main.py scrape reddit

# Twitter bookmarks (from archive - recommended)
python main.py scrape twitter --archive-path /path/to/twitter-archive

# Twitter bookmarks (browser automation - slower, may break)
python main.py scrape twitter --method browser

# DeviantArt favorites (requires manual login)
python main.py scrape deviantart --username your_da_username

# Run all scrapers
python main.py scrape all
```

### Running the tournament

```bash
# First, preprocess images (screens for NSFW, crops caption-style images)
python main.py preprocess

# Then run the tournament on processed images
python main.py tournament

# Start fresh (clear previous progress)
python main.py tournament --reset

# Adjust batch size (matches per save)
python main.py tournament --batch-size 20

# Run tournament on original images (skip preprocessing)
python main.py tournament --images-dir ./images
```

### Preprocessing details

The preprocess step uses Claude to assess each image:
- **Caption detection**: Images with "caption" layouts (art + text side by side) get auto-cropped to just the art portion
- **NSFW filtering**: Explicit content is flagged and excluded from the tournament
- **Quality check**: Very low quality or text-only images are filtered out

Results are saved incrementally, so you can stop and resume. Processed images go to `./images_processed/`.

### Check status

```bash
python main.py status
```

## Tournament mechanics

- **Single elimination**: Each image competes until it loses
- **Random seeding**: Images are randomly paired each round
- **Byes**: If contestant count isn't a power of 2, some get automatic advances
- **Persistent state**: Progress saves after every match, safe to interrupt
- **Credit tracking**: Winner info includes source URL and artist for proper attribution

With 2000 images, you're looking at ~11 rounds and ~2000 API calls total.

## Output

The winner ends up in `./winner/` with:
- The winning image
- `credit.json` with attribution info (source URL, artist, etc.)

## Estimated costs

Using Claude Sonnet for preprocessing and judging:
- Preprocessing: ~1 API call per image (screens & determines crops)
- Tournament: ~N matches where N = number of approved images
- ~2000 images = ~2000 preprocess calls + ~2000 tournament matches
- Each call sends 1-2 images (~500KB average each)
- Rough estimate: ~$15-30 in API costs for a full run

## Project structure

```
aquaedc-pfp-tournament/
├── main.py                 # CLI entry point
├── requirements.txt
├── .env.example
├── scrapers/
│   ├── twitter_scraper.py  # Twitter/X bookmarks
│   ├── reddit_scraper.py   # Reddit saved posts
│   └── deviantart_scraper.py
├── tournament/
│   ├── preprocess.py       # Image screening & cropping
│   └── bracket.py          # Tournament logic
├── images/                  # Downloaded images go here
│   ├── twitter/
│   ├── reddit/
│   └── deviantart/
├── images_processed/        # Screened & cropped images
├── data/                    # Metadata and state
│   ├── twitter_metadata.json
│   ├── reddit_metadata.json
│   ├── deviantart_metadata.json
│   ├── preprocess_state.json
│   └── tournament_state.json
└── winner/                  # Final output
    ├── winner_*.jpg
    └── credit.json
```

## Notes on artist credit

The system preserves source URLs and artist names when available. The expectation is that when using the winning image as a profile picture, you'll credit the artist in your bio. Example:

> pfp by @artistname on twitter

or

> pfp art: artistname on deviantart

This is standard etiquette and most artists are fine with it.

## Troubleshooting

**Twitter scraper can't find bookmarks.js**
- Make sure you've fully extracted the archive
- The file should be at `archive-folder/data/bookmarks.js`

**DeviantArt scraper is slow**
- It visits each deviation page individually to get full-size images
- This is intentional to avoid rate limiting
- Browser method is unavoidable since their API is limited

**Tournament keeps timing out**
- Check your Anthropic API key
- Reduce batch size with `--batch-size 5`
- Progress saves after each match, so you can resume

**"No images found"**
- Check that images actually downloaded to `./images/`
- Verify the scrapers completed successfully
