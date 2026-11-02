(() => {
  'use strict';
  const COOKIE = 'cookie_consent_v1';
  const MAX_AGE = 60 * 60 * 24 * 180;
  const banner = document.getElementById('cookieConsentBanner');
  const review = document.getElementById('cookieConsentReview');
  if (!banner || !review) return;
  const readChoice = () => document.cookie.split('; ')
    .find((item) => item.startsWith(`${COOKIE}=`))?.split('=')[1] || '';
  const show = () => {
    banner.classList.remove('d-none');
    document.getElementById('cookieConsentReject')?.focus();
  };
  const hide = () => banner.classList.add('d-none');
  const save = (choice) => {
    const secure = window.location.protocol === 'https:' ? '; secure' : '';
    document.cookie = `${COOKIE}=${choice}; path=/; max-age=${MAX_AGE}; samesite=lax${secure}`;
    hide();
    window.dispatchEvent(new CustomEvent('cookie-consent-changed', { detail: { optional: choice === 'accepted' } }));
  };
  document.getElementById('cookieConsentAccept')?.addEventListener('click', () => save('accepted'));
  document.getElementById('cookieConsentReject')?.addEventListener('click', () => save('rejected'));
  document.getElementById('cookieConsentClose')?.addEventListener('click', () => save('rejected'));
  review.addEventListener('click', show);
  if (!['accepted', 'rejected'].includes(readChoice())) show();
})();
