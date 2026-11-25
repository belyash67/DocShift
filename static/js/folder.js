/* Перенос папки через локальный сервер и запись результата с разрешения браузера. */
(() => {
  const choose = document.getElementById('folder-pick');
  const input = document.getElementById('folder-input');
  const targetPick = document.getElementById('folder-target-pick');
  const targetSummary = document.getElementById('folder-target-summary');
  const submit = document.getElementById('folder-submit');
  const summary = document.getElementById('folder-summary');
  const error = document.getElementById('folder-error');
  const resultLink = document.getElementById('folder-result-link');
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  let selected = [];
  let directories = [];
  let targetFolder = null;

  function setStatus(message, isError = false) {
    if (summary) summary.textContent = message;
    if (error) {
      error.textContent = isError ? message : '';
      error.hidden = !isError;
    }
  }

  async function ensureEmptyTarget(target) {
    for await (const _entry of target.keys()) {
      throw new Error('Папка назначения не пуста. Выберите другую пустую папку.');
    }
  }

  async function saveResultToFolder(target, resultUrl, onProgress) {
    await ensureEmptyTarget(target);
    const response = await fetch(`${resultUrl}/manifest`, {cache: 'no-store'});
    if (!response.ok) throw new Error('Не получен список файлов результата');
    const manifest = await response.json();
    if (!manifest.root_name || !Array.isArray(manifest.files) || !Array.isArray(manifest.dirs)) {
      throw new Error('Неверный список файлов результата');
    }
    const prefix = `${manifest.root_name}/`;
    function insideSource(path) {
      if (path === manifest.root_name) return '';
      if (!path.startsWith(prefix)) throw new Error(`Неверный путь результата: ${path}`);
      return path.slice(prefix.length);
    }
    const cache = new Map([['', target]]);
    async function ensureDir(relative) {
      if (cache.has(relative)) return cache.get(relative);
      let current = target;
      let walked = '';
      for (const piece of relative.split('/').filter(Boolean)) {
        walked = walked ? `${walked}/${piece}` : piece;
        if (cache.has(walked)) {
          current = cache.get(walked);
        } else {
          current = await current.getDirectoryHandle(piece, {create: true});
          cache.set(walked, current);
        }
      }
      return current;
    }
    for (const directory of manifest.dirs) await ensureDir(insideSource(directory));
    let completed = 0;
    for (const serverPath of manifest.files) {
      const relative = insideSource(serverPath);
      if (!relative) throw new Error('Неверное имя файла результата');
      const slash = relative.lastIndexOf('/');
      const parent = await ensureDir(slash < 0 ? '' : relative.slice(0, slash));
      const fileName = relative.slice(slash + 1);
      const encoded = serverPath.split('/').map(encodeURIComponent).join('/');
      const data = await fetch(`${resultUrl}/files/${encoded}`, {cache: 'no-store'});
      if (!data.ok) throw new Error(`Не удалось загрузить ${serverPath}`);
      const file = await parent.getFileHandle(fileName, {create: true});
      const writable = await file.createWritable();
      try {
        await writable.write(await data.blob());
      } finally {
        await writable.close();
      }
      completed++;
      onProgress(`Сохранение файлов: ${completed} / ${manifest.files.length}`);
    }
    return {files: completed, directories: manifest.dirs.length - 1};
  }

  if (choose && input && submit) {
    function setFiles(files, dirs) {
      selected = files;
      directories = dirs;
      const bytes = selected.reduce((sum, item) => sum + item.file.size, 0);
      const root = selected[0]?.path?.split('/')[0] || '(папка)';
      setStatus(`${root} · ${selected.length} файлов · ${dirs.length} каталогов · ${(bytes / 1024 / 1024).toFixed(2)} МБ`);
      submit.disabled = !selected.length || selected.length > 2500 || bytes > 150 * 1024 * 1024;
      if (submit.disabled && selected.length) setStatus('Превышен лимит: максимум 2500 файлов и 150 МБ.', true);
    }

    input.addEventListener('change', () => {
      // webkitdirectory не перечисляет пустые каталоги; для них нужен Chrome/Edge или ZIP.
      setFiles(Array.from(input.files).map(file => ({file, path: file.webkitRelativePath || file.name})), []);
    });

    choose.addEventListener('click', async () => {
      if (!window.showDirectoryPicker) { input.click(); return; }
      try {
        const root = await window.showDirectoryPicker({mode: 'read'});
        const files = [];
        const dirs = [];
        async function scan(folder, relative) {
          dirs.push(relative);
          for await (const [name, entry] of folder.entries()) {
            const path = `${relative}/${name}`;
            if (entry.kind === 'directory') await scan(entry, path);
            else files.push({file: await entry.getFile(), path});
          }
        }
        setStatus('Чтение структуры папки…');
        await scan(root, root.name);
        setFiles(files, dirs);
      } catch (cause) {
        if (cause.name !== 'AbortError') setStatus(`Ошибка выбора папки: ${cause.message}`, true);
      }
    });

    if (targetPick && targetSummary) {
      if (!window.showDirectoryPicker) {
        targetPick.disabled = true;
        targetSummary.textContent = 'Выбор папки назначения доступен в Chrome/Edge. В этом браузере скачайте результат ZIP.';
        const fallback = document.getElementById('folder-fallback');
        if (fallback) fallback.open = true;
      } else {
        targetPick.addEventListener('click', async () => {
          try {
            // Вызов должен происходить непосредственно после нажатия пользователя.
            const chosen = await window.showDirectoryPicker({mode: 'readwrite'});
            await ensureEmptyTarget(chosen);
            targetFolder = chosen;
            targetSummary.textContent = `${chosen.name} · пустая папка выбрана. Результат сохранится сюда автоматически.`;
            targetSummary.classList.add('selected');
          } catch (cause) {
            if (cause.name === 'AbortError') return;
            targetFolder = null;
            targetSummary.textContent = cause.message;
            targetSummary.classList.remove('selected');
          }
        });
      }
    }

    submit.addEventListener('click', async () => {
      if (!selected.length) return;
      submit.disabled = true;
      choose.disabled = true;
      if (targetPick) targetPick.disabled = true;
      setStatus(`Загрузка ${selected.length} файлов и выполнение миграции…`);
      const form = new FormData();
      form.append('csrf', token || '');
      for (const directory of directories) form.append('dirs', directory);
      for (const item of selected) {
        form.append('files', item.file, item.file.name);
        form.append('paths', item.path);
      }
      let resultUrl = null;
      try {
        const response = await fetch('/folders/import', {method: 'POST', body: form});
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Не удалось перенести папку');
        resultUrl = result.result_url;
        if (targetFolder) {
          setStatus('Миграция завершена. Сохранение в выбранную папку…');
          await saveResultToFolder(targetFolder, resultUrl, message => setStatus(message));
          try { window.sessionStorage.setItem(`docshift-saved:${resultUrl}`, targetFolder.name); } catch (_error) {}
          window.location.assign(resultUrl);
        } else {
          window.location.assign(resultUrl);
        }
      } catch (cause) {
        if (resultUrl) {
          setStatus(`Миграция выполнена, но сохранение в папку не завершено: ${cause.message}. Откройте результат и скачайте ZIP или выберите другую пустую папку.`, true);
          resultLink.href = resultUrl;
          resultLink.hidden = false;
        } else {
          setStatus(`Не удалось перенести папку: ${cause.message}`, true);
          submit.disabled = false;
          choose.disabled = false;
          if (targetPick && window.showDirectoryPicker) targetPick.disabled = false;
        }
      }
    });
  }

  const save = document.getElementById('save-folder');
  const saveStatus = document.getElementById('save-folder-status');
  if (save && saveStatus) {
    function showSaved(name) {
      saveStatus.textContent = `Результат сохранён в «${name}».`;
      save.textContent = '▣ Сохранить ещё в другую пустую папку';
      const title = document.getElementById('folder-result-title');
      const caption = document.getElementById('folder-result-caption');
      const help = document.getElementById('folder-save-help');
      if (title) title.textContent = `Готово — файлы сохранены в «${name}»`;
      if (caption) caption.textContent = 'Структура каталогов и содержимое файлов сохранены. Результат миграции проверен.';
      if (help) help.textContent = 'Результат уже находится в выбранной папке. При необходимости скачайте ZIP или сохраните ещё одну копию в другую пустую папку.';
    }
    try {
      const savedName = window.sessionStorage.getItem(`docshift-saved:${save.dataset.resultUrl}`);
      if (savedName) showSaved(savedName);
    } catch (_error) {}
    if (!window.showDirectoryPicker) {
      save.disabled = true;
      saveStatus.textContent = 'Для записи прямо в выбранную папку нужен Chrome или Edge. Используйте кнопку «Скачать результат ZIP».';
      return;
    }
    save.addEventListener('click', async () => {
      let target;
      try {
        target = await window.showDirectoryPicker({mode: 'readwrite'});
        await ensureEmptyTarget(target);
      } catch (cause) {
        if (cause.name !== 'AbortError') saveStatus.textContent = `Ошибка выбора: ${cause.message}`;
        return;
      }
      save.disabled = true;
      try {
        await saveResultToFolder(target, save.dataset.resultUrl,
          message => { saveStatus.textContent = message; });
        try { window.sessionStorage.setItem(`docshift-saved:${save.dataset.resultUrl}`, target.name); } catch (_error) {}
        showSaved(target.name);
      } catch (cause) {
        saveStatus.textContent = `Не удалось завершить сохранение: ${cause.message}. Можно скачать ZIP или выбрать другую пустую папку.`;
        save.disabled = false;
      }
    });
  }
})();
