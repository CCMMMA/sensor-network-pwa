(function () {
  // Open the tab named in the URL (actions come back to the tab they started from).
  const showTab = (hash) => {
    const trigger = document.querySelector(`#adminTabs [data-bs-target="${hash}"]`);
    if (trigger && window.bootstrap) bootstrap.Tab.getOrCreateInstance(trigger).show();
  };
  const showTabFromUrl = () => {
    if (['#users', '#stations', '#requests'].includes(window.location.hash)) showTab(window.location.hash);
  };
  showTabFromUrl();
  window.addEventListener('hashchange', showTabFromUrl);
  document.querySelectorAll('#adminTabs [data-bs-toggle="tab"]').forEach((trigger) => {
    trigger.addEventListener('shown.bs.tab', () => {
      window.history.replaceState({}, '', trigger.dataset.bsTarget);
    });
  });

  document.querySelectorAll('[data-confirm]').forEach((button) => {
    button.addEventListener('click', (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });

  // A changed policy is saved at once; the Save button remains for browsers without scripts.
  document.querySelectorAll('.policy-select').forEach((select) => {
    select.form.querySelector('.policy-save').classList.add('d-none');
    select.addEventListener('change', () => select.form.requestSubmit());
  });

  const search = document.getElementById('userSearch');
  const empty = document.getElementById('userSearchEmpty');
  search.addEventListener('input', () => {
    const term = search.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll('#userTable .user-row').forEach((row) => {
      const match = !term || row.dataset.search.includes(term);
      row.classList.toggle('d-none', !match);
      if (match) shown += 1;
    });
    empty.classList.toggle('d-none', shown > 0);
  });
})();
