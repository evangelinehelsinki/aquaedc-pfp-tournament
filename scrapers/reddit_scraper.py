"""
Reddit saved posts image scraper.

Two methods available:
1. API method - Uses PRAW to fetch saved posts (requires API credentials)
2. Archive method - Parse Reddit data export (Settings > Data Request)

The archive method is recommended as it doesn't require API setup.
"""

import os
import json
import csv
import asyncio
import aiohttp
import aiofiles
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from tqdm import tqdm
from dotenv import load_dotenv

# Optional: praw for API method
try:
    import praw
    PRAW_AVAILABLE = True
except ImportError:
    PRAW_AVAILABLE = False

load_dotenv()

# Image domains we care about
IMAGE_DOMAINS = {
    'i.redd.it',
    'i.imgur.com',
    'imgur.com',
    'preview.redd.it',
}

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def get_reddit_client() -> 'praw.Reddit':
    """Initialize Reddit client."""
    if not PRAW_AVAILABLE:
        raise ImportError("PRAW not installed. Run: pip install praw")
    return praw.Reddit(
        client_id=os.getenv('REDDIT_CLIENT_ID'),
        client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
        username=os.getenv('REDDIT_USERNAME'),
        password=os.getenv('REDDIT_PASSWORD'),
        user_agent='aquaedc-pfp-scraper/1.0'
    )


