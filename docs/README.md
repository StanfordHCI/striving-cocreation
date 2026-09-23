# Striving Co-Creation — paper website

The repository contains one website: the selected Soft design. It uses DM Sans,
a brown-to-white introductory gradient, a centered paper title and authors,
one Stanford University affiliation, and arXiv/GitHub links. A click-to-play
video and full abstract lead into the interactive hierarchy. The laptop is
visible on arrival; scrolling enters its softly blurred screen, builds the
hierarchy, and pulls back. Results, architecture, editing, and citation follow.

## Run locally

```bash
cd docs
npm ci
npm run dev            # http://localhost:4321/
```

## Build and check

```bash
npm run check         # generates Astro types before checking TypeScript
npm run build:site     # production files in dist-site/
```

The build verifies the fictional backgrounds against `demo-screens.json` and
rejects former recorded-media directories. The deployment packager includes
only the paper page, its compiled dependencies, licensed fonts, fictional
backgrounds, and approved UIST figures/video. Generated output is ignored by Git.

## Publish on Stanford HCI GitHub Pages

The repository is `StanfordHCI/striving-cocreation`; its project website URL is:

**https://stanfordhci.github.io/striving-cocreation/**

A repository administrator must enable Pages once:

1. Open [Settings → Pages](https://github.com/StanfordHCI/striving-cocreation/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Open [Actions → Deploy paper website](https://github.com/StanfordHCI/striving-cocreation/actions/workflows/website.yml)
   and choose **Run workflow** on `main`.
4. Wait for the build and deployment jobs to pass, then open the website URL.

Later changes to `docs/` on `main` rebuild and deploy automatically. Until Pages
is enabled, the workflow builds the artifact and explains the setup step without
attempting deployment. No personal access token or separate web server is needed;
the workflow uses GitHub's built-in token. Organization policy may require an
administrator to permit GitHub Actions or Pages.

The workflow sets `TEMPO_GITHUB_PAGES=true`, which prefixes routes and assets with
`/striving-cocreation`. Local development continues to serve from `/`. To check
the same build locally:

```bash
TEMPO_GITHUB_PAGES=true npm run build:site
TEMPO_GITHUB_PAGES=true npx astro preview --outDir ./dist-site --port 4322
# http://localhost:4322/striving-cocreation/
```

See [GitHub's custom Pages workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).
GitHub Pages hosts this static paper website; it does not run the Python backend
or access anyone's recorded data.

## Source guide

- `src/components/paper/PaperPage.astro`: paper metadata, author list, resource links, and page order.
- `PaperVideo.astro`: video playback; no autoplay, one centered play button.
- `PaperAbstract.astro` and `src/data/paperAbstract.ts`: camera-ready abstract, aligned to the video's 800px width.
- `PaperDetails.astro` and `src/data/paper.bib`: UIST results, architecture, editing, visible citation, copy button, and Bloom design credit.
- `PaperInductionMotif.astro` and `PaperEditingMotif.astro`: original decorative SVG drawings beside the introduction.
- `src/components/graph/GraphStory.tsx` and `graph.css`: scroll animation, labels, node selection, and connected-node details. Reduced motion shows the complete graph and definitions without requiring the animation.
- `src/components/graph/graph.json` and `src/data/paperHierarchy.ts`: the paper example and definition excerpts. See [graph provenance](./graph-example.md).
- `src/styles/paper*.css`: paper layout, soft palette, and graph clearances. Shared styles retain their descriptive filenames; there are no alternative routes or theme selectors.
- `public/figures/uist2026/`: original camera-ready figures; its README records provenance and checksums.
- `public/media/`: approved 30-second UIST preview, compressed to H.264/AAC, and its poster. The original stays outside the repository.
- `public/demo-screens/`: four generated fictional screens with blur baked into their pixels, not personal screenshots. See [asset provenance and verification](./demo-screens.md).
- `src/assets/fonts/`: locally bundled DM Sans; license in `public/licenses/DM-Sans-OFL.txt`. No external font requests.

Paper essentials and figures render as HTML. A keyboard skip link bypasses the
animation. The footer credits [Bloom](https://stanfordhci.github.io/Bloom/) as design
inspiration. Alternative designs and prototype routes are not part of this tree.
