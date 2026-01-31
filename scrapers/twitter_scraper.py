"""
Twitter/X bookmarks image scraper.

Two methods available:
1. Archive parsing - Use your Twitter data export (Settings > Your Account > Download archive)
2. Browser automation - Use Playwright to scrape bookmarks directly (slower, may break)

The archive method is recommended as it's more reliable.
"""

import os
import json
import asyncio
import aiohttp
import aiofiles
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from tqdm import tqdm

# Optional: playwright for browser method
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def parse_twitter_archive(archive_path: Path) -> list[dict]:
    """
    Parse Twitter data archive for bookmarked images.
    
    Expected structure:
    archive/
    └── data/
        └── bookmarks.js
    """
    bookmarks_file = archive_path / 'data' / 'bookmarks.js'
    
    if not bookmarks_file.exists():
        # Try alternate location
        bookmarks_file = archive_path / 'data' / 'bookmark.js'
    
    if not bookmarks_file.exists():
        raise FileNotFoundError(
            f"Could not find bookmarks.js in {archive_path}\n"
            "Make sure you've extracted your Twitter archive and provided the correct path."
        )
    
    # Twitter archive JS files start with "window.YTD.bookmarks.part0 = "
    content = bookmarks_file.read_text(encoding='utf-8')
    
    # Extract JSON part
    json_match = re.search(r'=\s*(\[.*\])', content, re.DOTALL)
    if not json_match:
        raise ValueError("Could not parse bookmarks.js - unexpected format")
    
    bookmarks_data = json.loads(json_match.group(1))
    
    images = []
    
    for bookmark in bookmarks_data:
        tweet = bookmark.get('bookmark', {}).get('tweet', {})
        if not tweet:
            continue
        
        tweet_id = tweet.get('tweetId', tweet.get('id_str', ''))
        user = tweet.get('core', {}).get('user_results', {}).get('result', {}).get('legacy', {})
        username = user.get('screen_name', 'unknown')
        
        # Check for media
        extended_entities = tweet.get('extended_entities', {})
        media_list = extended_entities.get('media', [])
        
        # Also check legacy location
        if not media_list:
            media_list = tweet.get('entities', {}).get('media', [])
        
        for media in media_list:
            media_type = media.get('type', '')
            
            if media_type == 'photo':
                # Get highest quality version
                media_url = media.get('media_url_https', media.get('media_url', ''))
                if media_url:
                    # Request large size
                    if '?' not in media_url:
                        media_url = f"{media_url}?format=jpg&name=large"
                    
                    images.append({
                        'url': media_url,
                        'source': 'twitter',
                        'source_url': f'https://twitter.com/{username}/status/{tweet_id}',
                        'author': username,
                        'tweet_id': tweet_id,
                        'scraped_at': datetime.utcnow().isoformat(),
                    })
            
            elif media_type == 'animated_gif':
                # GIFs are stored as videos, get thumbnail
                thumb_url = media.get('media_url_https', '')
                if thumb_url:
                    images.append({
                        'url': thumb_url,
                        'source': 'twitter',
                        'source_url': f'https://twitter.com/{username}/status/{tweet_id}',
                        'author': username,
                        'tweet_id': tweet_id,
                        'is_gif_thumb': True,
                        'scraped_at': datetime.utcnow().isoformat(),
                    })
    
    return images


async def scrape_bookmarks_browser(max_scrolls: int = 100) -> list[dict]:
    """
    Scrape bookmarks using browser automation.
    
    This is slower and more fragile than the archive method.
    You'll need to log in manually when the browser opens.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise ImportError("Playwright not installed. Run: pip install playwright && playwright install")
    
    images = []
    seen_urls = set()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # Need to log in
        context = await browser.new_context()
        page = await context.new_page()
        
        print("🌐 Opening Twitter - please log in if needed")
        await page.goto('https://twitter.com/i/bookmarks')
        
        # Wait for user to log in and bookmarks to load
        print("⏳ Waiting for bookmarks to load (press Enter when ready)...")
        input()
        
        print("📜 Scrolling through bookmarks...")
        
        for scroll_num in tqdm(range(max_scrolls), desc="Scrolling"):
            # Extract image URLs from current viewport
            img_elements = await page.query_selector_all('img[src*="pbs.twimg.com/media"]')
            
            for img in img_elements:
                src = await img.get_attribute('src')
                if src and src not in seen_urls:
                    seen_urls.add(src)
                    
                    # Try to get original size
                    clean_url = re.sub(r'\?.*$', '', src)
                    clean_url = f"{clean_url}?format=jpg&name=large"
                    
                    # Try to find parent tweet link
                    parent = img
                    source_url = 'https://twitter.com/i/bookmarks'
                    for _ in range(10):
                        parent = await parent.evaluate_handle('el => el.parentElement')
                        if parent:
                            href = await parent.evaluate('el => el.querySelector("a[href*=\'/status/\']")?.href')
                            if href:
                                source_url = href
                                break
                    
                    images.append({
                        'url': clean_url,
                        'source': 'twitter',
                        'source_url': source_url,
                        'scraped_at': datetime.utcnow().isoformat(),
                    })
            
            # Scroll down
            await page.evaluate('window.scrollBy(0, window.innerHeight)')
            await asyncio.sleep(1)  # Rate limiting
            
            # Check if we've hit the bottom
            at_bottom = await page.evaluate('''
                () => (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 100
            ''')
            
            if at_bottom:
                print(f"\n📍 Reached end of bookmarks after {scroll_num + 1} scrolls")
                break
        
        await browser.close()
    
    return images


async def download_image(session: aiohttp.ClientSession, image: dict, output_dir: Path) -> dict | None:
    """Download a single image."""
    url = image['url']
    
    # Generate filename
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or '.' not in filename:
        filename = f"twitter_{hash(url) & 0xffffffff}.jpg"
    
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
                
                if len(content) < 1000:
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
    
    parser = argparse.ArgumentParser(description='Scrape Twitter/X bookmarks for images')
    parser.add_argument('--method', choices=['archive', 'browser'], default='archive',
                       help='Scraping method (default: archive)')
    parser.add_argument('--archive-path', type=Path,
                       help='Path to extracted Twitter archive folder')
    parser.add_argument('--max-scrolls', type=int, default=100,
                       help='Max scrolls for browser method (default: 100)')
    
    args = parser.parse_args()
    
    if args.method == 'archive':
        if not args.archive_path:
            # Look for common locations
            common_paths = [
                Path.home() / 'Downloads' / 'twitter-archive',
                Path.home() / 'Downloads' / 'twitter',
                Path('./twitter-archive'),
            ]
            
            for p in common_paths:
                if p.exists():
                    args.archive_path = p
                    break
            
            if not args.archive_path:
                print("❌ Please provide --archive-path to your extracted Twitter archive")
                print("\nTo get your archive:")
                print("1. Go to twitter.com/settings/download_your_data")
                print("2. Request your archive")
                print("3. Extract the downloaded zip")
                print("4. Run: python twitter_scraper.py --archive-path /path/to/archive")
                return
        
        print(f"📂 Parsing archive from {args.archive_path}")
        images = parse_twitter_archive(args.archive_path)
    
    else:
        print("🌐 Using browser automation...")
        images = asyncio.run(scrape_bookmarks_browser(args.max_scrolls))
    
    print(f"\n📊 Found {len(images)} images")
    
    if not images:
        print("No images found!")
        return
    
    output_dir = Path(__file__).parent.parent / 'images' / 'twitter'
    metadata_path = Path(__file__).parent.parent / 'data' / 'twitter_metadata.json'
    
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
