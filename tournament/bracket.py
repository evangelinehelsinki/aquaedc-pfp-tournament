"""
Tournament bracket system for image selection.

Uses Claude to judge image pairs and pick favorites through
single-elimination bracket rounds.
"""

import os
import json
import random
import base64
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich.layout import Layout
from rich.live import Live
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

console = Console()

# Supported image formats
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB limit for Claude API


@dataclass
class Contestant:
    """An image competing in the tournament."""
    id: str
    path: str
    source: str = 'unknown'
    source_url: str = ''
    author: str = ''
    eliminated: bool = False
    wins: int = 0
    losses: int = 0
    
    def __hash__(self):
        return hash(self.id)


@dataclass
class Match:
    """A single match between two contestants."""
    id: str
    round_num: int
    contestant_a: str  # Contestant ID
    contestant_b: str  # Contestant ID
    winner: Optional[str] = None
    reasoning: str = ''
    completed: bool = False


@dataclass
class TournamentState:
    """Complete tournament state for persistence."""
    contestants: dict[str, dict] = field(default_factory=dict)
    matches: list[dict] = field(default_factory=list)
    current_round: int = 1
    total_rounds: int = 0
    started_at: str = ''
    last_updated: str = ''
    winner_id: Optional[str] = None
    
    @classmethod
    def load(cls, path: Path) -> 'TournamentState':
        """Load state from JSON file."""
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                return cls(**data)
        return cls()
    
    def save(self, path: Path):
        """Save state to JSON file."""
        self.last_updated = datetime.utcnow().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(asdict(self), f, indent=2)


