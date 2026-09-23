import { defineConfig } from 'astro/config';
import react from '@astrojs/react';

export default defineConfig({
  integrations: [react()],
  devToolbar: { enabled: false },
  site: 'https://stanfordhci.github.io',
  base: process.env.TEMPO_GITHUB_PAGES === 'true' ? '/striving-cocreation' : '/',
});
