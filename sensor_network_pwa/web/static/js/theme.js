(() => {
  'use strict';

  const COOKIE = 'theme_preference';
  const MAX_AGE = 60 * 60 * 24 * 365;
  const cookieValue = () => document.cookie.split('; ')
    .find((item) => item.startsWith(`${COOKIE}=`))?.split('=')[1] || '';
  const deviceTheme = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const saved = cookieValue();
  const initial = ['light', 'dark'].includes(saved) ? saved : deviceTheme();
  document.documentElement.dataset.bsTheme = initial;

  document.addEventListener('DOMContentLoaded', () => {
    const selector = document.getElementById('themePreference');
    if (!selector) return;
    selector.value = document.documentElement.dataset.bsTheme || 'light';
    selector.addEventListener('change', () => {
      const theme = selector.value === 'dark' ? 'dark' : 'light';
      const secure = window.location.protocol === 'https:' ? '; secure' : '';
      document.documentElement.dataset.bsTheme = theme;
      document.cookie = `${COOKIE}=${theme}; path=/; max-age=${MAX_AGE}; samesite=lax${secure}`;
      window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme } }));
    });
  });
})();
