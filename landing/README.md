# Toda landing page

The public site is a single self-contained `index.html`: no build step, remote
font request, framework, stock art, fake product screenshot, or invented metric.

Run it locally with:

```bash
cd landing
python3 -m http.server 4000
```

The design is a light technical editorial map for self-hosting operators. The
organization graph is semantic inline SVG, the system and principles sections
use rules rather than generic card grids, and the install section gives one
primary command per operating-system family. Keep the restrained pine/acid
palette, sharp geometry, visible information relationships, and plain claims
when editing it.

Because the file is self-contained, it can be hosted by GitHub Pages, Netlify,
Vercel, Cloudflare Pages, or any static object host. The GitHub links currently
point to the source repository and the install URLs assume the `toda.ai` domain.
