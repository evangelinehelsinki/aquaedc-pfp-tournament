"""
DeviantArt favorites image scraper.

Uses Playwright browser automation to scrape your favorites.
You'll need to log in manually when the browser opens.
"""

import os
import json
import asyncio
import aiohttp
import aiofiles
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urljoin

from tqdm import tqdm

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    exit(1)


async def scrape_favorites(username: str | None = None, max_pages: int = 50) -> list[dict]:
    """
    Scrape DeviantArt favorites using browser automation.
    
    Args:
        username: Your DeviantArt username (will prompt if not provided)
        max_pages: Maximum number of pages to scrape
    """
    images = []
    seen_urls = set()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Go to DeviantArt and let user log in
        print("🌐 Opening DeviantArt - please log in if needed")
        await page.goto('https://www.deviantart.com/')
        
        print("⏳ Press Enter once you're logged in...")
        input()
        
        # Get username if not provided
        if not username:
            # Try to get from page
            try:
                username = await page.evaluate('''
                    () => window.__INITIAL_STATE__?.session?.user?.username || null
                ''')
            except:
                pass
            
            if not username:
                username = input("Enter your DeviantArt username: ").strip()
        
        print(f"👤 Scraping favorites for: {username}")
        
        # Navigate to favorites
        favorites_url = f'https://www.deviantart.com/{username}/favourites/all'
        await page.goto(favorites_url)
        await asyncio.sleep(2)
        
        print("📜 Scrolling through favorites...")
        
        page_num = 0
        last_count = 0
        stall_count = 0
        
        with tqdm(total=max_pages, desc="Loading pages") as pbar:
            while page_num < max_pages:
                # Extract deviation links from current view
                deviations = await page.evaluate('''
                    () => {
                        const links = document.querySelectorAll('a[href*="/art/"]');
                        const results = [];
                        const seen = new Set();
                        
                        for (const link of links) {
                            const href = link.href;
                            if (href && href.includes('/art/') && !seen.has(href)) {
                                seen.add(href);
                                
                                // Try to find the image
                                const img = link.querySelector('img');
                                let imgUrl = null;
                                
                                if (img) {
                                    // DeviantArt uses various image URL patterns
                                    imgUrl = img.src || img.getAttribute('data-src');
                                }
                                
                                results.push({
                                    page_url: href,
                                    thumbnail_url: imgUrl
                                });
                            }
                        }
                        return results;
                    }
                ''')
                
                for dev in deviations:
                    if dev['page_url'] not in seen_urls:
                        seen_urls.add(dev['page_url'])
                        images.append({
                            'source': 'deviantart',
                            'source_url': dev['page_url'],
                            'thumbnail_url': dev.get('thumbnail_url'),
                            'scraped_at': datetime.utcnow().isoformat(),
                        })
                
                # Check if we're still finding new items
                if len(images) == last_count:
                    stall_count += 1
                    if stall_count >= 5:
                        print(f"\n📍 No new items found, stopping")
                        break
                else:
                    stall_count = 0
                    last_count = len(images)
                
                # Scroll down
                await page.evaluate('window.scrollBy(0, window.innerHeight * 2)')
                await asyncio.sleep(1.5)
                
                page_num += 1
                pbar.update(1)
                pbar.set_postfix({'found': len(images)})
        
        print(f"\n🔍 Found {len(images)} favorites, now fetching full-size images...")
        
        # Now visit each deviation page to get the full-size image
        for i, img in enumerate(tqdm(images, desc="Fetching full-size URLs")):
            try:
                await page.goto(img['source_url'], wait_until='domcontentloaded')
                await asyncio.sleep(0.5)
                
                # Try to find the full-size image URL
                full_url = await page.evaluate('''
                    () => {
                        // Method 1: Look for download button/link
                        const downloadLink = document.querySelector('a[href*="download"]');
                        if (downloadLink) return downloadLink.href;
                        
                        // Method 2: Look for the main image
                        const mainImg = document.querySelector('img[src*="images-wixmp"]');
                        if (mainImg) return mainImg.src;
                        
                        // Method 3: Check meta tags
                        const ogImage = document.querySelector('meta[property="og:image"]');
                        if (ogImage) return ogImage.content;
                        
                        return null;
                    }
                ''')
                
                if full_url:
                    # Clean up URL to get highest quality
                    # DeviantArt URLs often have size parameters we can modify
                    full_url = re.sub(r'/v1/fill/.*?/', '/', full_url)
                    img['url'] = full_url
                
                # Get artist name
                artist = await page.evaluate('''
                    () => {
                        const artistLink = document.querySelector('a[href*="deviantart.com/"][class*="user"]');
                        if (artistLink) {
                            const match = artistLink.href.match(/deviantart\\.com\\/([^/]+)/);
                            return match ? match[1] : null;
                        }
                        return null;
                    }
                ''')
                
                if artist:
                    img['author'] = artist
                
            except Exception as e:
                print(f"\n⚠️  Error fetching {img['source_url']}: {e}")
                continue
        
        await browser.close()
    
    # Filter to only images where we got a URL
    images = [img for img in images if img.get('url')]
    
    return images


async def download_image(session: aiohttp.ClientSession, image: dict, output_dir: Path) -> dict | None:
    """Download a single image."""
    url = image.get('url')
    if not url:
        return None
    
    # Generate filename from URL or source
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or '.' not in filename:
        # Use deviation ID from source URL
        source_match = re.search(r'/art/.*?-(\d+)$', image.get('source_url', ''))
        if source_match:
            filename = f"da_{source_match.group(1)}.jpg"
        else:
            filename = f"da_{hash(url) & 0xffffffff}.jpg"
    
    output_path = output_dir / filename
    counter = 1
    while output_path.exists():
        stem = output_path.stem.rsplit('_', 1)[0]
        output_path = output_dir / f"{stem}_{counter}{output_path.suffix}"
        counter += 1
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.deviantart.com/'
        }
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60), headers=headers) as response:
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


async def download_all_images(images: list[dict], output_dir: Path, concurrency: int = 5) -> list[dict]:
    """Download all images concurrently."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Lower concurrency for DeviantArt to avoid rate limiting
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
    
    parser = argparse.ArgumentParser(description='Scrape DeviantArt favorites for images')
    parser.add_argument('--username', help='Your DeviantArt username')
    parser.add_argument('--max-pages', type=int, default=50,
                       help='Maximum scroll iterations (default: 50)')
    
    args = parser.parse_args()
    
    print("🎨 DeviantArt Favorites Scraper")
    print("=" * 40)
    
    images = asyncio.run(scrape_favorites(args.username, args.max_pages))
    
    print(f"\n📊 Found {len(images)} images with full-size URLs")
    
    if not images:
        print("No images found!")
        return
    
    output_dir = Path(__file__).parent.parent / 'images' / 'deviantart'
    metadata_path = Path(__file__).parent.parent / 'data' / 'deviantart_metadata.json'
    
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