class Tournament:
    """Single-elimination tournament manager."""
    
    def __init__(self, images_dir: Path, state_path: Path):
        self.images_dir = images_dir
        self.state_path = state_path
        self.state = TournamentState.load(state_path)
        self.client = anthropic.Anthropic()
        
        # Load contestants from state or discover new ones
        self.contestants: dict[str, Contestant] = {}
        self.matches: list[Match] = []
        
        self._load_state()
    
    def _load_state(self):
        """Load or initialize tournament state."""
        if self.state.contestants:
            # Resume existing tournament
            for cid, data in self.state.contestants.items():
                self.contestants[cid] = Contestant(**data)
            
            for mdata in self.state.matches:
                self.matches.append(Match(**mdata))
            
            console.print(f"[green]Resumed tournament with {len(self.contestants)} contestants[/green]")
        else:
            # Discover images and start fresh
            self._discover_images()
            self._initialize_tournament()
    
    def _discover_images(self):
        """Find all images in the images directory."""
        console.print(f"[cyan]Scanning {self.images_dir} for images...[/cyan]")
        
        image_paths = []
        
        for ext in IMAGE_EXTENSIONS:
            image_paths.extend(self.images_dir.rglob(f'*{ext}'))
            image_paths.extend(self.images_dir.rglob(f'*{ext.upper()}'))
        
        # Load metadata if available
        metadata_by_filename = {}
        data_dir = self.images_dir.parent / 'data'
        
        for meta_file in data_dir.glob('*_metadata.json'):
            try:
                with open(meta_file) as f:
                    for item in json.load(f):
                        if 'filename' in item:
                            metadata_by_filename[item['filename']] = item
            except Exception as e:
                console.print(f"[yellow]Warning: Could not load {meta_file}: {e}[/yellow]")
        
        for img_path in image_paths:
            # Generate stable ID from file content hash
            with open(img_path, 'rb') as f:
                content_hash = hashlib.md5(f.read()).hexdigest()[:12]
            
            cid = f"img_{content_hash}"
            
            # Get metadata if available
            meta = metadata_by_filename.get(img_path.name, {})
            
            self.contestants[cid] = Contestant(
                id=cid,
                path=str(img_path),
                source=meta.get('source', img_path.parent.name),
                source_url=meta.get('source_url', ''),
                author=meta.get('author', ''),
            )
        
        console.print(f"[green]Found {len(self.contestants)} images[/green]")
    
    def _initialize_tournament(self):
        """Set up the initial tournament bracket."""
        if not self.contestants:
            raise ValueError("No contestants found!")
        
        # Shuffle contestants for random seeding
        contestant_ids = list(self.contestants.keys())
        random.shuffle(contestant_ids)
        
        # Calculate number of rounds needed
        n = len(contestant_ids)
        self.state.total_rounds = (n - 1).bit_length()  # ceil(log2(n))
        
        # If not a power of 2, some contestants get byes
        bracket_size = 2 ** self.state.total_rounds
        byes_needed = bracket_size - n
        
        console.print(f"[cyan]Tournament bracket:[/cyan]")
        console.print(f"  Contestants: {n}")
        console.print(f"  Rounds: {self.state.total_rounds}")
        console.print(f"  Byes (round 1): {byes_needed}")
        
        # Create first round matches
        self._create_round_matches(contestant_ids, byes_needed)
        
        self.state.started_at = datetime.utcnow().isoformat()
        self._save_state()
    
    def _create_round_matches(self, contestant_ids: list[str], byes: int = 0):
        """Create matches for the current round."""
        round_num = self.state.current_round
        
        # Apply byes - those contestants automatically advance
        bye_recipients = contestant_ids[:byes]
        competing = contestant_ids[byes:]
        
        for cid in bye_recipients:
            self.contestants[cid].wins += 1
            console.print(f"[dim]  Bye: {cid}[/dim]")
        
        # Pair up remaining contestants
        for i in range(0, len(competing), 2):
            if i + 1 < len(competing):
                match_id = f"r{round_num}_m{i//2}"
                self.matches.append(Match(
                    id=match_id,
                    round_num=round_num,
                    contestant_a=competing[i],
                    contestant_b=competing[i + 1],
                ))
    
    def _save_state(self):
        """Persist current state."""
        self.state.contestants = {cid: asdict(c) for cid, c in self.contestants.items()}
        self.state.matches = [asdict(m) for m in self.matches]
        self.state.save(self.state_path)
    
    def _load_image_base64(self, path: str) -> tuple[str, str]:
        """Load image as base64 for Claude API."""
        img_path = Path(path)
        
        # Determine media type
        ext = img_path.suffix.lower()
        media_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }
        media_type = media_types.get(ext, 'image/jpeg')
        
        # Check file size
        file_size = img_path.stat().st_size
        
        if file_size > MAX_IMAGE_SIZE:
            # Resize if too large
            with Image.open(img_path) as img:
                # Calculate new size to fit under limit
                ratio = (MAX_IMAGE_SIZE / file_size) ** 0.5
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                import io
                buffer = io.BytesIO()
                img.save(buffer, format='JPEG', quality=85)
                data = base64.standard_b64encode(buffer.getvalue()).decode('utf-8')
                return data, 'image/jpeg'
        
        with open(img_path, 'rb') as f:
            data = base64.standard_b64encode(f.read()).decode('utf-8')
        
        return data, media_type
    
    def _judge_match(self, match: Match) -> str:
        """Have Claude judge a match between two images."""
        contestant_a = self.contestants[match.contestant_a]
        contestant_b = self.contestants[match.contestant_b]
        
        # Load both images
        img_a_data, img_a_type = self._load_image_base64(contestant_a.path)
        img_b_data, img_b_type = self._load_image_base64(contestant_b.path)
        
        # Build the prompt
        system_prompt = """You are helping select a profile picture. You'll be shown two images and need to pick which one you prefer as a potential avatar/profile picture.

Consider:
- Visual appeal and aesthetics
- How well it would work as a small circular/square avatar
- Distinctiveness and memorability
- Personal resonance - what draws you to it

Be genuine about your preferences. This is about what YOU like, not what's objectively "better."

Respond with your choice (A or B) and a brief explanation of why you prefer it."""

        user_content = [
            {"type": "text", "text": "Here are the two images. Which do you prefer as a profile picture?\n\nImage A:"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img_a_type,
                    "data": img_a_data,
                }
            },
            {"type": "text", "text": "\n\nImage B:"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img_b_type,
                    "data": img_b_data,
                }
            },
            {"type": "text", "text": "\n\nWhich do you prefer? Reply with just 'A' or 'B' on the first line, then your reasoning."}
        ]
        
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )
        
        response_text = response.content[0].text.strip()
        
        # Parse response
        first_line = response_text.split('\n')[0].strip().upper()
        
        if 'A' in first_line and 'B' not in first_line:
            winner_id = match.contestant_a
        elif 'B' in first_line:
            winner_id = match.contestant_b
        else:
            # Ambiguous, look for keywords
            if response_text.lower().startswith('a') or 'image a' in response_text.lower()[:50]:
                winner_id = match.contestant_a
            else:
                winner_id = match.contestant_b
        
        match.winner = winner_id
        match.reasoning = response_text
        match.completed = True
        
        return winner_id
    
    def get_pending_matches(self) -> list[Match]:
        """Get all matches in the current round that haven't been completed."""
        return [m for m in self.matches 
                if m.round_num == self.state.current_round and not m.completed]
    
    def get_round_winners(self, round_num: int) -> list[str]:
        """Get all winners from a specific round."""
        winners = []
        
        # Include bye recipients (they have wins but weren't in matches)
        for cid, c in self.contestants.items():
            if c.wins > 0 and not c.eliminated:
                # Check if they won in this round
                was_in_match = any(
                    m.round_num == round_num and (m.contestant_a == cid or m.contestant_b == cid)
                    for m in self.matches
                )
                if was_in_match:
                    match = next(
                        m for m in self.matches 
                        if m.round_num == round_num and (m.contestant_a == cid or m.contestant_b == cid)
                    )
                    if match.winner == cid:
                        winners.append(cid)
                elif round_num == 1:  # Bye in first round
                    winners.append(cid)
        
        return winners
    
    def advance_round(self):
        """Move to the next round, creating new matches from winners."""
        # Get winners from current round
        current_round = self.state.current_round
        
        # Mark losers as eliminated
        for match in self.matches:
            if match.round_num == current_round and match.completed:
                loser_id = match.contestant_a if match.winner == match.contestant_b else match.contestant_b
                self.contestants[loser_id].eliminated = True
                self.contestants[loser_id].losses += 1
                self.contestants[match.winner].wins += 1
        
        # Collect all non-eliminated contestants
        remaining = [cid for cid, c in self.contestants.items() if not c.eliminated]
        
        if len(remaining) == 1:
            # We have a winner!
            self.state.winner_id = remaining[0]
            self._save_state()
            return
        
        # Advance to next round
        self.state.current_round += 1
        
        # Create new matches
        random.shuffle(remaining)
        for i in range(0, len(remaining), 2):
            if i + 1 < len(remaining):
                match_id = f"r{self.state.current_round}_m{i//2}"
                self.matches.append(Match(
                    id=match_id,
                    round_num=self.state.current_round,
                    contestant_a=remaining[i],
                    contestant_b=remaining[i + 1],
                ))
            else:
                # Odd number, bye
                self.contestants[remaining[i]].wins += 1
                console.print(f"[dim]  Bye: {remaining[i]}[/dim]")
        
        self._save_state()
    
    def run(self, batch_size: int = 10):
        """Run the tournament to completion."""
        console.print(Panel.fit(
            "[bold cyan]🏆 Profile Picture Tournament[/bold cyan]\n"
            f"Contestants: {len(self.contestants)}\n"
            f"Current Round: {self.state.current_round} / {self.state.total_rounds}",
            title="Tournament Status"
        ))
        
        while self.state.winner_id is None:
            pending = self.get_pending_matches()
            
            if not pending:
                # Current round complete, advance
                self.advance_round()
                
                if self.state.winner_id:
                    break
                
                pending = self.get_pending_matches()
                console.print(f"\n[bold cyan]═══ Round {self.state.current_round} ═══[/bold cyan]")
                console.print(f"Matches this round: {len(pending)}")
            
            # Process matches in batches
            batch = pending[:batch_size]
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                console=console,
            ) as progress:
                task = progress.add_task(
                    f"[cyan]Round {self.state.current_round} matches...",
                    total=len(batch)
                )
                
                for match in batch:
                    contestant_a = self.contestants[match.contestant_a]
                    contestant_b = self.contestants[match.contestant_b]
                    
                    try:
                        winner_id = self._judge_match(match)
                        winner = self.contestants[winner_id]
                        
                        # Show result
                        progress.console.print(
                            f"  [dim]{Path(contestant_a.path).name}[/dim] vs "
                            f"[dim]{Path(contestant_b.path).name}[/dim] → "
                            f"[green]{Path(winner.path).name}[/green]"
                        )
                        
                    except Exception as e:
                        console.print(f"[red]Error in match {match.id}: {e}[/red]")
                        # Skip this match for now
                        continue
                    
                    progress.advance(task)
                    self._save_state()
            
            # Show round progress
            completed = len([m for m in self.matches if m.round_num == self.state.current_round and m.completed])
            total = len([m for m in self.matches if m.round_num == self.state.current_round])
            console.print(f"[dim]Round {self.state.current_round}: {completed}/{total} matches complete[/dim]")
        
        # Tournament complete!
        winner = self.contestants[self.state.winner_id]
        
        console.print("\n")
        console.print(Panel.fit(
            f"[bold green]🎉 WINNER 🎉[/bold green]\n\n"
            f"[bold]{Path(winner.path).name}[/bold]\n"
            f"Source: {winner.source}\n"
            f"Author: {winner.author or 'Unknown'}\n"
            f"URL: {winner.source_url or 'N/A'}\n\n"
            f"Wins: {winner.wins}",
            title="Tournament Complete!",
            border_style="green"
        ))
        
        # Copy winner to output location
        output_dir = self.images_dir.parent / 'winner'
        output_dir.mkdir(exist_ok=True)
        
        import shutil
        winner_output = output_dir / f"winner_{Path(winner.path).name}"
        shutil.copy2(winner.path, winner_output)
        
        console.print(f"\n[green]Winner copied to: {winner_output}[/green]")
        
        # Save credit info
        credit_info = {
            'filename': winner_output.name,
            'original_path': winner.path,
            'source': winner.source,
            'source_url': winner.source_url,
            'author': winner.author,
            'wins': winner.wins,
            'tournament_completed': datetime.utcnow().isoformat(),
        }
        
        with open(output_dir / 'credit.json', 'w') as f:
            json.dump(credit_info, f, indent=2)
        
        console.print(f"[green]Credit info saved to: {output_dir / 'credit.json'}[/green]")
        
        return winner


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Run image selection tournament')
    parser.add_argument('--images-dir', type=Path, default=Path(__file__).parent.parent / 'images',
                       help='Directory containing images to choose from')
    parser.add_argument('--batch-size', type=int, default=10,
                       help='Number of matches to run per batch (default: 10)')
    parser.add_argument('--reset', action='store_true',
                       help='Reset tournament state and start fresh')
    
    args = parser.parse_args()
    
    state_path = Path(__file__).parent.parent / 'data' / 'tournament_state.json'
    
    if args.reset and state_path.exists():
        state_path.unlink()
        console.print("[yellow]Tournament state reset[/yellow]")
    
    tournament = Tournament(args.images_dir, state_path)
    tournament.run(batch_size=args.batch_size)


if __name__ == '__main__':
    main()