def extract_image_url_from_url(url: str) -> str | None:
    """Extract direct image URL from a Reddit post URL."""
    parsed = urlparse(url)
    
    # Direct image link
    if any(url.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
        if 'preview.redd.it' in url:
            url = url.replace('preview.redd.it', 'i.redd.it')
        return url
    
    # i.redd.it links
    if 'i.redd.it' in parsed.netloc:
        return url
    
    # Imgur page (not direct link)
    if 'imgur.com' in parsed.netloc and '/a/' not in url and '/gallery/' not in url:
        imgur_id = parsed.path.strip('/')
        if imgur_id and '.' not in imgur_id:
            return f'https://i.imgur.com/{imgur_id}.jpg'
    
    return None


def parse_reddit_archive(archive_path: Path) -> list[dict]:
    """
    Parse Reddit data export for saved posts with images.
    
    Reddit exports saved posts in a CSV file, typically at:
    - saved_posts.csv
    - or inside a subdirectory
    """
    archive_path = Path(archive_path)
    
    # Find the saved posts file
    possible_files = [
        archive_path / 'saved_posts.csv',
        archive_path / 'saved_comments.csv',  # Sometimes images are in comments too
    ]
    
    # Also search recursively
    for csv_file in archive_path.rglob('*.csv'):
        if 'saved' in csv_file.name.lower():
            possible_files.insert(0, csv_file)
    
    saved_posts_file = None
    for f in possible_files:
        if f.exists():
            saved_posts_file = f
            break
    
    if not saved_posts_file:
        # List what's in the archive to help debug
        contents = list(archive_path.rglob('*'))[:20]
        raise FileNotFoundError(
            f"Could not find saved_posts.csv in {archive_path}\n"
            f"Found files: {[str(c.relative_to(archive_path)) for c in contents]}\n"
            "Make sure you've extracted the Reddit data export."
        )
    
    print(f"📂 Parsing {saved_posts_file}")
    
    images = []
    
    with open(saved_posts_file, 'r', encoding='utf-8') as f:
        # Try to detect the CSV format
        sample = f.read(2048)
        f.seek(0)
        
        # Reddit exports can have different delimiters
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t')
        reader = csv.DictReader(f, dialect=dialect)
        
        for row in reader:
            # Reddit CSV typically has 'url' or 'permalink' columns
            url = row.get('url', row.get('URL', row.get('permalink', '')))
            post_id = row.get('id', row.get('ID', ''))
            subreddit = row.get('subreddit', row.get('Subreddit', ''))
            title = row.get('title', row.get('Title', ''))
            
            if not url:
                continue
            
            # Check if it's an image URL
            image_url = extract_image_url_from_url(url)
            
            if image_url:
                images.append({
                    'url': image_url,
                    'source': 'reddit',
                    'source_url': f'https://reddit.com/r/{subreddit}/comments/{post_id}' if post_id else url,
                    'title': title,
                    'subreddit': subreddit,
                    'scraped_at': datetime.utcnow().isoformat(),
                })
    
    return images


def extract_image_url(submission) -> str | None:
    """Extract direct image URL from a Reddit submission (API method)."""
    url = submission.url
    
    image_url = extract_image_url_from_url(url)
    if image_url:
        return image_url
    
    # Reddit gallery
    if hasattr(submission, 'is_gallery') and submission.is_gallery:
        try:
            media_metadata = submission.media_metadata
            if media_metadata:
                first_item = list(media_metadata.values())[0]
                if 's' in first_item and 'u' in first_item['s']:
                    return first_item['s']['u'].replace('&amp;', '&')
        except Exception:
            pass
    
    return None


def collect_saved_images(reddit: praw.Reddit, limit: int | None = None) -> list[dict]:
    """Collect image URLs from saved posts."""
    user = reddit.user.me()
    saved = user.saved(limit=limit)
    
    images = []
    
    for item in tqdm(saved, desc="Scanning saved posts"):
        # Skip comments
        if isinstance(item, praw.models.Comment):
            continue
            
        submission = item
        image_url = extract_image_url(submission)
        
        if image_url:
            images.append({
                'url': image_url,
                'source': 'reddit',
                'source_url': f'https://reddit.com{submission.permalink}',
                'title': submission.title,
                'subreddit': str(submission.subreddit),
                'author': str(submission.author) if submission.author else '[deleted]',
                'saved_at': datetime.utcnow().isoformat(),
            })
    
    return images


async def download_image(session: aiohttp.ClientSession, image: dict, output_dir: Path) -> dict | None:
    """Download a single image."""
    url = image['url']
    
    # Generate filename from URL
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or '.' not in filename:
        filename = f"{hash(url) & 0xffffffff}.jpg"
    
    # Make filename unique
    output_path = output_dir / filename
    counter = 1
    while output_path.exists():
        stem = output_path.stem.rsplit('_', 1)[0]
        output_path = output_dir / f"{stem}_{counter}{output_path.suffix}"
        counter += 1
    
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status == 200:
                content = await response.read()
                
                # Validate it's actually an image
                if len(content) < 1000:  # Too small
                    return None
                    
                async with aiofiles.open(output_path, 'wb') as f:
                    await f.write(content)
                
                image['local_path'] = str(output_path)
                image['filename'] = output_path.name
                return image
    except Exception as e:
        print(f"Failed to download {url}: {e}")
    
    return None


async def download_all_images(images: list[dict], output_dir: Path, concurrency: int = 10) -> list[dict]:
    """Download all images concurrently."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [download_image(session, img, output_dir) for img in images]
        
        results = []
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Downloading images"):
            result = await coro
            if result:
                results.append(result)
    
    return results


def save_metadata(images: list[dict], output_path: Path):
    """Save image metadata to JSON."""
    with open(output_path, 'w') as f:
        json.dump(images, f, indent=2)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Scrape Reddit saved posts for images')
    parser.add_argument('--method', choices=['archive', 'api'], default='archive',
                       help='Scraping method (default: archive)')
    parser.add_argument('--archive-path', type=Path,
                       help='Path to extracted Reddit data export folder')
    
    args = parser.parse_args()
    
    if args.method == 'archive':
        if not args.archive_path:
            # Look for common locations
            common_paths = [
                Path.home() / 'reddit',
                Path.home() / 'Downloads' / 'reddit',
                Path.home() / 'Downloads' / 'reddit_data',
                Path('./reddit-archive'),
                Path('./reddit'),
            ]
            
            for p in common_paths:
                if p.exists():
                    args.archive_path = p
                    break
            
            if not args.archive_path:
                print("❌ Please provide --archive-path to your extracted Reddit data export")
                print("\nTo get your data export:")
                print("1. Go to https://www.reddit.com/settings/data-request")
                print("2. Request your data")
                print("3. Wait for email and download the zip")
                print("4. Extract the zip")
                print("5. Run: python reddit_scraper.py --archive-path /path/to/extracted")
                return
        
        print(f"📂 Parsing archive from {args.archive_path}")
        images = parse_reddit_archive(args.archive_path)
    
    else:
        # API method
        if not PRAW_AVAILABLE:
            print("❌ PRAW not installed. Run: pip install praw")
            print("   Or use --method archive with a data export instead.")
            return
        
        print("🔄 Initializing Reddit client...")
        reddit = get_reddit_client()
        
        print(f"👤 Logged in as: {reddit.user.me().name}")
        
        print("\n📥 Collecting saved images...")
        images = collect_saved_images(reddit)
    
    print(f"\n📊 Found {len(images)} images")
    
    if not images:
        print("No images found!")
        return
    
    output_dir = Path(__file__).parent.parent / 'images' / 'reddit'
    metadata_path = Path(__file__).parent.parent / 'data' / 'reddit_metadata.json'
    
    print(f"\n⬇️  Downloading to {output_dir}...")
    downloaded = asyncio.run(download_all_images(images, output_dir))
    
    print(f"\n💾 Saving metadata...")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    save_metadata(downloaded, metadata_path)
    
    print(f"\n✅ Done! Downloaded {len(downloaded)} images")
    print(f"   Images: {output_dir}")
    print(f"   Metadata: {metadata_path}")


if __name__ == '__main__':
    main()
