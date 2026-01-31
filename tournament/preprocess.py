"""
Image pre-processor for tournament.

Screens images before the tournament to:
1. Detect and crop caption-style images (image + text layout)
2. Flag NSFW or otherwise unsuitable images for exclusion
3. Validate images are suitable for profile picture use

This runs once before the tournament and saves results to avoid re-processing.
"""

import os
import json
import base64
import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

import anthropic
from PIL import Image
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from dotenv import load_dotenv

load_dotenv()

console = Console()

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
MAX_IMAGE_SIZE = 20 * 1024 * 1024


@dataclass
class ImageAssessment:
    """Assessment result for a single image."""
    image_id: str
    path: str
    status: str  # 'ok', 'crop', 'exclude'
    reason: str = ''
    crop_box: Optional[tuple[int, int, int, int]] = None  # (left, top, right, bottom)
    assessed_at: str = ''


class ImagePreprocessor:
    """Pre-processes images before tournament."""
    
    def __init__(self, images_dir: Path, output_dir: Path, state_path: Path):
        self.images_dir = images_dir
        self.output_dir = output_dir
        self.state_path = state_path
        self.client = anthropic.Anthropic()
        
        # Load existing assessments
        self.assessments: dict[str, ImageAssessment] = {}
        self._load_state()
    
    def _load_state(self):
        """Load previous assessment results."""
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
                for item in data.get('assessments', []):
                    assessment = ImageAssessment(**item)
                    self.assessments[assessment.image_id] = assessment
            console.print(f"[dim]Loaded {len(self.assessments)} previous assessments[/dim]")
    
    def _save_state(self):
        """Save assessment results."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'assessments': [asdict(a) for a in self.assessments.values()],
            'last_updated': datetime.utcnow().isoformat(),
        }
        with open(self.state_path, 'w') as f:
            json.dump(data, f, indent=2)
    
    def _get_image_id(self, path: Path) -> str:
        """Generate stable ID from file content."""
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:12]
    
    def _load_image_base64(self, path: Path) -> tuple[str, str, tuple[int, int]]:
        """Load image as base64, return (data, media_type, (width, height))."""
        ext = path.suffix.lower()
        media_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }
        media_type = media_types.get(ext, 'image/jpeg')
        
        # Get dimensions
        with Image.open(path) as img:
            width, height = img.size
        
        # Check file size and resize if needed
        file_size = path.stat().st_size
        if file_size > MAX_IMAGE_SIZE:
            with Image.open(path) as img:
                ratio = (MAX_IMAGE_SIZE / file_size) ** 0.5
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                import io
                buffer = io.BytesIO()
                img.save(buffer, format='JPEG', quality=85)
                data = base64.standard_b64encode(buffer.getvalue()).decode('utf-8')
                return data, 'image/jpeg', (width, height)
        
        with open(path, 'rb') as f:
            data = base64.standard_b64encode(f.read()).decode('utf-8')
        
        return data, media_type, (width, height)
    
    def _assess_image(self, path: Path) -> ImageAssessment:
        """Have Claude assess a single image."""
        image_id = self._get_image_id(path)
        
        # Check if already assessed
        if image_id in self.assessments:
            return self.assessments[image_id]
        
        try:
            img_data, media_type, (width, height) = self._load_image_base64(path)
        except Exception as e:
            return ImageAssessment(
                image_id=image_id,
                path=str(path),
                status='exclude',
                reason=f'Could not load image: {e}',
                assessed_at=datetime.utcnow().isoformat(),
            )
        
        system_prompt = """You are helping pre-process images for a profile picture selection tournament.

For each image, assess whether it's suitable and determine if any cropping is needed.

Respond with a JSON object (no markdown, just raw JSON):
{
    "status": "ok" | "crop" | "exclude",
    "reason": "brief explanation",
    "crop": [left, top, right, bottom] or null
}

Guidelines:

STATUS "exclude" if:
- Contains explicit nudity or sexual content
- Contains graphic violence or gore
- Is primarily text with no meaningful image content
- Is too low quality to use as a profile picture
- Contains content that would be inappropriate as a public avatar

STATUS "crop" if:
- Image has a "caption" layout: artwork on one side, text on the other
- Image has large text overlays or watermarks that could be cropped out
- Image has significant borders or padding that should be removed
- The interesting content is only in a portion of the image

For crops, provide pixel coordinates as [left, top, right, bottom] based on the original dimensions.
The image dimensions are: WIDTH x HEIGHT

STATUS "ok" if:
- Image is suitable as-is for profile picture use
- No cropping needed

