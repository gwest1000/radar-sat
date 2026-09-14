/** On-demand capture only: no extra work, downloads, or PNGs during playback. */
export function frameExportFilename(product: string, validTime: string): string {
  const region = product.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const time = new Date(validTime);
  if (!Number.isFinite(time.getTime())) throw new Error("This frame has no valid timestamp yet.");
  return `radar-sat-${region}-${time.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}.png`;
}

function canvas(width: number, height: number): HTMLCanvasElement {
  const result = document.createElement("canvas");
  result.width = Math.max(1, Math.round(width));
  result.height = Math.max(1, Math.round(height));
  return result;
}

function context(surface: HTMLCanvasElement): CanvasRenderingContext2D {
  const result = surface.getContext("2d");
  if (!result) throw new Error("Your browser could not create the frame image.");
  return result;
}

function visible(element: HTMLElement, root: HTMLElement): boolean {
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (node.hidden || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    if (node === root) break;
  }
  return element.getBoundingClientRect().width > 0;
}

// Current satellite styling uses these three colour operations. Implement them
// in pixels for Safari versions without CanvasRenderingContext2D.filter.
export function applyColourFilter(pixels: Uint8ClampedArray, filter: string): void {
  const operations = [...filter.matchAll(/(saturate|brightness|contrast)\(([\d.]+)\)/g)];
  for (let i = 0; i < pixels.length; i += 4) {
    let r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
    for (const [, name, value] of operations) {
      const amount = Number(value);
      if (name === "saturate") {
        const grey = 0.213 * r + 0.715 * g + 0.072 * b;
        r = grey + amount * (r - grey); g = grey + amount * (g - grey); b = grey + amount * (b - grey);
      } else if (name === "brightness") {
        r *= amount; g *= amount; b *= amount;
      } else {
        r = amount * (r - 127.5) + 127.5; g = amount * (g - 127.5) + 127.5; b = amount * (b - 127.5) + 127.5;
      }
      r = Math.min(255, Math.max(0, r)); g = Math.min(255, Math.max(0, g)); b = Math.min(255, Math.max(0, b));
    }
    pixels[i] = r; pixels[i + 1] = g; pixels[i + 2] = b;
  }
}

async function loadExportImage(url: string): Promise<HTMLImageElement> {
  const image = new Image();
  image.crossOrigin = "anonymous";
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("A map layer took too long to export. Please try again.")), 20_000);
    image.onload = () => { clearTimeout(timer); resolve(); };
    image.onerror = () => { clearTimeout(timer); reject(new Error("A map layer could not be exported. Please try again.")); };
    image.src = url;
  });
  return image;
}

/** Capture mutable video/canvas surfaces and DOM text BEFORE the first await.
 * Image URLs are frozen too, then loaded with CORS on demand. This keeps image
 * playback unchanged and cannot accidentally export a newer live-edge raster.
 */
