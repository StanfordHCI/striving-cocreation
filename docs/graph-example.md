# Interactive graph sources

The public graph in `src/components/graph/graph.json` adapts the worked example
in **Figure 1 of the supplied UIST camera-ready paper**. It displays the
hierarchy after the illustrated edit, rather than animating an invented edit
or inference history.

Source files supplied by the author:

- `uist26-156-TAPS/source/figures/teaser.png`: all five action labels, both
  activity labels, both striving labels, and their parent connections.
- `uist26-156-TAPS/source/sections/03_system.tex`, Section 3.1.1: the operation
  “browsing YouTube search results for classical music.” Only its initial
  capitalization is changed for display.

The cultural-identity branch retains the activity about managing homesickness
through personal interests, food, news, and family messages. The family-care
branch uses the user’s replacement activity, “Supporting a family member with
aging in place,” and the resulting striving about care for aging family.

Figure 1 illustrates screen observations but does not provide their complete
operation log. The other nine operation labels are **reconstructed examples**
of visible interactions beneath the published actions. Each is marked
“Illustrative operation · Reconstructed for this demo” in the source data.
They must not be described as captured events, actual model output, or a
participant’s event log. The first published operation is marked separately.
These provenance notes are retained for maintenance; the graph UI shows node
labels and connections without a source section or adaptation caption.

The graph contains 19 nodes: 10 operations, 5 actions, 2 activities, and
2 strivings. It does not merge distinct activities into a category node.
The former generic synthetic hierarchy, fabricated goal revision history,
and unrelated figure presented as “From your screen” have been removed.
Decorative blurred background images remain fictional and are not evidence.

The four definitions are exact excerpts from Section 3.1.1, kept separately
in `src/data/paperHierarchy.ts`. Paper reference:
<https://arxiv.org/abs/2605.00497v2>.
