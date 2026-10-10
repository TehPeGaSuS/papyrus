'use strict';
/**
 * paste_create.js: logic for the paste creation page.
 * Depends on: nothing but the DOM (app.js only supplies the theme toggle).
 *
 * A paste is one or more files, each in its own card (filename, language, indent mode and size, wrap, and
 * a textarea). Besides posting the paste it gives the plain textareas the editor basics from Gist-style
 * editors: indent mode (spaces or tabs), indent size (2, 4, 8), soft or no wrap, Tab/Shift+Tab indenting,
 * and keeping the indentation of the previous line on Enter.
 */

document.addEventListener('DOMContentLoaded', function () {
    const $ = id => document.getElementById(id);
    const cardsBox    = $('cards');
    const submitBtn   = $('submitBtn');
    const submitLabel = submitBtn.textContent;          // "Paste": put back after a failed attempt
    const addFileBtn  = $('addFileBtn');
    const maxFiles    = parseInt(cardsBox.dataset.maxFiles, 10) || 1;

    const store = {
        get(key)       { try { return localStorage.getItem(key); } catch (e) { return null; } },
        set(key, value){ try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    };

    const cards = () => Array.from(cardsBox.querySelectorAll('.file-editor'));
    const parts = card => ({
        name: card.querySelector('.f-name'), lang: card.querySelector('.f-lang'),
        mode: card.querySelector('.f-indent-mode'), size: card.querySelector('.f-indent-size'),
        wrap: card.querySelector('.f-wrap'), text: card.querySelector('.f-content'), remove: card.querySelector('.f-remove'),
    });
    let current = cards()[0];            // the card that has (or last had) the focus: dropped files go there

    // ── Language: "Auto" follows the filename's extension ────────────────────
    let hints = { extensions: {}, names: {} };
    try { hints = JSON.parse($('languageHints').textContent); } catch (e) { /* no auto label */ }

    function detectLanguage(name) {
        name = (name || '').trim().toLowerCase();
        if (!name) return null;
        const dot = name.lastIndexOf('.');
        return hints.names[name] || (dot >= 0 ? hints.extensions[name.slice(dot + 1)] : null) || null;
    }

    function updateAutoLabel(card) {
        const { lang, name } = parts(card);
        const option = lang.querySelector('option[value="auto"]');
        if (!option) return;
        const detected = detectLanguage(name.value);
        option.textContent = detected ? 'Auto (' + detected + ')' : 'Auto-detect';
    }

    let languageOptions = null;          // the <option> list, fetched once and copied into every card
    function fillLanguages(card) {
        if (!languageOptions) return;
        const { lang } = parts(card);
        lang.innerHTML = '';
        languageOptions.forEach(o => lang.appendChild(o.cloneNode(true)));
        updateAutoLabel(card);
    }
    fetch('/api/languages')
        .then(r => r.json())
        .then(langs => {
            languageOptions = [];
            const auto = document.createElement('option');
            auto.value = 'auto';
            languageOptions.push(auto);
            langs.forEach(l => {
                const o = document.createElement('option');
                o.value = l.value;
                o.textContent = l.name;
                languageOptions.push(o);
            });
            cards().forEach(fillLanguages);
        })
        .catch(() => {});

    // ── Expiry and comment preferences (for the whole paste) ──────────────
    const expirySelect = $('expires_in');
    const savedExpiry  = store.get('preferred_expiry');
    // Only reuse a saved choice the site still offers (the options are configurable).
    if (savedExpiry && Array.from(expirySelect.options).some(o => o.value === savedExpiry)) expirySelect.value = savedExpiry;
    expirySelect.addEventListener('change', () => store.set('preferred_expiry', expirySelect.value));

    const commentsCheck = $('allowComments');
    if (commentsCheck) {
        const saved = store.get('preferred_comments');     // your last choice here, else the site's default
        commentsCheck.checked = saved === null ? commentsCheck.dataset.default === '1' : saved === 'true';
        commentsCheck.addEventListener('change', () => store.set('preferred_comments', commentsCheck.checked));
    }

    // ── One file card: editor options, indenting ─────────────────────────────
    function initCard(card) {
        const { name, mode: indentMode, size: indentSize, wrap: wrapMode, text: textarea } = parts(card);
        fillLanguages(card);
        name.addEventListener('input', () => updateAutoLabel(card));
        textarea.addEventListener('focus', () => { current = card; });
        name.addEventListener('focus', () => { current = card; });

        // ── Editor options: indent mode / size, line wrap ────────────────────────
        function restore(select, key, fallback) {
            const saved = store.get(key);
            select.value = Array.from(select.options).some(o => o.value === saved) ? saved : fallback;
        }
        restore(indentMode, 'indent_mode', 'spaces');
        restore(indentSize, 'indent_size', '4');
        restore(wrapMode,   'wrap_mode',   'off');          // like GitHub: long lines scroll instead of wrapping

        function applyEditorOptions() {
            textarea.style.tabSize = indentSize.value;                 // how a tab character is drawn
            const off = wrapMode.value === 'off';
            textarea.wrap = off ? 'off' : 'soft';
            textarea.classList.toggle('no-wrap', off);
        }
        [[indentMode, 'indent_mode'], [indentSize, 'indent_size'], [wrapMode, 'wrap_mode']].forEach(([select, key]) =>
            select.addEventListener('change', () => { store.set(key, select.value); applyEditorOptions(); textarea.focus(); }));
        applyEditorOptions();

        const unit = () => indentMode.value === 'tabs' ? '\t' : ' '.repeat(parseInt(indentSize.value, 10));

        // Edits go through execCommand so the browser's undo (Ctrl+Z) keeps working.
        function replaceRange(start, end, text) {
            textarea.focus();
            textarea.setSelectionRange(start, end);
            if (!document.execCommand || !document.execCommand('insertText', false, text)) {
                textarea.setRangeText(text, start, end, 'end');
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }
        const lineStartOf = pos => textarea.value.lastIndexOf('\n', pos - 1) + 1;
        function lineEndOf(pos) {
            const i = textarea.value.indexOf('\n', pos);
            return i === -1 ? textarea.value.length : i;
        }

        function indent() {
            const { selectionStart: start, selectionEnd: end, value } = textarea;
            const multiline = value.slice(start, end).includes('\n');
            if (!multiline) {
                // A caret (or a selection inside one line): insert one indent, up to the next tab stop.
                let text = unit();
                if (indentMode.value !== 'tabs') {
                    const size = parseInt(indentSize.value, 10);
                    text = ' '.repeat(size - ((start - lineStartOf(start)) % size));
                }
                replaceRange(start, end, text);
                return;
            }
            const from = lineStartOf(start);
            const to = lineEndOf(end > start && value[end - 1] === '\n' ? end - 1 : end);
            const lines = value.slice(from, to).split('\n');
            const block = lines.map(l => (l.trim() ? unit() + l : l)).join('\n');
            replaceRange(from, to, block);
            const added = block.length - (to - from);
            textarea.setSelectionRange(start + (lines[0].trim() ? unit().length : 0), end + added);
        }

        function outdent() {
            const { selectionStart: start, selectionEnd: end, value } = textarea;
            const from = lineStartOf(start);
            const to = lineEndOf(end > start && value[end - 1] === '\n' ? end - 1 : end);
            const size = parseInt(indentSize.value, 10);
            let removedFirst = 0, removedTotal = 0;
            const lines = value.slice(from, to).split('\n').map((l, i) => {
                const m = /^(\t| {1,8})/.exec(l);
                const n = !m ? 0 : m[0] === '\t' ? 1 : Math.min(m[0].length, size);
                if (i === 0) removedFirst = n;
                removedTotal += n;
                return l.slice(n);
            });
            if (!removedTotal) return;
            replaceRange(from, to, lines.join('\n'));
            textarea.setSelectionRange(Math.max(from, start - removedFirst), Math.max(from, end - removedTotal));
        }

        function autoIndent(e) {
            const { selectionStart: start, selectionEnd: end, value } = textarea;
            const lineStart = lineStartOf(start);
            const lead = /^[ \t]*/.exec(value.slice(lineStart, start))[0];    // only what is left of the caret
            const opener = '{[('.includes(value[start - 1] || ' ') ? unit() : '';
            if (!lead && !opener) return;                                      // nothing to carry over
            e.preventDefault();
            replaceRange(start, end, '\n' + lead + opener);
        }

        // The editor must not trap keyboard users: Control+Shift+M switches Tab between indenting and
        // moving focus (as in the Gist editor), and Esc followed by Tab moves focus once.
        let tabMovesFocus = false;
        let releaseTab = false;
        textarea.addEventListener('focus', () => { releaseTab = false; });
        textarea.addEventListener('keydown', e => {
            if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && e.key.toLowerCase() === 'm') {
                e.preventDefault();
                tabMovesFocus = !tabMovesFocus;
                textarea.classList.toggle('tab-moves-focus', tabMovesFocus);
                return;
            }
            if (e.key === 'Escape') { releaseTab = true; return; }
            const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
            if (e.key === 'Tab' && plain && !releaseTab && !tabMovesFocus) {
                e.preventDefault();
                if (e.shiftKey) outdent(); else indent();
                return;
            }
            releaseTab = false;
            if (e.key === 'Enter' && plain && !e.shiftKey && !e.isComposing) autoIndent(e);
        });

    }

    // ── Several cards ────────────────────────────────────────────────────────
    function syncLayout() {
        const all = cards();
        const multi = all.length > 1;
        cardsBox.classList.toggle('multi', multi);
        all.forEach(card => { parts(card).remove.hidden = !multi; });      // "Remove file" only with 2 or more files
        addFileBtn.hidden = all.length >= maxFiles;
    }

    function addCard() {
        if (cards().length >= maxFiles) return null;
        const card = $('cardTemplate').content.firstElementChild.cloneNode(true);
        const { remove } = parts(card);
        cardsBox.appendChild(card);
        initCard(card);
        remove.addEventListener('click', () => removeCard(card));
        syncLayout();
        return card;
    }

    function removeCard(card) {
        // Like pinnwand: ask only when there is something to lose.
        if (parts(card).text.value.trim() && !window.confirm('Remove this file and its text?')) return;
        card.remove();
        if (current === card) current = cards()[0];
        syncLayout();
        parts(current).text.focus();
    }

    const first = cards()[0];
    {
        initCard(first);
        parts(first).remove.addEventListener('click', () => removeCard(first));
    }
    addFileBtn.addEventListener('click', () => { const card = addCard(); if (card) { current = card; parts(card).name.focus(); } });
    syncLayout();

    // ── Load text files into the editor (drop them on the page, or open some) ──
    // Nothing is uploaded: the browser reads each file and puts its text in a card, and what gets pasted is the
    // same plain-text paste as ever. Only UTF-8 text up to the paste size limit is accepted. One file goes
    // into the current card; several files get a card each.
    const maxBytes  = parseInt(first.querySelector('.f-content').dataset.maxBytes, 10) || 2 * 1024 * 1024;
    const fileInput = $('openFile');
    const fileNote  = $('fileNote');
    let noteTimer = null;

    const prettyBytes = n => n < 1024 ? n + ' B' : n < 1024 * 1024 ? (n / 1024).toFixed(1) + ' KB' : (n / 1024 / 1024).toFixed(1) + ' MB';
    function note(message, isError) {
        fileNote.textContent = message;
        fileNote.classList.toggle('error', !!isError);
        clearTimeout(noteTimer);
        noteTimer = setTimeout(() => { fileNote.textContent = ''; fileNote.classList.remove('error'); }, isError ? 9000 : 5000);
    }

    // Replace everything through the browser's editing machinery, so Ctrl+Z brings the old text back.
    function replaceEditorText(textarea, text) {
        textarea.focus();
        textarea.select();
        if (!document.execCommand || !document.execCommand('insertText', false, text)) {
            textarea.value = text;
            textarea.dispatchEvent(new Event('input', { bubbles: true }));
        }
        textarea.setSelectionRange(0, 0);
        textarea.scrollTop = 0;
    }

    // Read one file; resolves to its text, or throws an Error whose message says why it was refused.
    async function readTextFile(file) {
        if (file.size === 0) throw new Error('"' + file.name + '" is empty.');
        if (file.size > maxBytes) {
            throw new Error('"' + file.name + '" is ' + prettyBytes(file.size) + '; pastes are limited to ' + prettyBytes(maxBytes) + '.');
        }
        let text;
        try {
            text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
        } catch (e) {
            throw new Error('"' + file.name + '" is not a UTF-8 text file, so it was not loaded.');
        }
        if (text.indexOf('\u0000') !== -1) throw new Error('"' + file.name + '" looks like a binary file, so it was not loaded.');
        return text;
    }

    function putFileInCard(card, file, text) {
        const { name, text: textarea } = parts(card);
        replaceEditorText(textarea, text);
        if (!name.value.trim()) {                           // keep a name you typed on purpose
            name.value = file.name;
            name.dispatchEvent(new Event('input'));         // refreshes the "Auto (language)" label
        }
    }

    async function handleFiles(files) {
        if (!files.length) return;
        if (files.length === 1) {
            try {
                const text = await readTextFile(files[0]);
                putFileInCard(current, files[0], text);
                note('Loaded "' + files[0].name + '" (' + prettyBytes(files[0].size) + ').', false);
            } catch (e) { note(e.message, true); }
            return;
        }
        const problems = [];
        let loaded = 0, skipped = 0;
        for (const file of files) {
            let text;
            try { text = await readTextFile(file); } catch (e) { problems.push(e.message); continue; }
            // An empty card (the page's first one, say) takes the first file; every other file gets a new card.
            let card = loaded === 0 && !parts(current).text.value.trim() ? current : addCard();
            if (!card) { skipped++; continue; }
            putFileInCard(card, file, text);
            loaded++;
        }
        if (skipped) problems.push(skipped + (skipped === 1 ? ' file' : ' files') + ' left out: a paste holds at most ' + maxFiles + ' files.');
        note((loaded ? 'Loaded ' + loaded + (loaded === 1 ? ' file. ' : ' files. ') : '') + problems.join(' '), problems.length > 0);
    }

    const carriesFiles = e => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') !== -1;
    ['dragenter', 'dragover'].forEach(type => document.addEventListener(type, e => {
        if (!carriesFiles(e)) return;                       // dragging text around keeps working as usual
        e.preventDefault();                                 // also stops the browser from opening the file
        e.dataTransfer.dropEffect = 'copy';
        cardsBox.classList.add('drop-target');
    }));
    document.addEventListener('dragleave', e => { if (e.relatedTarget === null) cardsBox.classList.remove('drop-target'); });
    document.addEventListener('drop', e => {
        if (!carriesFiles(e)) return;
        e.preventDefault();
        cardsBox.classList.remove('drop-target');
        handleFiles(Array.from(e.dataTransfer.files));
    });
    $('openFileBtn').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { handleFiles(Array.from(fileInput.files)); fileInput.value = ''; });

    // ── Ctrl/Cmd+S shortcut ──────────────────────────────────────────────────
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            submitPaste();
        }
    });
    submitBtn.addEventListener('click', submitPaste);

    // ── Clear: back to one empty file ────────────────────────────────────────
    const clearBtn = $('clearBtn');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            const all = cards();
            if (all.length > 1 && all.some(c => parts(c).text.value.trim()) && !window.confirm('Clear all ' + all.length + ' files?')) return;
            all.slice(1).forEach(c => c.remove());
            const { name, text } = parts(all[0]);
            text.value = '';
            name.value = '';
            $('title').value = '';
            updateAutoLabel(all[0]);
            current = all[0];
            syncLayout();
            text.focus();
        });
    }

    // ── Submit ───────────────────────────────────────────────────────────────
    async function submitPaste() {
        const all = cards();
        const empty = all.find(c => !parts(c).text.value.trim());
        if (empty) {
            if (all.length > 1) note('File ' + (all.indexOf(empty) + 1) + ' is empty. Add its text or remove it.', true);
            parts(empty).text.focus();
            return;
        }

        submitBtn.disabled    = true;
        submitBtn.textContent = '…';
        try {
            const resp = await fetch('/create', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    title:       $('title').value.trim(),       // empty: the server uses the first filename
                    files:       all.map(c => {
                        const p = parts(c);
                        return { filename: p.name.value.trim(), content: p.text.value, language: p.lang.value || 'auto' };   // "auto": from the filename
                    }),
                    expires_in:  expirySelect.value,
                    comments: commentsCheck ? commentsCheck.checked : false,       // (no checkbox: the site has comments off)
                }),
            });
            const result = await resp.json();
            if (result.error) {
                alert('Error: ' + result.error);
                submitBtn.disabled    = false;
                submitBtn.textContent = submitLabel;
                return;
            }
            window.location.href = result.url + '?created=1';   // the removal cookie is already set
        } catch (err) {
            console.error(err);
            alert('Failed to create paste. Try again.');
            submitBtn.disabled    = false;
            submitBtn.textContent = submitLabel;
        }
    }
});
