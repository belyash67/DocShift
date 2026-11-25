import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

function element() {
  const handlers = new Map();
  return {
    handlers, disabled: false, hidden: false, textContent: '',
    classList: {add() {}, remove() {}},
    addEventListener(name, handler) { handlers.set(name, handler); },
    async fire(name) { await handlers.get(name)(); },
  };
}

function directory(name) {
  const dirs = new Map();
  const files = new Map();
  return {
    name, dirs, files,
    async *keys() { yield* dirs.keys(); yield* files.keys(); },
    async getDirectoryHandle(key) {
      if (!dirs.has(key)) dirs.set(key, directory(key));
      return dirs.get(key);
    },
    async getFileHandle(key) {
      if (!files.has(key)) {
        const entry = {content: null, async createWritable() {
          return {async write(blob) { entry.content = await blob.text(); }, async close() {}};
        }};
        files.set(key, entry);
      }
      return files.get(key);
    },
  };
}

const sourceFile = Object.assign(new Blob(['data']), {name: 'a.txt'});
const emptySourceDir = {kind: 'directory', async *entries() {}};
const source = {name: 'Источник', async *entries() {
  yield ['docs', {kind: 'directory', async *entries() {
    yield ['a.txt', {kind: 'file', async getFile() { return sourceFile; }}];
  }}];
  yield ['empty', emptySourceDir];
}};
const target = directory('Итоговые данные');
const ids = ['folder-pick', 'folder-input', 'folder-target-pick', 'folder-target-summary',
  'folder-submit', 'folder-summary', 'folder-error', 'folder-result-link'];
const elements = Object.fromEntries(ids.map(id => [id, element()]));
let assigned = null;
const saved = new Map();
const requests = [];
const context = {
  Blob, URL, URLSearchParams, encodeURIComponent,
  FormData: class {append() {}},
  document: {
    getElementById(id) { return elements[id] || null; },
    querySelector() { return {content: 'test-token'}; },
  },
  window: {
    location: {origin: 'http://127.0.0.1:5000', assign(url) { assigned = url; }},
    sessionStorage: {setItem(key, value) { saved.set(key, value); }, getItem(key) { return saved.get(key) || null; }},
    async showDirectoryPicker({mode}) { return mode === 'read' ? source : target; },
  },
  async fetch(url) {
    requests.push(url);
    if (url === '/folders/import') return {ok: true, async json() { return {ok: true, result_url: '/folders/job'}; }};
    if (url === '/folders/job/manifest') return {ok: true, async json() { return {
      root_name: 'Источник', dirs: ['Источник', 'Источник/docs', 'Источник/empty'],
      files: ['Источник/docs/a.txt'],
    }; }};
    if (url === '/folders/job/files/%D0%98%D1%81%D1%82%D0%BE%D1%87%D0%BD%D0%B8%D0%BA/docs/a.txt') {
      return {ok: true, async blob() { return new Blob(['data']); }};
    }
    throw new Error(`Unexpected request: ${url}`);
  },
};
runInNewContext(readFileSync(new URL('../static/js/folder.js', import.meta.url), 'utf8'), context);
await elements['folder-pick'].fire('click');
await elements['folder-target-pick'].fire('click');
await elements['folder-submit'].fire('click');
assert.equal(target.dirs.has('Источник'), false);
assert.equal(target.dirs.has('empty'), true);
assert.equal(target.dirs.get('docs').files.get('a.txt').content, 'data');
assert.equal(assigned, '/folders/job');
assert.equal(saved.get('docshift-saved:/folders/job'), 'Итоговые данные');
assert.equal(requests.length, 3);
const resultElements = Object.fromEntries(['save-folder', 'save-folder-status', 'folder-result-title',
  'folder-result-caption', 'folder-save-help'].map(id => [id, element()]));
resultElements['save-folder'].dataset = {resultUrl: '/folders/job'};
runInNewContext(readFileSync(new URL('../static/js/folder.js', import.meta.url), 'utf8'), {
  ...context,
  document: {
    getElementById(id) { return resultElements[id] || null; },
    querySelector() { return null; },
  },
  window: {...context.window, location: {search: ''}},
});
assert.match(resultElements['folder-result-title'].textContent, /Итоговые данные/);
assert.match(resultElements['folder-save-help'].textContent, /уже находится/);
console.log('Folder destination flow OK');
