#!/usr/bin/env python3
"""
aquaedc-pfp-tournament

A system for scraping images from Twitter, Reddit, and DeviantArt,
then running a tournament bracket to let Claude pick its own profile picture.
"""

import sys
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("""
╔═══════════════════════════════════════════════════════════════╗
║           aquaedc-pfp-tournament                              ║
║           Let an AI pick its own profile picture              ║
╚═══════════════════════════════════════════════════════════════╝

Commands:
  scrape twitter     Scrape Twitter/X bookmarks for images
  scrape reddit      Scrape Reddit saved posts for images
  scrape deviantart  Scrape DeviantArt favorites for images
  scrape all         Run all scrapers
  
  tournament         Run the selection tournament
  tournament --reset Start tournament fresh (clears progress)
  
  status             Show current tournament status

Setup:
  1. Copy .env.example to .env and fill in credentials
  2. Run scrapers to collect images
  3. Run tournament to select a winner

Example:
  python main.py scrape reddit
  python main.py scrape twitter --archive-path ~/Downloads/twitter-archive
  python main.py tournament
        """)
        return
    
    command = sys.argv[1]
    subcommand = sys.argv[2] if len(sys.argv) > 2 else None
    extra_args = sys.argv[3:]
    
    if command == 'scrape':
        if subcommand == 'twitter':
            from scrapers.twitter_scraper import main as twitter_main
            sys.argv = ['twitter_scraper'] + extra_args
            twitter_main()
        
        elif subcommand == 'reddit':
            from scrapers.reddit_scraper import main as reddit_main
            reddit_main()
        
        elif subcommand == 'deviantart':
            from scrapers.deviantart_scraper import main as da_main
            sys.argv = ['deviantart_scraper'] + extra_args
            da_main()
        
        elif subcommand == 'all':
            print("Running all scrapers...\n")
            
            print("=" * 50)
            print("REDDIT")
            print("=" * 50)
            try:
                from scrapers.reddit_scraper import main as reddit_main
                reddit_main()
            except Exception as e:
                print(f"Reddit scraper failed: {e}")
            
            print("\n" + "=" * 50)
            print("TWITTER")
            print("=" * 50)
            try:
                from scrapers.twitter_scraper import main as twitter_main
                sys.argv = ['twitter_scraper'] + extra_args
                twitter_main()
            except Exception as e:
                print(f"Twitter scraper failed: {e}")
            
            print("\n" + "=" * 50)
            print("DEVIANTART")
            print("=" * 50)
            try:
                from scrapers.deviantart_scraper import main as da_main
                sys.argv = ['deviantart_scraper'] + extra_args
                da_main()
            except Exception as e:
                print(f"DeviantArt scraper failed: {e}")
        
        else:
            print(f"Unknown scraper: {subcommand}")
            print("Available: twitter, reddit, deviantart, all")
    
    elif command == 'tournament':
        from tournament.bracket import main as tournament_main
        sys.argv = ['bracket'] + ([subcommand] if subcommand else []) + extra_args
        tournament_main()
    
    elif command == 'status':
        import json
        state_path = Path(__file__).parent / 'data' / 'tournament_state.json'
        images_dir = Path(__file__).parent / 'images'
        
        print("\n📊 Tournament Status\n")
        
        # Count images
        image_count = {
            'twitter': 0,
            'reddit': 0,
            'deviantart': 0,
        }
        
        for source in image_count:
            source_dir = images_dir / source
            if source_dir.exists():
                image_count[source] = len(list(source_dir.glob('*.*')))
        
        print("Images collected:")
        for source, count in image_count.items():
            print(f"  {source}: {count}")
        print(f"  Total: {sum(image_count.values())}")
        
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            
            print(f"\nTournament progress:")
            print(f"  Round: {state.get('current_round', 0)} / {state.get('total_rounds', '?')}")
            print(f"  Contestants: {len(state.get('contestants', {}))}")
            print(f"  Matches completed: {len([m for m in state.get('matches', []) if m.get('completed')])}")
            
            if state.get('winner_id'):
                winner = state['contestants'].get(state['winner_id'], {})
                print(f"\n🏆 Winner: {winner.get('path', 'Unknown')}")
        else:
            print("\nNo tournament in progress.")
    
    else:
        print(f"Unknown command: {command}")
        print("Run without arguments to see available commands.")


if __name__ == '__main__':
    main()