Be practical - minor text or watermarks that don't dominate the image are fine. It's also the internet so images on the "lewder" side or on the edge should be marked as okay, only things visibly pornographic should be classified as such."""

        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_data,
                }
            },
            {
                "type": "text", 
                "text": f"Image dimensions: {width} x {height}\n\nAssess this image for profile picture suitability."
            }
        ]
        
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}]
            )
            
            response_text = response.content[0].text.strip()
            
            # Parse JSON response
            # Handle potential markdown wrapping
            if response_text.startswith('```'):
                response_text = response_text.split('```')[1]
                if response_text.startswith('json'):
                    response_text = response_text[4:]
            
            result = json.loads(response_text)
            
            crop_box = None
            if result.get('crop'):
                crop_box = tuple(result['crop'])
            
            assessment = ImageAssessment(
                image_id=image_id,
                path=str(path),
                status=result['status'],
                reason=result.get('reason', ''),
                crop_box=crop_box,
                assessed_at=datetime.utcnow().isoformat(),
            )
            
        except anthropic.BadRequestError as e:
            # API refused to process - likely NSFW
            assessment = ImageAssessment(
                image_id=image_id,
                path=str(path),
                status='exclude',
                reason=f'API refused to process (likely content policy): {str(e)[:100]}',
                assessed_at=datetime.utcnow().isoformat(),
            )
        except json.JSONDecodeError as e:
            # Couldn't parse response, mark as ok to be safe
            assessment = ImageAssessment(
                image_id=image_id,
                path=str(path),
                status='ok',
                reason=f'Could not parse assessment response',
                assessed_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            assessment = ImageAssessment(
                image_id=image_id,
                path=str(path),
                status='exclude',
                reason=f'Assessment error: {str(e)[:100]}',
                assessed_at=datetime.utcnow().isoformat(),
            )
        
        self.assessments[image_id] = assessment
        return assessment
    
    def _apply_crop(self, src_path: Path, dest_path: Path, crop_box: tuple[int, int, int, int]):
        """Crop an image and save to destination."""
        with Image.open(src_path) as img:
            cropped = img.crop(crop_box)
            
            # Ensure output format matches or use JPEG
            if dest_path.suffix.lower() in ('.jpg', '.jpeg'):
                if cropped.mode == 'RGBA':
                    cropped = cropped.convert('RGB')
                cropped.save(dest_path, 'JPEG', quality=95)
            elif dest_path.suffix.lower() == '.png':
                cropped.save(dest_path, 'PNG')
            else:
                if cropped.mode == 'RGBA':
                    cropped = cropped.convert('RGB')
                cropped.save(dest_path, 'JPEG', quality=95)
    
    def discover_images(self) -> list[Path]:
        """Find all images in the images directory."""
        image_paths = []
        for ext in IMAGE_EXTENSIONS:
            image_paths.extend(self.images_dir.rglob(f'*{ext}'))
            image_paths.extend(self.images_dir.rglob(f'*{ext.upper()}'))
        return image_paths
    
    def run(self, batch_size: int = 20) -> dict:
        """Run preprocessing on all images."""
        console.print("[bold cyan]🔍 Image Pre-processor[/bold cyan]\n")
        
        # Discover images
        image_paths = self.discover_images()
        console.print(f"Found {len(image_paths)} images to assess\n")
        
        if not image_paths:
            return {'ok': 0, 'crop': 0, 'exclude': 0}
        
        # Filter to only unassessed images
        unassessed = []
        for path in image_paths:
            image_id = self._get_image_id(path)
            if image_id not in self.assessments:
                unassessed.append(path)
        
        console.print(f"Already assessed: {len(image_paths) - len(unassessed)}")
        console.print(f"Need to assess: {len(unassessed)}\n")
        
        # Assess images
        if unassessed:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                console=console,
            ) as progress:
                task = progress.add_task("[cyan]Assessing images...", total=len(unassessed))
                
                for path in unassessed:
                    assessment = self._assess_image(path)
                    
                    status_icon = {'ok': '✓', 'crop': '✂', 'exclude': '✗'}[assessment.status]
                    status_color = {'ok': 'green', 'crop': 'yellow', 'exclude': 'red'}[assessment.status]
                    
                    progress.console.print(
                        f"  [{status_color}]{status_icon}[/{status_color}] "
                        f"[dim]{path.name}[/dim] - {assessment.reason[:50]}"
                    )
                    
                    progress.advance(task)
                    self._save_state()
        
        # Tally results
        stats = {'ok': 0, 'crop': 0, 'exclude': 0}
        for assessment in self.assessments.values():
            stats[assessment.status] = stats.get(assessment.status, 0) + 1
        
        console.print(f"\n[bold]Assessment Results:[/bold]")
        console.print(f"  [green]✓ OK:[/green] {stats['ok']}")
        console.print(f"  [yellow]✂ Need crop:[/yellow] {stats['crop']}")
        console.print(f"  [red]✗ Excluded:[/red] {stats['exclude']}")
        
        # Process crops and create clean output directory
        console.print(f"\n[cyan]Processing approved images...[/cyan]")
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        processed = 0
        
        for assessment in self.assessments.values():
            if assessment.status == 'exclude':
                continue
            
            src_path = Path(assessment.path)
            if not src_path.exists():
                continue
            
            dest_path = self.output_dir / src_path.name
            
            # Handle duplicates
            counter = 1
            while dest_path.exists():
                dest_path = self.output_dir / f"{src_path.stem}_{counter}{src_path.suffix}"
                counter += 1
            
            if assessment.status == 'crop' and assessment.crop_box:
                try:
                    self._apply_crop(src_path, dest_path, assessment.crop_box)
                    processed += 1
                except Exception as e:
                    console.print(f"[red]Failed to crop {src_path.name}: {e}[/red]")
            else:
                # Just copy
                import shutil
                shutil.copy2(src_path, dest_path)
                processed += 1
        
        console.print(f"\n[green]✅ Processed {processed} images to {self.output_dir}[/green]")
        
        return stats


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Pre-process images for tournament')
    parser.add_argument('--images-dir', type=Path, 
                       default=Path(__file__).parent.parent / 'images',
                       help='Directory containing source images')
    parser.add_argument('--output-dir', type=Path,
                       default=Path(__file__).parent.parent / 'images_processed',
                       help='Directory for processed images')
    parser.add_argument('--reset', action='store_true',
                       help='Reset assessment state and start fresh')
    
    args = parser.parse_args()
    
    state_path = Path(__file__).parent.parent / 'data' / 'preprocess_state.json'
    
    if args.reset and state_path.exists():
        state_path.unlink()
        console.print("[yellow]Assessment state reset[/yellow]")
    
    preprocessor = ImagePreprocessor(args.images_dir, args.output_dir, state_path)
    preprocessor.run()


if __name__ == '__main__':
    main()
