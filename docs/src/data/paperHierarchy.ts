// Definition excerpts from the supplied UIST camera-ready source:
// sections/03_system.tex, “The Striving Hierarchy” (Section 3.1.1).
// The striving excerpt comes from that section's introductory paragraph;
// Initial capitalization and ending punctuation are normalized where needed.
export const paperHierarchy = [
  {
    type: 'op', title: 'Operation',
    definition: 'Operations are the lowest level of the hierarchy and describe atomic behavioral interactions such as a click, a scroll, or a keystroke.',
  },
  {
    type: 'ac', title: 'Action',
    definition: 'Actions are goal-directed sequences composed from contiguous operations.',
  },
  {
    type: 'av', title: 'Activity',
    definition: 'Activities represent recurring patterns of actions that share a common motive. They are broader than single tasks but narrower than a life domain.',
  },
  {
    type: 'st', title: 'Striving',
    definition: 'Ongoing pursuits that organize multiple activities and persist beyond any single project.',
  },
] as const;
