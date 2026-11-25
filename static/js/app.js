/* Простой JS: выбор файла и опрос реального прогресса миграции. */
(() => {
  const fileInput = document.querySelector('#database');
  const dropzone = document.querySelector('#dropzone');
  if (fileInput && dropzone) {
    const label = document.querySelector('#file-label');
    const update = () => { label.textContent = fileInput.files[0]?.name || 'Нажмите, чтобы выбрать файл'; };
    fileInput.addEventListener('change', update);
    for (const type of ['dragenter', 'dragover']) dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add('dragover'); });
    for (const type of ['dragleave', 'drop']) dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove('dragover'); });
    dropzone.addEventListener('drop', event => { if (event.dataTransfer.files.length) { fileInput.files = event.dataTransfer.files; update(); } });
  }
  const container = document.querySelector('#migration-progress');
  if (!container) return;
  const phase = document.querySelector('#phase');
  const percent = document.querySelector('#percent');
  const fill = document.querySelector('#progress-fill');
  const processed = document.querySelector('#processed');
  const error = document.querySelector('#progress-error');
  const link = document.querySelector('#result-link');
  const token = document.querySelector('meta[name="csrf-token"]').content;
  const progress = async () => {
    try {
      const response = await fetch(container.dataset.status, { cache: 'no-store' });
      if (!response.ok) throw new Error('Не удалось получить состояние миграции');
      const info = await response.json();
      phase.textContent = info.phase;
      percent.textContent = `${info.percent}%`;
      fill.style.width = `${info.percent}%`;
      processed.textContent = `${info.progress} / ${info.total} записей`;
      if (info.status === 'done' || info.status === 'failed') {
        if (info.status === 'failed') { error.textContent = info.error || 'Миграция остановлена'; error.hidden = false; }
        link.hidden = false;
        window.location.assign(info.result_url);
        return;
      }
      window.setTimeout(progress, 350);
    } catch (err) {
      phase.textContent = 'Не удалось обновить прогресс';
      error.textContent = `${err.message}. Проверьте, запущен ли локальный сервер DocShift, и обновите страницу.`;
      error.hidden = false;
      link.hidden = false;
    }
  };
  (async () => {
    try {
      const response = await fetch(container.dataset.start, { method:'POST', headers: {'X-CSRF-Token':token}, cache:'no-store' });
      if (!response.ok) throw new Error('Не удалось запустить миграцию');
      progress();
    } catch (err) { error.textContent = err.message; error.hidden = false; link.hidden = false; }
  })();
})();
