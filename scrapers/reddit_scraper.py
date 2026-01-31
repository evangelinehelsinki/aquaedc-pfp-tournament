"""
Reddit saved posts image scraper.
Uses PRAW to fetch saved posts and download images.
"""

import os
import json
import asyncio
import aiohttp
import aiofiles
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import praw
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

# Image domains we care about
IMAGE_DOMAINS = {
    'i.redd.it',
    'i.imgur.com',
    'imgur.com',
    'preview.redd.it',
}

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def get_reddit_client() -> praw.Reddit:
    """Initialize Reddit client."""
    return praw.Reddit(
        client_id=os.getenv('REDDIT_CLIENT_ID'),
        client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
        username=os.getenv('REDDIT_USERNAME'),
        password=os.getenv('REDDIT_PASSWORD'),
        user_agent='aquaedc-pfp-scraper/1.0'
    )


def extract_image_url(submission) -> str | None:
    """Extract direct image URL from a Reddit submission."""
    url = submission.url
    parsed = urlparse(url)
    
    # Direct image link
    if any(url.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
        # Convert preview.redd.it to i.redd.it for full resolution
        if 'preview.redd.it' in url:
            url = url.replace('preview.redd.it', 'i.redd.it')
        return url
    
    # Imgur page (not direct link)
    if 'imgur.com' in parsed.netloc and '/a/' not in url and '/gallery/' not in url:
        # Single image imgur page
        imgur_id = parsed.path.strip('/')
        if imgur_id:
            return f'https://i.imgur.com/{imgur_id}.jpg'
    
    # Reddit gallery
    if hasattr(submission, 'is_gallery') and submission.is_gallery:
        try:
            # Get first image from gallery
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
    print("🔄 Initializing Reddit client...")
    reddit = get_reddit_client()
    
    print(f"👤 Logged in as: {reddit.user.me().name}")
    
    print("\n📥 Collecting saved images...")
    images = collect_saved_images(reddit)
    print(f"Found {len(images)} images in saved posts")
    
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
