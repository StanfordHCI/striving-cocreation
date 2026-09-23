# Tempo visual direction

The app and paper website share DM Sans, a soft brown palette, and a subtle
brown-to-white gradient. Colors distinguish hierarchy levels while controls
remain quiet and readable.

| Decision | Choice |
| --- | --- |
| UI and display font | Locally bundled DM Sans |
| Page background | White with a soft `#f7f4f2` gradient |
| Panels | White |
| Text | Dark brown, `#322d2a` |
| Secondary text | Muted brown, `#6b625e` |
| Primary action | Walnut brown |
| Selection | Pale brown |

Fonts load locally; their licenses remain beside the assets.

The app keeps a persistent sidebar on desktop and a horizontal navigation row
in narrow windows. One button in the top bar starts or stops recording from any
view. Recording settings persist when switching views. Record groups personal context and model setup together,
followed by application and website exclusions. Hierarchy keeps its brown
hierarchy-level colors. Every graph node is circular: deep brown for goals,
medium brown for activities, caramel for actions, and pale sand for operations.
The broad range of fill colors and node sizes distinguishes the levels.
Minimum sizes and dark outlines keep the small nodes visible. Hover and
selection preserve each level's appearance while emphasizing its connections.
Strivings always show their full labels. Hovering, focusing, or selecting a node
shows its full label too. Long labels wrap at a fixed readable text size, and
nearby labels move apart with a connector back to their node. The legend matches
the nodes.
Walnut identifies primary controls throughout the app.
Hierarchy opens in Cards. Table is a secondary view with Goals, Activities,
Actions, and Operations tabs, search, sortable columns, and links to the shared
inspector. Switching between Cards and Table preserves staged card edits.
Graph lives only in its own sidebar view.

“About you” uses an introduction and section navigation beside the form on
desktop, then one column on smaller screens. Back and save actions remain
visible during scrolling. All answers are optional. Back, Cancel, and Escape
discard unsaved edits and return to Record. Only explicitly labeled start
actions continue into recording.

Design tokens live in `electron-app/src/styles/main.css`. Form layout lives in
`electron-app/src/components/OnboardingForm.css`.

Headings name the contents of a section. Supporting text explains what the user
can do or how Tempo uses their input. Omit decorative taglines and subtitles
that repeat information already provided by the heading or questions.
