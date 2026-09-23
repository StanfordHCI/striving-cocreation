# Fictional graph backgrounds

`public/demo-screens/` contains four generated desktop interface mockups:
`research.png`, `notes.png`, `inbox.png`, and `schedule.png`.

Created on 2026-09-21 with the built-in image generation tool. The exact prompts
and SHA-256 checksums are in [demo-screens.json](./demo-screens.json). The generated
PNG files are used directly, including their AI provenance metadata.

The earlier website's timelapse was inspected locally to understand its broad
screen layouts. No real screenshot, screenshot crop, participant text, account
information, or image reference was supplied to the generator. These are new
fictional screens, not edits or anonymized copies of recorded screens.

The blur is already part of each image's pixels. Opening an image directly,
downloading it from GitHub, or disabling CSS still shows a blurred mockup.
There is no real source content embedded in these backgrounds to recover.
This does not claim that applying blur to a real screenshot guarantees privacy.

The graph uses these images decoratively; they are not evidence for the example
nodes. Published paper figures and the separately supplied video have their own
provenance and are outside this set.

## Keep recorded media out

Real capture archives stay outside this repository. Never restore the former
`public/screens/`, `public/screenshots/`, `public/timelapse/`, or `public/videos/`
directories. They are ignored by Git and rejected by the website's `predev` and
`prebuild` checks because Astro would otherwise publish even ignored files.
The check also rejects added, replaced, missing, or linked files in
`public/demo-screens/` until the manifest is explicitly reviewed and updated.
It is an inventory check, not an automatic detector of sensitive image content.

A local audit on 2026-09-21 compared Git blob hashes for 1,009 archived private
media files and original timelapse frames against all reachable refs in this
repository: no exact matches. No recorded-media file history was found at the
former website capture paths. The audit did not inspect the live GitHub server
or guarantee detection of resized or re-encoded copies. Do not import history
from the older private research repository.
