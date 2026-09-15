"""Keep synchronization pixels away from the visible picture boundary."""
from PIL import Image

STRIP_HEIGHT = 16
GUARD_HEIGHT = 8

def append_clock_strip(content: Image.Image, phase: int) -> Image.Image:
    width, height = content.size
    encoded = Image.new("RGB", (width, height + STRIP_HEIGHT), "white" if phase else "black")
    encoded.paste(content.convert("RGB"), (0, 0))
    edge = content.crop((0, height - 1, width, height)).convert("RGB")
    guard = edge.resize((width, GUARD_HEIGHT), Image.Resampling.NEAREST)
    encoded.paste(guard, (0, height))
    edge.close()
    guard.close()
    return encoded
