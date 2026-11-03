(() => {
  'use strict';
  const input = document.getElementById('docsSearch');
  const results = document.getElementById('docsSearchResults');
  const configNode = document.getElementById('docsConfig');
  if (!input || !results || !configNode) return;
  const config = JSON.parse(configNode.textContent);
  let timer;
  function render(items) {
    results.replaceChildren();
    if (!input.value.trim()) return;
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'docs-search-empty';
      empty.textContent = 'No matching guide found.';
      results.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const link = document.createElement('a');
      link.href = item.url;
      const title = document.createElement('strong');
      title.textContent = item.title;
      const snippet = document.createElement('span');
      snippet.textContent = item.snippet;
      link.append(title, snippet);
      results.appendChild(link);
    });
  }
  input.addEventListener('input', () => {
    window.clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < 2) { results.replaceChildren(); return; }
    timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${config.searchUrl}?q=${encodeURIComponent(query)}`, {headers: {Accept: 'application/json'}});
        render(response.ok ? (await response.json()).results : []);
      } catch (_error) { render([]); }
    }, 180);
  });
})();