export async function captureMapFrame(stage: HTMLElement, nativeWidth: number): Promise<Blob> {
  const box = stage.getBoundingClientRect();
  const originX = box.left + stage.clientLeft, originY = box.top + stage.clientTop;
  const width = stage.clientWidth, height = stage.clientHeight;
  if (!width || !height) throw new Error("The map is not ready to export yet.");
  const elements = [...stage.querySelectorAll<HTMLImageElement | HTMLCanvasElement | HTMLVideoElement>(
    "img.map-layer, .video-loop-decoder, canvas",
  )].filter((element) => visible(element, stage));
  if (!elements.length) throw new Error("Wait for the map to finish loading, then export again.");
  const video = elements.find((element) => element instanceof HTMLVideoElement) as HTMLVideoElement | undefined;
  const resolution = video ? video.videoWidth * width / video.getBoundingClientRect().width : nativeWidth;
  const scale = Math.min(resolution / width, 4096 / width, Math.sqrt(8_000_000 / (width * height)));
  const output = canvas(width * scale, height * scale);
  const ctx = context(output);
  const layers = elements.map((element) => {
    const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
    let source: HTMLCanvasElement | string;
    if (element instanceof HTMLImageElement) {
      if (!element.complete || !element.naturalWidth) throw new Error("A map layer is still loading. Please try again.");
      source = element.currentSrc || element.src;
    } else {
      if (element instanceof HTMLVideoElement && (element.readyState < 2 || element.seeking)) {
        throw new Error("The video is changing frames. Please try again in a moment.");
      }
      // Clip while copying, so the video's hidden clock strip and cropped
      // parent-domain pixels can never leak into the exported image.
      source = canvas(output.width, output.height);
      const copy = context(source);
      // North-America's Canadian lightning fallback is clipped to northern
      // coverage in CSS. Preserve that clip rather than restoring hidden bolts.
      const inset = style.clipPath.match(/^inset\(([^)]+)\)$/);
      if (inset) {
        const values = inset[1].split(/\s+/);
        const sides = [values[0], values[1] ?? values[0], values[2] ?? values[0], values[3] ?? values[1] ?? values[0]];
        const [top, right, bottom, left] = sides.map((value, i) =>
          parseFloat(value) * (value.endsWith("%") ? (i % 2 ? rect.width : rect.height) / 100 : 1));
        copy.beginPath();
        copy.rect((rect.left - originX + left) * scale, (rect.top - originY + top) * scale,
          (rect.width - left - right) * scale, (rect.height - top - bottom) * scale);
        copy.clip();
      }
      copy.drawImage(element, (rect.left - originX) * scale, (rect.top - originY) * scale, rect.width * scale, rect.height * scale);
    }
    return { source, x: (rect.left - originX) * scale, y: (rect.top - originY) * scale,
      width: rect.width * scale, height: rect.height * scale, opacity: Number(style.opacity), filter: style.filter };
  });
  // Preserve actual wrapped text positions, including Pacific/UTC clocks,
  // source times and missing-data warnings. No HTML screenshot dependency.
  const panels = [...stage.querySelectorAll<HTMLElement>(".map-status, .coverage-key")]
    .filter((element) => visible(element, stage)).map((element) => {
      const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      const glyphs: { text: string; x: number; y: number; font: string; colour: string }[] = [];
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (!node.parentElement || !visible(node.parentElement, stage)) continue;
        const textStyle = getComputedStyle(node.parentElement);
        const range = document.createRange();
        for (let i = 0; i < (node.textContent?.length ?? 0); i++) {
          const text = node.textContent![i];
          if (!text.trim()) continue;
          range.setStart(node, i); range.setEnd(node, i + 1);
          const position = range.getBoundingClientRect();
          glyphs.push({ text, x: position.left - originX, y: position.top - originY + (position.height - parseFloat(textStyle.fontSize)) / 2,
            font: `${textStyle.fontWeight} ${textStyle.fontSize} ${textStyle.fontFamily}`, colour: textStyle.color });
        }
      }
      const swatch = element.querySelector<HTMLElement>(".hatch-swatch")?.getBoundingClientRect();
      return { x: rect.left - originX, y: rect.top - originY, width: rect.width, height: rect.height,
        background: style.backgroundColor, border: style.borderTopColor, borderWidth: parseFloat(style.borderTopWidth),
        radius: parseFloat(style.borderTopLeftRadius), glyphs,
        swatch: swatch ? { x: swatch.left - originX, y: swatch.top - originY, width: swatch.width, height: swatch.height } : undefined };
    });
  ctx.fillStyle = getComputedStyle(stage).backgroundColor;
  ctx.fillRect(0, 0, output.width, output.height);
  for (const layer of layers) {
    const surface = typeof layer.source === "string" ? canvas(output.width, output.height) : layer.source;
    if (typeof layer.source === "string") {
      const image = await loadExportImage(layer.source);
      context(surface).drawImage(image, layer.x, layer.y, layer.width, layer.height);
    }
    ctx.save();
    ctx.globalAlpha = layer.opacity;
    if (layer.filter !== "none") {
      if ("filter" in ctx) ctx.filter = layer.filter;
      else {
        const pixels = context(surface).getImageData(0, 0, surface.width, surface.height);
        applyColourFilter(pixels.data, layer.filter);
        context(surface).putImageData(pixels, 0, 0);
      }
    }
    ctx.drawImage(surface, 0, 0);
    ctx.restore();
    surface.width = surface.height = 1;
  }
  ctx.scale(scale, scale);
  for (const panel of panels) {
    ctx.beginPath(); ctx.roundRect(panel.x, panel.y, panel.width, panel.height, panel.radius);
    ctx.fillStyle = panel.background; ctx.fill();
    if (panel.borderWidth) { ctx.strokeStyle = panel.border; ctx.lineWidth = panel.borderWidth; ctx.stroke(); }
    ctx.textBaseline = "top";
    for (const glyph of panel.glyphs) { ctx.font = glyph.font; ctx.fillStyle = glyph.colour; ctx.fillText(glyph.text, glyph.x, glyph.y); }
    if (panel.swatch) {
      const swatch = panel.swatch;
      ctx.save(); ctx.beginPath(); ctx.rect(swatch.x, swatch.y, swatch.width, swatch.height); ctx.clip();
      ctx.strokeStyle = "#82929b"; ctx.lineWidth = 1;
      for (let x = -swatch.height; x < swatch.width; x += 5) {
        ctx.beginPath(); ctx.moveTo(swatch.x + x, swatch.y + swatch.height); ctx.lineTo(swatch.x + x + swatch.height, swatch.y); ctx.stroke();
      }
      ctx.restore();
    }
  }
  return new Promise<Blob>((resolve, reject) => {
    output.toBlob((blob) => {
      output.width = output.height = 1;
      if (blob) resolve(blob); else reject(new Error("The browser could not encode this frame. Please try again."));
    }, "image/png");
  });
}
