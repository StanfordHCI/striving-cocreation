"""Apple Vision OCR helper for local text extraction from screenshots.

Compiles a Swift helper binary (using macOS Vision framework) on first use,
then runs it via subprocess to extract text from JPEG screenshots.

Fail-open: if swiftc is unavailable or compilation fails, OCR is silently
disabled and all screenshots are allowed through.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Optional


BIN_DIR = os.path.expanduser("~/.cache/tempo/bin")
OCR_BINARY = os.path.join(BIN_DIR, "ocr-helper")
OCR_TIMEOUT_SECONDS = 10

_ocr_available = False

# Swift source — uses VNRecognizeTextRequest for fast on-device OCR.
SWIFT_SOURCE = r"""
import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: ocr-helper <image-path>\n", stderr)
    exit(1)
}

let imagePath = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: imagePath),
      let tiffData = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiffData),
      let cgImage = bitmap.cgImage else {
    fputs("Failed to load image\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .fast
request.usesLanguageCorrection = false

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try handler.perform([request])

guard let observations = request.results else { exit(0) }

for observation in observations {
    if let candidate = observation.topCandidates(1).first {
        print(candidate.string)
    }
}
"""


async def init() -> bool:
    """Compile the OCR helper binary if needed. Returns True if OCR is available."""
    global _ocr_available

    # Already compiled
    if os.path.exists(OCR_BINARY):
        _ocr_available = True
        return True

    # Check for swiftc
    if not shutil.which("swiftc"):
        print("[ocr] swiftc not found — OCR helper disabled")
        return False

    os.makedirs(BIN_DIR, exist_ok=True)
    src_path = os.path.join(BIN_DIR, "ocr-helper.swift")
    Path(src_path).write_text(SWIFT_SOURCE)

    try:
        proc = await asyncio.create_subprocess_exec(
            "swiftc", "-O", "-o", OCR_BINARY, src_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            print(f"[ocr] Failed to compile OCR helper: {stderr.decode().strip()}")
            return False
    except asyncio.TimeoutError:
        print("[ocr] OCR helper compilation timed out")
        return False
    except Exception as exc:
        print(f"[ocr] OCR helper compilation error: {exc}")
        return False

    _ocr_available = True
    print("[ocr] OCR helper compiled successfully")
    return True


async def extract_text(image_path: str) -> Optional[str]:
    """Run OCR on an image file and return extracted text, or None on failure."""
    if not _ocr_available:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            OCR_BINARY, image_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=OCR_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            return None
        text = stdout.decode().strip()
        return text if text else None
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
